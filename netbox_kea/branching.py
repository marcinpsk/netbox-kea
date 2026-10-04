# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The one owner of every netbox-branching fact the plugin relies on (ADR 0007).

netbox-branching is optional. Without it, every function here is a no-op. This is the only
module in the plugin that imports ``netbox_branching``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from http import HTTPStatus
from typing import Any

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db import connections, models
from django.db.models.signals import pre_delete, pre_save
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.html import escape
from django_htmx.http import HttpResponseClientRedirect, HttpResponseClientRefresh
from rest_framework.permissions import SAFE_METHODS
from utilities.exceptions import AbortRequest

APP_LABEL = "netbox_kea"
BRANCHING_APP_LABEL = "netbox_branching"
SOURCES_HEADER = "X-NetBox-Kea-Sources"
# The REST "code" of each refusal.
BRANCH_WRITE_REFUSED = "branch_write_refused"
BRANCH_SELECTION_UNUSABLE = "branch_selection_unusable"
# The model whose delete in a branch means a delete of an object that a Kea Server owns (ADR 0006).
OWNERSHIP_LINK_LABEL = f"{APP_LABEL}.IPAMOwnershipLink"


class BranchActive(AbortRequest):
    """A plugin change was refused because a branch is active. Not a KeaException, so no Kea handler catches it.

    An AbortRequest, so a NetBox view rolls back, clears its queued events and shows ``message``. NetBox marks
    ``message`` safe, so it holds the escaped text.
    """

    def __init__(self, operation: str, branch: Any) -> None:
        self.text = f"{operation} is refused. {_refused_text(branch)}"
        super().__init__(escape(self.text))
        self.operation = operation
        self.branch = branch

    def __str__(self) -> str:
        return self.text


def installed() -> bool:
    """Return whether netbox-branching is an installed app."""
    return apps.is_installed(BRANCHING_APP_LABEL)


def active_branch() -> Any:
    """Return the active Branch, or None when no branch is active or netbox-branching is absent."""
    if not installed():
        return None
    from netbox_branching.contextvars import active_branch as branch_context

    return branch_context.get()


def supports_branching(model: type[models.Model]) -> bool:
    """Return the native configured routing decision, including model exemptions."""
    if not installed():
        return False
    from netbox_branching.utilities import supports_branching as native_supports_branching

    return native_supports_branching(model)


@contextmanager
def branch_scope(branch: Any):
    """Use the native branch context without exposing the optional plugin to callers."""
    from netbox_branching.utilities import activate_branch

    with activate_branch(branch):
        yield


def connection_aliases() -> set[str]:
    """Include dynamic native branch aliases without opening a database connection."""
    aliases = set(connections)
    if installed():
        from netbox_branching.utilities import _get_tracked_branch_aliases

        aliases.update(_get_tracked_branch_aliases())
    return aliases


def has_native_delete_receipt(branch: Any, instance: models.Model) -> bool:
    """Verify native applied deletion history in the current main request transaction."""
    from django.contrib.contenttypes.models import ContentType
    from netbox.context import current_request

    request = current_request.get()
    if request is None or not connections["default"].in_atomic_block:
        return False
    return (
        branch.applied_changes.using("default")
        .filter(
            change__changed_object_type=ContentType.objects.get_for_model(instance),
            change__changed_object_id=instance.pk,
            change__action="delete",
            change__request_id=request.id,
        )
        .exists()
    )


def branch_for_alias(alias: str | None) -> Any:
    """Resolve an explicitly selected native branch connection without changing context."""
    if alias is None or not installed():
        return None
    from netbox.plugins import get_plugin_config
    from netbox_branching.database import BranchAwareRouter
    from netbox_branching.models import Branch

    prefix = f"{BranchAwareRouter.connection_prefix}{get_plugin_config(BRANCHING_APP_LABEL, 'schema_prefix')}"
    if not alias.startswith(prefix):
        return None
    branch = Branch.objects.using("default").filter(schema_id=alias.removeprefix(prefix)).first()
    if branch is None:
        raise AbortRequest("The branch schema is unavailable. Create a fresh branch to use DHCP Import Mappings.")
    return branch


def refuse_in_branch(operation: str) -> None:
    """Raise BranchActive when a branch is active."""
    if (branch := active_branch()) is not None:
        raise BranchActive(operation, branch)


@dataclass(frozen=True)
class BranchBinding:
    """The branch that was active when a Kea client was built. A thread-pool worker does not inherit the context."""

    branch: Any

    def refuse(self, operation: str) -> None:
        """Raise BranchActive when the bound branch is set, or else when a branch is active now."""
        branch = self.branch if self.branch is not None else active_branch()
        if branch is not None:
            raise BranchActive(operation, branch)


def bind() -> BranchBinding:
    """Bind a Kea client to the branch that is active now, or to none."""
    return BranchBinding(active_branch())


def is_branchable(model: type[models.Model]) -> bool | None:
    """Branch DHCP Import Mappings, keep other plugin models in main, and defer for other apps.

    A constant, so it cannot raise: netbox-branching treats a raising resolver as no answer and then
    makes a change-logged model such as Server branchable. Guard 2 in test_branching.py computes the
    relations that a delete in a branch reaches, and fails when one is outside the design. netbox-branching
    also calls this with historical models.
    """
    if model._meta.app_label != APP_LABEL:
        return None
    return model._meta.model_name == "keadhcplink"


def register() -> None:
    """Register the resolver with netbox-branching and connect the plugin row receivers, from the plugin's ready()."""
    if not installed():
        return
    from netbox_branching.utilities import register_branching_resolver

    register_branching_resolver(is_branchable)
    connect_branch_refusal()
    from netbox_branching.signals import pre_merge, pre_revert

    pre_merge.connect(_mapping_merge_preflight, dispatch_uid="netbox_kea.mapping_merge_preflight")
    pre_revert.connect(_mapping_revert_preflight, dispatch_uid="netbox_kea.mapping_revert_preflight")
    from netbox_branching.models import Branch

    if not getattr(Branch, "_kea_mapping_actions", False):
        Branch._kea_mapping_actions = True
        for action in ("merge", "revert"):
            setattr(Branch, action, _mapping_action(getattr(Branch, action), action))
        from netbox_branching.merge_strategies.iterative import IterativeMergeStrategy
        from netbox_branching.merge_strategies.squash import SquashMergeStrategy

        for strategy in (SquashMergeStrategy, IterativeMergeStrategy):
            for action in ("merge", "revert"):
                setattr(strategy, action, _mapping_strategy(getattr(strategy, action), action))


def _mapping_strategy(original: Callable[..., Any], action: str) -> Callable[..., Any]:
    @wraps(original)
    def wrapped(strategy: Any, branch: Any, changes: Any, request: Any, logger: Any, user: Any) -> Any:
        from netbox_branching.merge_strategies.squash import SquashMergeStrategy

        from .dhcp_mapping_lifecycle import metadata_scope, prepare_replay

        with metadata_scope():
            collapsed, _ = SquashMergeStrategy._collapse_changes(
                sorted(changes, key=lambda change: change.time), logger
            )
            # Squash writes each collapsed target once. Iterative replay keeps its native per-change semantics.
            prepare_replay(branch, collapsed, action, request if isinstance(strategy, SquashMergeStrategy) else None)
            return original(strategy, branch, changes, request, logger, user)

    return wrapped


def _mapping_action(original: Callable[..., Any], action: str) -> Callable[..., Any]:
    @wraps(original)
    def wrapped(branch: Any, *args: Any, **kwargs: Any) -> Any:
        from .dhcp_mapping_lifecycle import action_scope

        with action_scope(branch, action):
            return original(branch, *args, **kwargs)

    return wrapped


def _mapping_merge_preflight(sender: Any, branch: Any, **kwargs: Any) -> None:
    from .dhcp_mapping_lifecycle import preflight_action

    preflight_action(branch, "merge")


def _mapping_revert_preflight(sender: Any, branch: Any, **kwargs: Any) -> None:
    from .dhcp_mapping_lifecycle import preflight_action

    preflight_action(branch, "revert")


def _refuse_save_in_branch(sender: Any, instance: Any, **kwargs: Any) -> None:
    refuse_in_branch(f"A save of {sender._meta.label} {instance.pk}")


def _refuse_delete_in_branch(sender: Any, instance: Any, **kwargs: Any) -> None:
    refuse_in_branch(f"A delete of {sender._meta.label} {instance.pk}")


def _refuse_owned_delete_in_branch(sender: Any, instance: Any, **kwargs: Any) -> None:
    if (branch := active_branch()) is None:
        return
    try:
        owned = instance.owned_object
    except ObjectDoesNotExist:
        raise BranchActive(f"A delete of {sender._meta.label} {instance.pk}", branch) from None
    raise BranchActive(
        f"A delete of {owned._meta.verbose_name} {owned}, which Kea Server {instance.server} owns,", branch
    )


def refusal_uid(model: type[models.Model]) -> str:
    """Return the dispatch_uid of the branch refusal receivers of *model*."""
    return f"{APP_LABEL}.refuse_in_branch.{model._meta.label}"


def connect_refusal(model: type[models.Model]) -> None:
    """Refuse a save or a delete of a *model* row in a branch.

    A pre_delete receiver disables Django's fast delete, so a queryset delete() reaches it too.
    """
    uid = refusal_uid(model)
    owned = model._meta.label == OWNERSHIP_LINK_LABEL
    pre_save.connect(_refuse_save_in_branch, sender=model, dispatch_uid=uid)
    pre_delete.connect(
        _refuse_owned_delete_in_branch if owned else _refuse_delete_in_branch, sender=model, dispatch_uid=uid
    )


def connect_branch_refusal() -> None:
    """Refuse branch changes to plugin rows that describe live Kea or IPAM ownership."""
    for model in apps.get_app_config(APP_LABEL).get_models():
        if not is_branchable(model):
            connect_refusal(model)


def plugin_owned(view_func: Callable[..., Any]) -> bool:
    """Return whether a resolved URL callback is defined inside netbox_kea."""
    return view_func.__module__.split(".")[0] == APP_LABEL


def main_url() -> str:
    """Return the Server list on main: the explicit switch to main, with nothing taken from a request."""
    from netbox_branching.constants import QUERY_PARAM

    return f"{reverse('plugins:netbox_kea:server_list')}?{QUERY_PARAM}="


def selection_unusable(request: HttpRequest) -> bool:
    """Return whether a request without an active branch still selects one, in netbox-branching's precedence."""
    from netbox_branching.constants import COOKIE_NAME, QUERY_PARAM

    if QUERY_PARAM in request.GET:
        return bool(request.GET[QUERY_PARAM])
    return bool(request.COOKIES.get(COOKIE_NAME))


def _branch_refused_text(branch: Any) -> str:
    return (
        f"Branch {branch} is active. Kea servers, sync settings and IPAM ownership come from main. "
        "DHCP Import Mappings follow their targets in a supported fresh branch. "
        "Live Kea and import changes require main. Switch to main to make this change."
    )


_UNUSABLE_TEXT = (
    "The selected branch is not usable: it is unknown, merged, archived or not ready. "
    "netbox-kea refused the change, and nothing changed."
)


def _refused_text(branch: Any) -> str:
    # netbox-branching 1.2.1 activates its own 400 response as the branch for an unready API branch header.
    return _UNUSABLE_TEXT if isinstance(branch, HttpResponse) else _branch_refused_text(branch)


def _refusal(request: HttpRequest, text: str, code: str, htmx: Callable[[], HttpResponse]) -> HttpResponse:
    from utilities.api import is_api_request

    if is_api_request(request):
        return JsonResponse({"detail": text, "code": code}, status=HTTPStatus.CONFLICT)
    if request.htmx:  # type: ignore[attr-defined]
        response = htmx()
        response.status_code = HTTPStatus.CONFLICT
        return response
    return render(
        request,
        "netbox_kea/branch_refused.html",
        {"refusal": text, "main_url": main_url()},
        status=HTTPStatus.CONFLICT,
    )


def refuse_active_branch(request: HttpRequest, branch: Any) -> HttpResponse:
    """Answer a plugin change in a branch with 409; an HTMX page reloads in the branch and shows the message."""
    text = _branch_refused_text(branch)

    def htmx() -> HttpResponse:
        messages.error(request, text)
        return HttpResponseClientRefresh()

    return _refusal(request, text, BRANCH_WRITE_REFUSED, htmx)


def refuse_unusable_selection(request: HttpRequest) -> HttpResponse:
    """Answer a plugin change with an unusable branch selection with 409; an HTMX page goes to main."""

    def htmx() -> HttpResponse:
        messages.error(request, f"{_UNUSABLE_TEXT} The page now shows main.")
        return HttpResponseClientRedirect(main_url())

    return _refusal(request, _UNUSABLE_TEXT, BRANCH_SELECTION_UNUSABLE, htmx)


class BranchRefusalMiddleware:
    """Refuse plugin changes in a branch or with an unusable branch selection, and name the sources in a branch."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Add the sources header to a response from a plugin-owned callback or GraphQL while a branch is active."""
        response = self.get_response(request)
        branch = active_branch()
        match = getattr(request, "resolver_match", None)
        if branch is None or isinstance(branch, HttpResponse) or match is None:
            return response
        if plugin_owned(match.func) or match.view_name == "graphql":
            from .dhcp_mapping_lifecycle import mapping_unavailable_reason

            mappings = "branch" if mapping_unavailable_reason() is None else "unavailable"
            response[SOURCES_HEADER] = (
                f"kea=live; plugin=main; dhcp-import-mappings={mappings}; branch={branch.schema_id}"
            )
        return response

    def process_view(
        self, request: HttpRequest, view_func: Callable[..., Any], view_args: Any, view_kwargs: Any
    ) -> HttpResponse | None:
        """Refuse an unsafe method on a plugin-owned callback before the view runs."""
        if not installed() or not plugin_owned(view_func):
            return None
        branch = active_branch()
        if isinstance(branch, HttpResponse):
            # netbox-branching 1.2.1 activates its own 400 as the branch when an API header names an unready branch.
            return branch
        if request.method in SAFE_METHODS:
            return None
        if branch is not None:
            return refuse_active_branch(request, branch)
        if selection_unusable(request):
            return refuse_unusable_selection(request)
        return None

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        """Render BranchActive from any view as the same 409."""
        if not isinstance(exception, BranchActive):
            return None
        if isinstance(exception.branch, HttpResponse):
            return exception.branch
        return refuse_active_branch(request, exception.branch)

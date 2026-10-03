# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The one owner of every netbox-branching fact the plugin relies on (ADR 0007).

netbox-branching is optional. Without it, every function here is a no-op. This is the only
module in the plugin that imports ``netbox_branching``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.db import models
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
    """Keep every netbox_kea model in main (False), and defer (None) for every other model.

    A constant, so it cannot raise: netbox-branching treats a raising resolver as no answer and then
    makes a change-logged model such as Server branchable. Guard 2 in test_branching.py computes the
    relations that a delete in a branch reaches, and fails when one is outside the design. netbox-branching
    also calls this with historical models.
    """
    if model._meta.label_lower == "netbox_kea.keadhcplink":
        return True
    return False if model._meta.app_label == APP_LABEL else None


def register() -> None:
    """Register the resolver with netbox-branching and connect the plugin row receivers, from the plugin's ready()."""
    if not installed():
        return
    from netbox_branching.utilities import register_branching_resolver

    register_branching_resolver(is_branchable)
    connect_branch_refusal()
    if apps.is_installed("netbox_dhcp"):
        prototype_connect_mapping_guards()


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
    """Refuse a save or a delete of every netbox_kea row in a branch: the resolver keeps each model in main."""
    for model in apps.get_app_config(APP_LABEL).get_models():
        if model._meta.model_name != "keadhcplink":
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
        f"Branch {branch} is active. Kea is live and shared by every branch, and netbox-kea data exists "
        "in main only, so netbox-kea refuses changes in a branch. Switch to main to make this change."
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
            response[SOURCES_HEADER] = f"kea=live; plugin=main; branch={branch.schema_id}"
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


# Throwaway guards for the real-database prototype. These are not a production implementation.


def prototype_mapping_table(branch):
    """Check that this branch owns the required mapping columns."""
    from django.db import connections

    with connections[branch.connection_name].cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            [branch.schema_name, "netbox_kea_keadhcplink"],
        )
        return {"id", "server_id", "object_id", "object_type_id", "last_updated"}.issubset(
            {row[0] for row in cursor.fetchall()}
        )


def prototype_mapping_lock(using):
    """Serialize metadata writers for the current transaction."""
    from django.db import connections

    if not connections[using].in_atomic_block:
        raise ImproperlyConfigured("Prototype mapping writes require an atomic operation")
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [0x4B45414D4150])


def prototype_mapping_context(model):
    """Refuse unsafe branch routing before a mapping operation."""
    branch = active_branch()
    if branch is None:
        return
    from netbox_branching.utilities import supports_branching

    from .models import KeaDhcpLink

    if not prototype_mapping_table(branch):
        raise AbortRequest("Create a fresh branch to change DHCP Import Mappings")
    if not supports_branching(KeaDhcpLink) or not supports_branching(model):
        raise AbortRequest("Branching configuration excludes this DHCP Import Mapping or target")


def prototype_replay_branch(model, object_id):
    """Read synchronous provenance for the current replay deletion."""
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType
    from netbox.context import current_request

    request = current_request.get()
    if request is None:
        return None
    change = (
        ObjectChange.objects.using("default")
        .filter(
            request_id=request.id,
            changed_object_type=ContentType.objects.get_for_model(model),
            changed_object_id=object_id,
            action="delete",
        )
        .order_by("-pk")
        .first()
    )
    if change is None:
        return None
    try:
        return change.application.branch
    except ObjectDoesNotExist:
        return None


def prototype_mapping_identity(data):
    """Select the semantic source and target identity fields."""
    return tuple(
        data.get(field) for field in ("server", "family", "kea_subnet_id", "kea_identity", "object_type", "object_id")
    )


def prototype_mapping_coverage(branch, link):
    """Require matching reversible history for the current mapping."""
    from django.contrib.contenttypes.models import ContentType

    changes = branch.get_changes().filter(
        changed_object_type=ContentType.objects.get_for_model(type(link)),
        changed_object_id=link.pk,
        action="delete",
    )
    identity = prototype_mapping_identity(link.serialize_object())
    if not any(prototype_mapping_identity(change.prechange_data) == identity for change in changes):
        raise AbortRequest("Main has a DHCP Import Mapping this branch cannot restore. Recreate the branch")


def prototype_target_delete(sender, instance, using, **kwargs):
    """Check target cleanup inside the deleting transaction."""
    from django.contrib.contenttypes.models import ContentType

    from .models import KeaDhcpLink

    prototype_mapping_lock(using)
    prototype_mapping_context(sender)
    if active_branch() is not None:
        return
    branch = prototype_replay_branch(sender, instance.pk)
    if branch is None or branch.status != "merging":
        return
    links = KeaDhcpLink.objects.using(using).filter(
        object_type=ContentType.objects.get_for_model(sender),
        object_id=instance.pk,
    )
    for link in links:
        prototype_mapping_coverage(branch, link)


def prototype_mapping_delete(sender, instance, using, **kwargs):
    """Check direct replay deletion of a mapping."""
    prototype_mapping_lock(using)
    prototype_mapping_context(sender)
    if active_branch() is not None:
        return
    branch = prototype_replay_branch(sender, instance.pk)
    if branch is not None and branch.status == "merging":
        prototype_mapping_coverage(branch, instance)


def prototype_mapping_save(sender, instance, using, **kwargs):
    """Validate destination identities and snapshot real updates."""
    from .models import Server

    prototype_mapping_lock(using)
    prototype_mapping_context(sender)
    model = instance.object_type.model_class()
    if model is None or model._meta.label_lower not in {"netbox_dhcp.subnet", "netbox_dhcp.hostreservation"}:
        raise AbortRequest("Unsupported DHCP Import Mapping target")
    prototype_mapping_context(model)
    if instance.family not in (4, 6) or not model.objects.using(using).filter(pk=instance.object_id).exists():
        raise AbortRequest("DHCP Import Mapping target is unavailable")
    if not Server.objects.using("default").filter(pk=instance.server_id).exists():
        raise AbortRequest("DHCP Import Mapping Server is unavailable")
    if instance._state.adding and instance.pk and sender.objects.using(using).filter(pk=instance.pk).exists():
        raise AbortRequest("A newer DHCP Import Mapping already uses this identity")
    if not instance._state.adding:
        old = sender.objects.using(using).get(pk=instance.pk)
        old.snapshot()
        instance._prechange_snapshot = old._prechange_snapshot


def prototype_target_save(sender, instance, using, **kwargs):
    """Serialize transactional target writes and refuse identity reuse."""
    from django.db import connections

    prototype_mapping_context(sender)
    if connections[using].in_atomic_block:
        prototype_mapping_lock(using)
    if instance._state.adding and instance.pk and sender.objects.using(using).filter(pk=instance.pk).exists():
        raise AbortRequest("A newer DHCP object already uses this identity")


def prototype_mapping_preaction(sender, branch, **kwargs):
    """Explain unsupported replay and reset native request history."""
    from core import signals as core_signals

    relevant = (
        branch.get_changes()
        .filter(
            changed_object_type__app_label="netbox_dhcp",
            changed_object_type__model__in=("subnet", "hostreservation"),
            action="delete",
        )
        .exists()
    )
    link_deletes = (
        branch.get_changes()
        .filter(
            changed_object_type__app_label="netbox_kea",
            changed_object_type__model="keadhcplink",
            action="delete",
        )
        .exists()
    )
    if relevant or link_deletes:
        if not prototype_mapping_table(branch):
            raise AbortRequest("Create a fresh branch to merge or revert DHCP Import Mappings")
        if link_deletes and branch.merge_strategy != "squash":
            raise AbortRequest("DHCP Import Mapping deletion requires the squash strategy")
    # Match the real request boundary and the plugin's existing tracked job pattern.
    clear = getattr(core_signals, "clear_signal_history", None)
    if clear is not None:
        clear(sender=sender)


def prototype_connect_mapping_guards():
    """Connect the throwaway lifecycle probes and guards."""
    from netbox_branching.signals import pre_merge, pre_revert

    from .models import KeaDhcpLink

    pre_save.connect(prototype_mapping_save, sender=KeaDhcpLink, dispatch_uid="prototype.mapping.save")
    pre_delete.connect(prototype_mapping_delete, sender=KeaDhcpLink, dispatch_uid="prototype.mapping.delete")
    for name in ("Subnet", "HostReservation"):
        model = apps.get_model("netbox_dhcp", name)
        pre_delete.connect(prototype_target_delete, sender=model, dispatch_uid=f"prototype.target.delete.{name}")
        pre_save.connect(prototype_target_save, sender=model, dispatch_uid=f"prototype.target.save.{name}")
    pre_merge.connect(prototype_mapping_preaction, dispatch_uid="prototype.mapping.merge")
    pre_revert.connect(prototype_mapping_preaction, dispatch_uid="prototype.mapping.revert")

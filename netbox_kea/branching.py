# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The one owner of every netbox-branching fact the plugin relies on (ADR 0007).

netbox-branching is optional. Without it, every function here is a no-op. This is the only
module in the plugin that imports ``netbox_branching``.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from django.apps import apps
from django.contrib import messages
from django.db import models
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django_htmx.http import HttpResponseClientRedirect, HttpResponseClientRefresh

APP_LABEL = "netbox_kea"
BRANCHING_APP_LABEL = "netbox_branching"
SOURCES_HEADER = "X-NetBox-Kea-Sources"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class BranchActive(Exception):
    """A plugin change was refused because a branch is active. Not a KeaException, so no Kea handler catches it."""

    def __init__(self, operation: str, branch: Any) -> None:
        super().__init__(f"{operation} is refused while branch {branch} is active")
        self.operation = operation
        self.branch = branch


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


def is_branchable(model: type[models.Model]) -> bool | None:
    """Keep every netbox_kea model in main (False), and defer (None) for every other model.

    A constant, so it cannot raise: netbox-branching treats a raising resolver as no answer and then
    makes a change-logged model such as Server branchable. Guard 2 in test_branching.py computes the
    foreign-key rule and fails when a plugin model would need a branch copy. netbox-branching also
    calls this with historical models.
    """
    return False if model._meta.app_label == APP_LABEL else None


def register() -> None:
    """Register the resolver with netbox-branching, from the plugin's ready()."""
    if not installed():
        return
    from netbox_branching.utilities import register_branching_resolver

    register_branching_resolver(is_branchable)


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
        f"Branch {branch.name} is active. Kea is live and shared by every branch, and netbox-kea data exists "
        "in main only, so netbox-kea refuses changes in a branch. Switch to main to make this change."
    )


_UNUSABLE_TEXT = (
    "The selected branch is not usable: it is unknown, merged, archived or not ready. "
    "netbox-kea refused the change, and nothing changed."
)


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

    return _refusal(request, text, "branch_write_refused", htmx)


def refuse_unusable_selection(request: HttpRequest) -> HttpResponse:
    """Answer a plugin change with an unusable branch selection with 409; an HTMX page goes to main."""

    def htmx() -> HttpResponse:
        messages.error(request, f"{_UNUSABLE_TEXT} The page now shows main.")
        return HttpResponseClientRedirect(main_url())

    return _refusal(request, _UNUSABLE_TEXT, "branch_selection_unusable", htmx)


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

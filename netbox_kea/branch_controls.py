# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Disable refused actions in final plugin HTML, including inherited NetBox controls."""

from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup, Tag
from django.http import HttpRequest
from django.urls import Resolver404, resolve

from .branching import mutation_form, unsafe_plugin_request

_STYLE = """
.kea-branch-control { display: inline-block; }
.kea-branch-control > [aria-disabled="true"] { pointer-events: none; opacity: .65; cursor: not-allowed; }
.kea-branch-tooltip { visibility: hidden; position: fixed; bottom: 1rem; left: 50%;
 transform: translateX(-50%); z-index: 2000; width: max-content; max-width: min(36rem, 90vw);
 padding: .75rem 1rem; color: #fff; background: #182433; border-radius: .25rem;
 white-space: normal; font-size: .875rem; font-weight: normal; text-align: left; }
.kea-branch-control:hover > .kea-branch-tooltip,
.kea-branch-control:focus > .kea-branch-tooltip { visibility: visible; }
"""
_REQUEST_ATTRIBUTES = ("href", "formaction", "formmethod", "data-bs-toggle", "data-bs-target", "onclick")


class _Targets:
    def __init__(self, request: HttpRequest):
        self.base = request.build_absolute_uri()
        self.origin = urlsplit(self.base)

    def refused(self, target: str, method: str = "GET", *, navigation: bool = False) -> bool:
        url = urlsplit(urljoin(self.base, target))
        if (url.scheme, url.netloc) != (self.origin.scheme, self.origin.netloc):
            return False
        try:
            callback = resolve(url.path).func
        except Resolver404:
            return False
        return unsafe_plugin_request(callback, method) or (navigation and mutation_form(callback))


def _disable(soup: BeautifulSoup, control: Tag, reason: str, identifier: str) -> None:
    control["aria-disabled"] = "true"
    control["tabindex"] = "-1"
    control["class"] = " ".join([*control.get_attribute_list("class"), "disabled"])
    if control.name in {"button", "input", "select", "textarea"}:
        control["disabled"] = ""
    for attr in list(control.attrs):
        if attr in _REQUEST_ATTRIBUTES or attr.startswith(("hx-", "data-hx-")):
            del control[attr]
    wrapper = soup.new_tag(
        "div" if control.name in {"div", "form"} else "span", attrs={"class": "kea-branch-control", "tabindex": "0"}
    )
    tooltip_id = f"kea-branch-reason-{identifier}"
    while soup.find(id=tooltip_id) is not None:
        tooltip_id += "-reason"
    wrapper["aria-describedby"] = tooltip_id
    tooltip = soup.new_tag("span", attrs={"id": tooltip_id, "class": "kea-branch-tooltip", "role": "tooltip"})
    tooltip.string = reason
    control.wrap(wrapper)
    wrapper.append(tooltip)


def _form_owner(soup: BeautifulSoup, control: Tag) -> Tag | None:
    form_id = control.get("form")
    if form_id is not None:
        return soup.find("form", id=str(form_id))
    return control.find_parent("form")


def _method(value: object) -> str:
    method = str(value).lower()
    return method if method in {"get", "post", "dialog"} else "get"


def _submission(soup: BeautifulSoup, control: Tag) -> tuple[str, str] | None:
    kind = str(control.get("type", "submit" if control.name == "button" else "text")).lower()
    if control.name == "button" and kind not in {"button", "reset"}:
        kind = "submit"
    if control.name not in {"button", "input"} or kind not in {"submit", "image"}:
        return None
    form = _form_owner(soup, control)
    if form is None:
        return None
    method = _method(control.get("formmethod", form.get("method", "get")))
    action = str(control.get("formaction", form.get("action", "")))
    return action, method


def _htmx_refused(targets: _Targets, control: Tag) -> bool:
    for verb in ("get", "post", "put", "patch", "delete"):
        target = control.get(f"hx-{verb}", control.get(f"data-hx-{verb}"))
        if target is not None and targets.refused(str(target), verb, navigation=verb == "get"):
            return True
    return False


def _inherited_request_refused(targets: _Targets, control: Tag) -> bool:
    if control.name not in {"a", "button", "input"}:
        return False
    for attr in ("href", "hx-get", "data-hx-get"):
        target = control.get(attr)
        if target is not None and not targets.refused(str(target), navigation=True):
            return False
    return any(_htmx_refused(targets, parent) for parent in control.find_parents())


def _remove_refused_htmx(targets: _Targets, control: Tag) -> None:
    for verb in ("get", "post", "put", "patch", "delete"):
        for attr in (f"hx-{verb}", f"data-hx-{verb}"):
            target = control.get(attr)
            if target is not None and targets.refused(str(target), verb, navigation=verb == "get"):
                del control[attr]


def _preserve_safe_submitters(soup: BeautifulSoup, forms: list[Tag]) -> None:
    for control in soup.find_all(["button", "input"]):
        submission = _submission(soup, control)
        owner = _form_owner(soup, control)
        if submission is not None and submission[1] == "get" and owner in forms and not control.has_attr("formaction"):
            control["formaction"] = submission[0]


def disable_mutations(request: HttpRequest, html: str, reason: str) -> str | None:
    """Return transformed HTML, or None when there is no refused control."""
    soup = BeautifulSoup(html, "html.parser")
    targets = _Targets(request)
    refused = []
    forms = []
    containers = []
    for control in soup.find_all(True):
        if control.name == "form":
            method = _method(control.get("method", "get"))
            if method != "dialog" and targets.refused(str(control.get("action", "")), method):
                forms.append(control)
        if (
            _htmx_refused(targets, control)
            and control.name not in {"a", "button", "input"}
            and control.find(["a", "button", "input"])
        ):
            containers.append(control)
            continue
        submission = _submission(soup, control)
        href = control.get("href")
        if (
            (submission is not None and submission[1] != "dialog" and targets.refused(*submission))
            or (href and targets.refused(str(href), navigation=control.name == "a"))
            or _htmx_refused(targets, control)
            or _inherited_request_refused(targets, control)
        ):
            refused.append(control)
    if not refused and not forms and not containers:
        return None
    _preserve_safe_submitters(soup, forms)
    for container in containers:
        _remove_refused_htmx(targets, container)
    for form in forms:
        # A dialog method outside a dialog aborts native submission, including Enter in an input.
        # Explicit safe GET submitters retain their overrides and their form's input values.
        form["method"] = "dialog"
        form.attrs.pop("action", None)
    response_id = uuid4().hex
    for number, control in enumerate(refused):
        _disable(soup, control, reason, f"{response_id}-{number}")
    style = soup.new_tag("style")
    style.string = _STYLE
    (soup.head or soup).append(style)
    return str(soup)

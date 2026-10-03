# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Real unannotated callbacks exercise the rendered branch controls contract."""

import gzip

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.middleware.gzip import GZipMiddleware
from django.urls import path
from django.views import View
from netbox.urls import urlpatterns as netbox_urlpatterns

from netbox_kea.branching import BranchRefusalMiddleware
from netbox_kea.views._base import _KeaChangeMixin

GZipAlias = GZipMiddleware
RefusalAlias = BranchRefusalMiddleware


class GZipSubclass(GZipMiddleware):
    """A configured encoder can use a class defined by another plugin."""


class RefusalSubclass(BranchRefusalMiddleware):
    """A configured refusal middleware can use a subclass path."""


def passthrough(get_response):
    """A regular middleware factory remains valid in the configured chain."""
    return get_response


HTML = """
<a id="edit" href="/kea-controls/change/">Edit</a>
<a id="jobs" href="/plugins/kea/sync-jobs/">Sync Jobs</a>
<a id="read" href="/kea-controls/read/">Read</a>
<a id="foreign" href="https://example.invalid/kea-controls/change/">Outside</a>
<a id="missing" href="/kea-controls/unknown/">Unresolved</a>
<form id="implicit" method="post" action="/kea-controls/read/">
 <input name="query" value="example"><button id="save">Save</button><button id="invalid-type" type="unknown">Save</button>
 <input id="image" type="image" src="/static/example.png">
 <button id="export" formmethod="get" formaction="/kea-controls/read/">Export</button>
 <button id="export-inherited-action" formmethod="get">Export using form action</button>
 <button id="menu" type="button">Menu</button>
 <a id="cancel" href="/kea-controls/read/">Cancel</a>
</form>
<button id="external" form="implicit">External Save</button>
<form id="search" method="get" action="/kea-controls/read/">
 <input name="query"><button id="find">Search</button>
 <button id="override" formmethod="post" formaction="/kea-controls/read/">Change</button>
</form>
<form id="implicit-only" method="post"><input name="query"></form>
<form id="empty-method" action="/kea-controls/read/"><button id="default-get">Read</button></form>
<div data-hx-post="/kea-controls/read/" hx-trigger="click" hx-target="#results" hx-swap="innerHTML"><button id="inherited">Inherited</button><a id="nested-read" href="/kea-controls/read/" hx-get="/kea-controls/read/">Nested read</a></div>
<button id="patch" data-hx-patch="/kea-controls/read/">Patch</button>
<a id="modal" href="#" hx-get="/kea-controls/change/">Delete</a>
<button id="htmx-read" hx-get="/kea-controls/read/">Refresh</button>
"""


def read(request):
    """Render submission cases through the installed real middleware stack."""
    mode = request.GET.get("response")
    if mode == "json":
        return JsonResponse({"html": HTML})
    if mode == "stream":
        return StreamingHttpResponse([HTML.encode()])
    if mode == "encoded":
        response = HttpResponse(gzip.compress(HTML.encode()))
        response["Content-Encoding"] = "gzip"
        return response
    response = HttpResponse(HTML)
    response["Content-Length"] = str(len(response.content))
    return response


class ChangeView(_KeaChangeMixin, View):
    """A newly registered change form needs no UI-specific annotation."""

    def get(self, request):
        return HttpResponse('<form method="post"><button>Save</button></form>')


urlpatterns = [
    path("kea-controls/read/", read),
    path("kea-controls/change/", ChangeView.as_view()),
    *netbox_urlpatterns,
]

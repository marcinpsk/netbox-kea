# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""NetBox's URLs, plus one UI and one REST route whose view calls refuse_in_branch()."""

from django.http import HttpRequest, HttpResponse
from django.urls import path
from netbox.urls import urlpatterns as netbox_urlpatterns

from netbox_kea import branching

REFUSE_PATH = "kea-branch-test/refuse/"
API_REFUSE_PATH = f"api/{REFUSE_PATH}"


def refuse(request: HttpRequest) -> HttpResponse:
    """Stand in for plugin code that reaches a change sink, as increment 4's Kea transport and model receivers do."""
    branching.refuse_in_branch("a test change")
    return HttpResponse("not refused")


urlpatterns = [path(REFUSE_PATH, refuse), path(API_REFUSE_PATH, refuse), *netbox_urlpatterns]

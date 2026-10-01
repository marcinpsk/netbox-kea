# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""NetBox's URLs, plus UI and REST routes that reach a plugin change sink, two of them outside the plugin."""

from django.http import HttpRequest, HttpResponse
from django.urls import path
from netbox.urls import urlpatterns as netbox_urlpatterns

from netbox_kea import branching
from netbox_kea.models import Server

REFUSE_PATH = "kea-branch-test/refuse/"
API_REFUSE_PATH = f"api/{REFUSE_PATH}"
OUTSIDE_REFUSE_PATH = "kea-branch-test/refuse-outside/"
API_OUTSIDE_SAVE_PATH = "api/kea-branch-test/save-outside/"


def refuse(request: HttpRequest) -> HttpResponse:
    """Stand in for plugin code that reaches a change sink, as increment 4's Kea transport and model receivers do."""
    branching.refuse_in_branch("a test change")
    return HttpResponse("not refused")


def refuse_outside_the_plugin(request: HttpRequest) -> HttpResponse:
    """Stand in for a NetBox view whose code reaches a plugin change sink."""
    branching.refuse_in_branch("a test change")
    return HttpResponse("not refused")


def save_outside_the_plugin(request: HttpRequest, pk: int) -> HttpResponse:
    """Stand in for a NetBox REST view that saves a plugin row."""
    Server.objects.get(pk=pk).save()
    return HttpResponse("saved")


# plugin_owned() reads the callback module: these views are outside netbox_kea.
refuse_outside_the_plugin.__module__ = "core_view_stand_in"
save_outside_the_plugin.__module__ = "core_view_stand_in"

urlpatterns = [
    path(REFUSE_PATH, refuse),
    path(API_REFUSE_PATH, refuse),
    path(OUTSIDE_REFUSE_PATH, refuse_outside_the_plugin),
    path(f"{API_OUTSIDE_SAVE_PATH}<int:pk>/", save_outside_the_plugin),
    *netbox_urlpatterns,
]

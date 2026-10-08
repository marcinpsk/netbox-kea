# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
import logging
from typing import Any

import requests
from django.contrib import messages
from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from netbox.views import generic

from .. import forms
from ..constants import Family
from ..kea import KeaException
from ..models import Server
from ..utilities import (
    kea_error_hint,
)
from ._base import _KeaChangeMixin
from .server import _STATUS_TAB

logger = logging.getLogger(__name__)


class _BaseServerDHCPEnableView(_KeaChangeMixin, generic.ObjectView):
    """Confirmation view to re-enable a Kea DHCP service that was previously disabled."""

    queryset = Server.objects.all()
    dhcp_version: Family
    template_name = "netbox_kea/server_dhcp_enable.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        return {"dhcp_version": self.dhcp_version, "tab": _STATUS_TAB}

    def post(self, request: HttpRequest, pk: int, **kwargs: Any) -> HttpResponse:
        instance = self.get_object(pk=pk)
        try:
            client = instance.get_client(version=self.dhcp_version)
            client.dhcp_enable(self.dhcp_version)
            messages.success(request, f"DHCPv{self.dhcp_version} service re-enabled on {instance}.")
        except KeaException as exc:
            messages.error(request, f"Failed to enable DHCPv{self.dhcp_version}: {kea_error_hint(exc)}")
        except (requests.RequestException, RuntimeError, ValueError):
            logger.exception("Unexpected error enabling DHCPv%s on server %s", self.dhcp_version, pk)
            messages.error(request, "An internal error occurred.")
        return redirect(reverse("plugins:netbox_kea:server_status", args=[pk]))


class ServerDHCP4EnableView(_BaseServerDHCPEnableView):
    """Re-enable DHCPv4 processing."""

    dhcp_version = 4


class ServerDHCP6EnableView(_BaseServerDHCPEnableView):
    """Re-enable DHCPv6 processing."""

    dhcp_version = 6


class _BaseServerDHCPDisableView(_KeaChangeMixin, generic.ObjectView):
    """Confirmation form to temporarily disable a Kea DHCP service."""

    queryset = Server.objects.all()
    dhcp_version: Family
    template_name = "netbox_kea/server_dhcp_disable.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        form = forms.DHCPDisableForm(request.POST or None)
        return {"dhcp_version": self.dhcp_version, "form": form, "tab": _STATUS_TAB}

    def post(self, request: HttpRequest, pk: int, **kwargs: Any) -> HttpResponse:
        instance = self.get_object(pk=pk)
        form = forms.DHCPDisableForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                self.get_extra_context(request, instance) | {"object": instance},
            )
        max_period = form.cleaned_data.get("max_period")
        try:
            client = instance.get_client(version=self.dhcp_version)
            client.dhcp_disable(self.dhcp_version, max_period=max_period)
            if max_period:
                messages.warning(
                    request,
                    f"DHCPv{self.dhcp_version} disabled on {instance} for up to {max_period}s.",
                )
            else:
                messages.warning(request, f"DHCPv{self.dhcp_version} disabled on {instance}.")
        except KeaException as exc:
            messages.error(request, f"Failed to disable DHCPv{self.dhcp_version}: {kea_error_hint(exc)}")
        except (requests.RequestException, RuntimeError, ValueError):
            logger.exception("Unexpected error disabling DHCPv%s on server %s", self.dhcp_version, pk)
            messages.error(request, "An internal error occurred.")
        return redirect(reverse("plugins:netbox_kea:server_status", args=[pk]))


class ServerDHCP4DisableView(_BaseServerDHCPDisableView):
    """Disable DHCPv4 processing."""

    dhcp_version = 4


class ServerDHCP6DisableView(_BaseServerDHCPDisableView):
    """Disable DHCPv6 processing."""

    dhcp_version = 6

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
from ..kea import KeaException
from ..models import Server
from ..utilities import (
    kea_error_hint,
)
from ._base import _KeaChangeMixin

logger = logging.getLogger(__name__)


class _BaseServerDHCPEnableView(_KeaChangeMixin, generic.ObjectView):
    """Confirmation view to re-enable a Kea DHCP service that was previously disabled."""

    queryset = Server.objects.all()
    dhcp_version: int
    template_name = "netbox_kea/server_dhcp_enable.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        return {"dhcp_version": self.dhcp_version}

    def post(self, request: HttpRequest, pk: int, **kwargs: Any) -> HttpResponse:
        instance = self.get_object(pk=pk)
        service = f"dhcp{self.dhcp_version}"
        try:
            client = instance.get_client(version=self.dhcp_version)
            client.dhcp_enable(service)
            messages.success(request, f"DHCPv{self.dhcp_version} service re-enabled on {instance}.")
        except KeaException as exc:
            messages.error(request, f"Failed to enable DHCPv{self.dhcp_version}: {kea_error_hint(exc)}")
        except (requests.RequestException, ValueError):
            logger.exception("Unexpected error enabling %s on server %s", service, pk)
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
    dhcp_version: int
    template_name = "netbox_kea/server_dhcp_disable.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        form = forms.DHCPDisableForm(request.POST or None)
        return {"dhcp_version": self.dhcp_version, "form": form}

    def post(self, request: HttpRequest, pk: int, **kwargs: Any) -> HttpResponse:
        instance = self.get_object(pk=pk)
        form = forms.DHCPDisableForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                self.get_extra_context(request, instance) | {"object": instance},
            )
        service = f"dhcp{self.dhcp_version}"
        max_period = form.cleaned_data.get("max_period")
        try:
            client = instance.get_client(version=self.dhcp_version)
            client.dhcp_disable(service, max_period=max_period)
            if max_period:
                messages.warning(
                    request,
                    f"DHCPv{self.dhcp_version} disabled on {instance} for up to {max_period}s.",
                )
            else:
                messages.warning(request, f"DHCPv{self.dhcp_version} disabled on {instance}.")
        except KeaException as exc:
            messages.error(request, f"Failed to disable DHCPv{self.dhcp_version}: {kea_error_hint(exc)}")
        except (requests.RequestException, ValueError):
            logger.exception("Unexpected error disabling %s on server %s", service, pk)
            messages.error(request, "An internal error occurred.")
        return redirect(reverse("plugins:netbox_kea:server_status", args=[pk]))


class ServerDHCP4DisableView(_BaseServerDHCPDisableView):
    """Disable DHCPv4 processing."""

    dhcp_version = 4


class ServerDHCP6DisableView(_BaseServerDHCPDisableView):
    """Disable DHCPv6 processing."""

    dhcp_version = 6

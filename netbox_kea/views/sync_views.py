import csv
import logging
from typing import Any

import requests
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, ProgrammingError
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.http.request import HttpRequest
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View
from netaddr import AddrFormatError, IPAddress
from utilities.views import register_model_view

from .. import forms
from ..kea import KeaException
from ..models import Server
from ..utilities import (
    kea_error_hint,
    parse_lease_csv,
    parse_reservation_csv,
)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin
from .combined import _fetch_reservations_from_server

logger = logging.getLogger(__name__)


class _BaseSyncView(ConditionalLoginRequiredMixin, View):
    """POST-only HTMX endpoint that syncs a Kea lease/reservation to a NetBox IPAddress.

    Returns a small HTML badge fragment.
    Subclasses set ``_status`` to ``"active"`` (leases) or ``"reserved"``
    (reservations) and call the appropriate sync helper.
    """

    _status: str = "active"

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not (request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress")):
            return HttpResponseForbidden("You do not have permission to sync to NetBox IPAM.")

        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)

        ip_str = request.POST.get("ip_address", "").strip()
        if not ip_str:
            return HttpResponse("ip_address is required", status=400)

        try:
            IPAddress(ip_str)
        except (AddrFormatError, ValueError):
            return HttpResponse("Invalid IP address", status=400)

        data = self._fetch_live_data(server, ip_str)
        if data is None:
            return HttpResponse("Could not fetch live data from Kea.", status=400)
        try:
            nb_ip, _created = self._sync(data)
        except (ValueError, IntegrityError, ValidationError, OperationalError, ProgrammingError):
            logger.exception("Sync error for ip=%s", ip_str)
            return HttpResponse("Sync error: see server logs for details.", status=500)

        return render(
            request,
            "netbox_kea/inc/sync_badge.html",
            {"nb_ip": nb_ip},
        )

    def _fetch_live_data(self, server: "Server", ip_str: str) -> "dict | None":  # noqa: ARG002
        """Fetch live data for *ip_str* from Kea.  Subclasses override for protocol-specific lookup.

        Returns ``None`` when live fetch is not implemented or fails.
        """
        return None

    def _sync(self, data: dict):
        raise NotImplementedError


class ServerLease4SyncView(_BaseSyncView):
    """Sync a single DHCPv4 lease to a NetBox IPAddress (status=active)."""

    def _fetch_live_data(self, server: "Server", ip_str: str) -> "dict | None":
        try:
            client = server.get_client(version=4)
            lease = client.lease_get_by_ip(4, ip_str)
            return lease if lease else None
        except (KeaException, requests.RequestException, ValueError):
            logger.debug("Could not fetch live lease4 data for %s", ip_str)
            return None

    def _sync(self, data: dict):
        from ..sync import sync_lease_to_netbox

        return sync_lease_to_netbox(data)


class ServerLease6SyncView(_BaseSyncView):
    """Sync a single DHCPv6 lease to a NetBox IPAddress (status=active)."""

    def _fetch_live_data(self, server: "Server", ip_str: str) -> "dict | None":
        try:
            client = server.get_client(version=6)
            lease = client.lease_get_by_ip(6, ip_str)
            return lease if lease else None
        except (KeaException, requests.RequestException, ValueError):
            logger.debug("Could not fetch live lease6 data for %s", ip_str)
            return None

    def _sync(self, data: dict):
        from ..sync import sync_lease_to_netbox

        return sync_lease_to_netbox(data)


class ServerReservation4SyncView(_BaseSyncView):
    """Sync a DHCPv4 reservation to a NetBox IPAddress (status=reserved)."""

    def _fetch_live_data(self, server: "Server", ip_str: str) -> "dict | None":
        try:
            client = server.get_client(version=4)
            reservation = client.reservation_get_by_ip(4, ip_str)
            return reservation if reservation else None
        except (KeaException, requests.RequestException, ValueError):
            logger.debug("Could not fetch live reservation4 data for %s", ip_str)
            return None

    def _sync(self, data: dict):
        from ..sync import sync_reservation_to_netbox

        return sync_reservation_to_netbox(data)


class ServerReservation6SyncView(_BaseSyncView):
    """Sync a DHCPv6 reservation to a NetBox IPAddress (status=reserved)."""

    def _fetch_live_data(self, server: "Server", ip_str: str) -> "dict | None":
        try:
            client = server.get_client(version=6)
            reservation = client.reservation_get_by_ip(6, ip_str)
            return reservation if reservation else None
        except (KeaException, requests.RequestException, ValueError):
            logger.debug("Could not fetch live reservation6 data for %s", ip_str)
            return None

    def _sync(self, data: dict):
        from ..sync import sync_reservation_to_netbox

        return sync_reservation_to_netbox(data)


class _BaseBulkReservationSyncView(ConditionalLoginRequiredMixin, View):
    """Fetch all reservations for a server and sync them to NetBox IPAM."""

    dhcp_version: int = 4  # overridden in subclasses

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not (request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress")):
            return HttpResponseForbidden("You do not have permission to sync to NetBox IPAM.")

        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        from ..sync import sync_reservation_to_netbox

        try:
            reservations = _fetch_reservations_from_server(server, self.dhcp_version)
        except KeaException as exc:
            logger.exception("Kea error fetching reservations from %s (DHCPv%s)", server.name, self.dhcp_version)
            messages.error(request, kea_error_hint(exc))
            return HttpResponseRedirect(
                reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
            )
        except (requests.RequestException, ValueError):
            logger.exception("Failed to fetch reservations from %s (DHCPv%s)", server.name, self.dhcp_version)
            messages.error(request, "Failed to fetch reservations: see server logs for details.")
            return HttpResponseRedirect(
                reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
            )

        created = updated = errors = 0
        for res in reservations:
            if not res.get("ip-address") and not res.get("ip-addresses"):
                continue
            try:
                nb_ip, was_created = sync_reservation_to_netbox(res)
                if was_created:
                    created += 1
                elif nb_ip:
                    updated += 1
            except (ValueError, IntegrityError, ValidationError, OperationalError, ProgrammingError):
                ip_log = res.get("ip-address") or ", ".join(res.get("ip-addresses") or []) or "unknown"
                logger.exception("Failed to sync reservation %s", ip_log)
                errors += 1

        if errors:
            messages.warning(
                request,
                f"Bulk sync: {created} created, {updated} updated, {errors} errors.",
            )
        else:
            messages.success(
                request,
                f"Bulk sync complete: {created} created, {updated} updated.",
            )
        redirect_url = reverse(
            f"plugins:netbox_kea:server_reservations{self.dhcp_version}",
            args=[pk],
        )
        return HttpResponseRedirect(redirect_url)


class ServerReservation4BulkSyncView(_BaseBulkReservationSyncView):
    """Bulk sync all DHCPv4 reservations to NetBox IPAM."""

    dhcp_version = 4


class ServerReservation6BulkSyncView(_BaseBulkReservationSyncView):
    """Bulk sync all DHCPv6 reservations to NetBox IPAM."""

    dhcp_version = 6


# ─────────────────────────────────────────────────────────────────────────────
# Bulk Reservation Import (CSV → Kea)
# ─────────────────────────────────────────────────────────────────────────────


class _BaseBulkReservationImportView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Upload a CSV file and batch-insert reservations into Kea.

    Subclasses set :attr:`dhcp_version` and :attr:`form_class`.

    **GET**: render the upload form.
    **POST**: parse CSV → loop :meth:`KeaClient.reservation_add` → show summary.

    Result codes:
    - ``created``: reservation successfully added.
    - ``skipped``: Kea returned result=1 with "already exists" text (idempotent).
    - ``errors``: any other :class:`~netbox_kea.kea.KeaException` or unexpected failure.
    """

    dhcp_version: int
    form_class: type

    template_name = "netbox_kea/server_reservation_bulk_import.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the CSV upload form."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = self.form_class()
        return_url = reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
        return render(
            request,
            self.template_name,
            {
                "object": instance,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "return_url": return_url,
                "result": None,
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Parse uploaded CSV and insert reservations into Kea."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        return_url = reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
        form = self.form_class(request.POST, request.FILES)
        result = None

        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )

        csv_file = request.FILES["csv_file"]
        try:
            content = csv_file.read().decode("utf-8-sig")  # utf-8-sig strips BOM automatically
        except UnicodeDecodeError:
            form.add_error("csv_file", "File must be UTF-8 encoded.")
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )

        try:
            rows = parse_reservation_csv(content, self.dhcp_version)
        except (ValueError, csv.Error):
            logger.exception("CSV parse error in reservation bulk import")
            form.add_error("csv_file", "CSV parsing failed — check the file format and column headers.")
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )

        try:
            client = instance.get_client(version=self.dhcp_version)
        except (KeaException, requests.RequestException, ValueError):
            logger.exception("Failed to get Kea client for server %s", instance.pk)
            form.add_error(None, "Failed to connect to Kea server.")
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )
        created = 0
        skipped = 0
        error_rows: list[dict[str, Any]] = []

        for row in rows:
            try:
                client.reservation_add(f"dhcp{self.dhcp_version}", row)
                created += 1
            except KeaException as exc:  # noqa: PERF203
                text = getattr(exc, "response", {}).get("text", "") or ""
                result_code = getattr(exc, "response", {}).get("result", -1)
                if result_code == 1 and ("already exist" in text.lower() or "duplicate" in text.lower()):
                    skipped += 1
                else:
                    error_rows.append({"row": row, "error": kea_error_hint(exc)})
            except requests.RequestException:
                logger.exception("Connection error importing reservation row %s", row)
                error_rows.append({"row": row, "error": "Connection error — could not reach Kea server."})
            except ValueError:
                logger.exception("Data error importing reservation row %s", row)
                error_rows.append({"row": row, "error": "Invalid response from Kea — could not parse server reply."})
            except Exception:  # noqa: BLE001 — intentionally catch all to surface per-row errors without aborting import
                logger.exception("Unexpected error importing reservation row %s", row)
                error_rows.append({"row": row, "error": "An unexpected error occurred."})

        result = {
            "created": created,
            "skipped": skipped,
            "errors": len(error_rows),
            "error_rows": error_rows,
            "total": created + skipped + len(error_rows),
        }
        return render(
            request,
            self.template_name,
            {
                "object": instance,
                "form": self.form_class(),
                "dhcp_version": self.dhcp_version,
                "return_url": return_url,
                "result": result,
            },
        )


class ServerReservation4BulkImportView(_BaseBulkReservationImportView):
    """Bulk import DHCPv4 reservations from a CSV file."""

    dhcp_version = 4
    form_class = forms.Reservation4BulkImportForm


class ServerReservation6BulkImportView(_BaseBulkReservationImportView):
    """Bulk import DHCPv6 reservations from a CSV file."""

    dhcp_version = 6
    form_class = forms.Reservation6BulkImportForm


# ─────────────────────────────────────────────────────────────────────────────
# Bulk Lease CSV Import
# ─────────────────────────────────────────────────────────────────────────────


class _BaseBulkLeaseImportView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Upload a CSV file and batch-insert leases into Kea via ``lease_add``.

    **GET**: render the upload form.
    **POST**: parse CSV → loop :meth:`KeaClient.lease_add` → show summary.
    """

    dhcp_version: int
    form_class: type

    template_name = "netbox_kea/server_lease_bulk_import.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the CSV upload form."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = self.form_class()
        return_url = reverse(f"plugins:netbox_kea:server_leases{self.dhcp_version}", args=[pk])
        return render(
            request,
            self.template_name,
            {
                "object": instance,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "return_url": return_url,
                "result": None,
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Parse uploaded CSV and create leases in Kea."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        return_url = reverse(f"plugins:netbox_kea:server_leases{self.dhcp_version}", args=[pk])
        form = self.form_class(request.POST, request.FILES)

        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )

        csv_file = request.FILES["csv_file"]
        try:
            content = csv_file.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            form.add_error("csv_file", "File must be UTF-8 encoded.")
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )

        try:
            rows = parse_lease_csv(self.dhcp_version, content)
        except (ValueError, csv.Error):
            logger.exception("CSV parse error in lease bulk import")
            form.add_error("csv_file", "CSV parsing failed — check the file format and column headers.")
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )

        try:
            client = instance.get_client(version=self.dhcp_version)
        except (KeaException, requests.RequestException, ValueError):
            logger.exception("Failed to get Kea client for server %s", instance.pk)
            form.add_error(None, "Failed to connect to Kea server.")
            return render(
                request,
                self.template_name,
                {
                    "object": instance,
                    "form": form,
                    "dhcp_version": self.dhcp_version,
                    "return_url": return_url,
                    "result": None,
                },
            )
        created = 0
        error_rows: list[dict[str, Any]] = []

        for row in rows:
            try:
                client.lease_add(self.dhcp_version, row)
                created += 1
            except KeaException as exc:  # noqa: PERF203
                error_rows.append({"row": row, "error": kea_error_hint(exc)})
            except requests.RequestException:
                logger.exception("Connection error importing lease row %s", row)
                error_rows.append({"row": row, "error": "Connection error — could not reach Kea server."})
            except ValueError:
                logger.exception("Data error importing lease row %s", row)
                error_rows.append({"row": row, "error": "Invalid response from Kea — could not parse server reply."})

        result = {
            "created": created,
            "errors": len(error_rows),
            "error_rows": error_rows,
            "total": created + len(error_rows),
        }
        return render(
            request,
            self.template_name,
            {
                "object": instance,
                "form": self.form_class(),
                "dhcp_version": self.dhcp_version,
                "return_url": return_url,
                "result": result,
            },
        )


@register_model_view(Server, "lease4_bulk_import")
class ServerLease4BulkImportView(_BaseBulkLeaseImportView):
    """Bulk import DHCPv4 leases from a CSV file."""

    dhcp_version = 4
    form_class = forms.Lease4BulkImportForm


@register_model_view(Server, "lease6_bulk_import")
class ServerLease6BulkImportView(_BaseBulkLeaseImportView):
    """Bulk import DHCPv6 leases from a CSV file."""

    dhcp_version = 6
    form_class = forms.Lease6BulkImportForm

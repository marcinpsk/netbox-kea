import csv
import logging
from typing import Any

import requests
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, OperationalError, ProgrammingError
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
from ..reservation_transfer import (
    ReservationTransferDiagnostic,
    ReservationTransferError,
    parse_reservation_document,
    resolve_import_proposal,
)
from ..subnet_catalogue import MutationScope
from ..utilities import (
    kea_error_hint,
    parse_lease_csv,
)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin
from .reservation_mutations import _confirmed_side_effects, _identity_from_request, _load_target

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
            nb_ip, _created, _changed = self._sync(data)
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
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Failed to fetch live lease4 data for %s", ip_str)
            return None

    def _sync(self, data: dict):
        from ..sync import sync_lease_to_netbox

        # Per-row Sync button = explicit user intent → override foreign-IP guard.
        return sync_lease_to_netbox(data, force=True)


class ServerLease6SyncView(_BaseSyncView):
    """Sync a single DHCPv6 lease to a NetBox IPAddress (status=active)."""

    def _fetch_live_data(self, server: "Server", ip_str: str) -> "dict | None":
        try:
            client = server.get_client(version=6)
            lease = client.lease_get_by_ip(6, ip_str)
            return lease if lease else None
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Failed to fetch live lease6 data for %s", ip_str)
            return None

    def _sync(self, data: dict):
        from ..sync import sync_lease_to_netbox

        # Per-row Sync button = explicit user intent → override foreign-IP guard.
        return sync_lease_to_netbox(data, force=True)


class _BaseReservationSyncView(ConditionalLoginRequiredMixin, View):
    """Synchronize one exact typed Reservation and all its allocation addresses."""

    dhcp_version: int

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        if not (request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress")):
            return HttpResponseForbidden("You do not have permission to sync to NetBox IPAM.")
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        try:
            reservation, _catalogue = _load_target(server, self.dhcp_version, subnet_id, identity)
            from ..sync import sync_reservation_to_netbox

            result = sync_reservation_to_netbox(reservation, cleanup=False, force=True)
        except (KeaException, requests.RequestException, DatabaseError, RuntimeError, ValueError):
            logger.exception("Could not synchronize a DHCPv%s Reservation", self.dhcp_version)
            return HttpResponse("Reservation synchronization failed. See server logs.", status=500)
        return render(request, "netbox_kea/inc/reservation_sync_badge.html", {"state": result.state})


class ServerReservation4SyncView(_BaseReservationSyncView):
    """Synchronize one canonical DHCPv4 Reservation target."""

    dhcp_version = 4


class ServerReservation6SyncView(_BaseReservationSyncView):
    """Synchronize one canonical DHCPv6 Reservation target."""

    dhcp_version = 6


class _BaseBulkReservationSyncView(ConditionalLoginRequiredMixin, View):
    """Fetch one full typed Snapshot and synchronize every valid Reservation."""

    dhcp_version: int = 4  # overridden in subclasses

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not (request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress")):
            return HttpResponseForbidden("You do not have permission to sync to NetBox IPAM.")

        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        from ..sync import cleanup_stale_ips_batch, sync_reservation_to_netbox

        try:
            from ..subnet_catalogue import for_synchronization

            client = server.get_client(version=self.dhcp_version)
            catalogue = for_synchronization(server, self.dhcp_version)
            snapshot = client.reservation_snapshot(self.dhcp_version, catalogue)
        except KeaException as exc:
            logger.exception("Kea error fetching reservations from %s (DHCPv%s)", server.name, self.dhcp_version)
            messages.error(request, kea_error_hint(exc))
            return HttpResponseRedirect(
                reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
            )
        except (requests.RequestException, RuntimeError, ValueError, TypeError):
            logger.exception("Failed to fetch reservations from %s (DHCPv%s)", server.name, self.dhcp_version)
            messages.error(request, "Failed to fetch reservations: see server logs for details.")
            return HttpResponseRedirect(
                reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[pk])
            )

        created = updated = errors = skipped = 0
        synced_records = []
        # Foreign (manually-curated) NetBox IPs skipped to avoid overwriting them.
        conflicts: list[str] = []
        for reservation in snapshot.records:
            if reservation.scope.kind == "global" or not reservation.addresses:
                skipped += 1
                continue
            try:
                sync_result = sync_reservation_to_netbox(reservation, cleanup=False, conflicts=conflicts)
                synced_records.append(reservation)
                created += sync_result.created
                updated += sync_result.changed
            except (ValueError, ValidationError, DatabaseError):
                logger.exception(
                    "Failed to sync Reservation %s in DHCPv%s Subnet %s",
                    reservation.identity.identifier_type,
                    reservation.family,
                    reservation.scope.subnet.subnet_id,
                )
                errors += 1
        conflicts_skipped = len(conflicts)

        # Run stale-IP cleanup once per hostname with the full keep-set
        # to prevent false positives when multiple records share a hostname.
        # Skip cleanup when errors occurred — the keep-set is incomplete.
        stale_cleaned = 0
        if snapshot.complete and not errors:
            stale_cleaned = cleanup_stale_ips_batch(synced_records)

        stale_msg = f", {stale_cleaned} stale cleaned" if stale_cleaned else ""
        conflict_msg = f", {conflicts_skipped} conflicts skipped" if conflicts_skipped else ""
        incomplete_count = len(snapshot.diagnostics)
        skip_msg = f", {skipped} not applicable" if skipped else ""
        incomplete_msg = f", {incomplete_count} quarantined" if incomplete_count else ""
        if errors or incomplete_count:
            messages.warning(
                request,
                f"Bulk sync: {created} created, {updated} updated, {errors} errors"
                f"{skip_msg}{incomplete_msg}{conflict_msg}{stale_msg}.",
            )
        elif conflicts_skipped:
            messages.warning(
                request,
                f"Bulk sync complete: {created} created, {updated} updated, "
                f"{conflicts_skipped} conflicts skipped{skip_msg}{stale_msg}.",
            )
        else:
            messages.success(
                request,
                f"Bulk sync complete: {created} created, {updated} updated{skip_msg}{stale_msg}.",
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


class ReservationCheckNetboxIPView(ConditionalLoginRequiredMixin, View):
    """Advisory GET endpoint: report whether *ip* already exists in NetBox IPAM.

    Used by the reservation **Add** form to warn (without blocking) when the IP
    the user is entering already exists in NetBox — especially when it is a
    *foreign* (manually-curated) entry that a sync would overwrite.

    Server-scoped via ``pk`` so the existing ``Server`` view permission applies.
    Returns an empty body when the ``ip`` query param is missing/invalid or the
    IP is not present in NetBox; otherwise renders an advisory HTML fragment.
    """

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Look up *ip* in NetBox IPAM and return an advisory fragment (or empty body)."""
        # Scope to a viewable server so anonymous/unauthorised probes can't
        # enumerate NetBox IPAM through this endpoint.
        get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)

        raw_ip = (request.GET.get("ip") or "").strip()
        if not raw_ip:
            return HttpResponse("")
        try:
            # Canonicalize before the DB lookup: non-canonical forms (especially
            # IPv6 case/zero-compression variants) would otherwise miss the stored
            # canonical record and suppress the conflict advisory.
            ip_str = str(IPAddress(raw_ip))
        except (AddrFormatError, ValueError):
            return HttpResponse("")

        from ipam.models import IPAddress as NbIP

        from ..sync import is_kea_managed_ip

        # Scope the lookup to IPs this user may view so the advisory never leaks an
        # IP's status/description/assignment to someone without IPAM access. Mirrors
        # get_netbox_ip()'s host match but adds NetBox object-level permission filtering.
        nb_ip = NbIP.objects.restrict(request.user, "view").filter(address__startswith=f"{ip_str}/").first()
        if nb_ip is None:
            return HttpResponse("")

        return render(
            request,
            "netbox_kea/inc/reservation_ip_check.html",
            {"nb_ip": nb_ip, "kea_managed": is_kea_managed_ip(nb_ip)},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bulk Reservation Import (YAML or JSON to Kea)
# ─────────────────────────────────────────────────────────────────────────────


class _BaseBulkReservationImportView(_KeaChangeMixin, ConditionalLoginRequiredMixin, View):
    """Validate one document, then create typed Reservations until the first failure."""

    dhcp_version: int
    form_class: type

    template_name = "netbox_kea/server_reservation_bulk_import.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the Reservation document import form."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        return self._render(request, instance, self.form_class(), None)

    def _render(self, request: HttpRequest, instance: Server, form: Any, result: dict[str, Any] | None) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "object": instance,
                "form": form,
                "dhcp_version": self.dhcp_version,
                "return_url": reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[instance.pk]),
                "result": result,
            },
        )

    @staticmethod
    def _diagnostic_result(
        diagnostics: list[ReservationTransferDiagnostic] | tuple[ReservationTransferDiagnostic, ...],
    ):
        return {
            "created": 0,
            "failed": 0,
            "not_attempted": 0,
            "total": 0,
            "diagnostics": diagnostics,
            "failure": None,
        }

    def _resolve_reservations(self, proposals, capabilities, mutation_scope):
        diagnostics = []
        reservations = []
        for index, proposal in enumerate(proposals):
            if proposal.identity.identifier_type not in capabilities.identifiers:
                diagnostics.append(
                    ReservationTransferDiagnostic(
                        code="unavailable-identity",
                        message="The live Kea server does not support this Reservation Identity type.",
                        source_position=f"reservations[{index}].identity.type",
                    )
                )
                continue
            subnet = mutation_scope.find_by_cidr(proposal.subnet_cidr)
            if subnet is None:
                diagnostics.append(
                    ReservationTransferDiagnostic(
                        code="unknown-subnet",
                        message="The live Subnet Catalogue does not contain this CIDR.",
                        source_position=f"reservations[{index}].scope.subnet.cidr",
                    )
                )
                continue
            reservations.append(resolve_import_proposal(proposal, subnet.identity))
        return reservations, diagnostics

    @staticmethod
    def _create_reservations(request, instance, client, catalogue, reservations):
        created = 0
        failure = None
        for index, reservation in enumerate(reservations):
            try:
                mutation_result = client.reservation_create(reservation, catalogue)
            except KeaException as exc:  # noqa: PERF203
                logger.exception("Kea rejected Reservation document entry %s", index)
                failure = {"position": f"reservations[{index}]", "message": kea_error_hint(exc)}
                break
            except (requests.RequestException, RuntimeError, ValueError):  # noqa: PERF203
                logger.exception("Reservation document entry %s failed", index)
                failure = {
                    "position": f"reservations[{index}]",
                    "message": "The Reservation could not be created. See server logs.",
                }
                break
            _confirmed_side_effects(request, instance, "created", mutation_result)
            created += 1
        return created, failure

    def _execute_import(self, request, instance, proposals):
        client = instance.get_client(version=self.dhcp_version)
        capabilities = client.reservation_capabilities(self.dhcp_version)
        if not capabilities.mutation_available:
            raise RuntimeError("Reservation mutation commands are unavailable.")
        with MutationScope(instance, self.dhcp_version) as mutation_scope:
            catalogue = mutation_scope.snapshot
            if catalogue is None:
                raise RuntimeError("The Subnet Catalogue is unavailable.")
            reservations, diagnostics = self._resolve_reservations(proposals, capabilities, mutation_scope)
            if diagnostics:
                return 0, None, diagnostics
            created, failure = self._create_reservations(
                request,
                instance,
                client,
                catalogue,
                reservations,
            )
        return created, failure, diagnostics

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate the complete document before issuing any Kea mutation."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = self.form_class(request.POST, request.FILES)
        if not form.is_valid():
            return self._render(request, instance, form, None)

        try:
            parsed = parse_reservation_document(
                form.cleaned_data["document"],
                form.cleaned_data["format"],
                expected_family=self.dhcp_version,
            )
        except ReservationTransferError:
            logger.exception("Reservation transfer document parsing failed")
            form.add_error("document", "The document does not use valid syntax for the selected format.")
            return self._render(request, instance, form, None)
        diagnostics = [*parsed.diagnostics]
        if diagnostics:
            return self._render(request, instance, form, self._diagnostic_result(diagnostics))

        try:
            created, failure, diagnostics = self._execute_import(request, instance, parsed.proposals)
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Could not prepare Reservation document import for server %s", instance.pk)
            form.add_error(None, "The live Kea server could not validate this import. See server logs.")
            return self._render(request, instance, form, None)
        if diagnostics:
            return self._render(request, instance, form, self._diagnostic_result(diagnostics))

        failed = 1 if failure is not None else 0
        result = {
            "created": created,
            "failed": failed,
            "not_attempted": len(parsed.proposals) - created - failed,
            "total": len(parsed.proposals),
            "diagnostics": (),
            "failure": failure,
        }
        return self._render(request, instance, self.form_class(), result)


class ServerReservation4BulkImportView(_BaseBulkReservationImportView):
    """Bulk import DHCPv4 Reservations from a YAML or JSON document."""

    dhcp_version = 4
    form_class = forms.Reservation4ImportForm


class ServerReservation6BulkImportView(_BaseBulkReservationImportView):
    """Bulk import DHCPv6 Reservations from a YAML or JSON document."""

    dhcp_version = 6
    form_class = forms.Reservation6ImportForm


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
            except Exception:  # noqa: BLE001 — intentionally catch all to surface per-row errors without aborting import
                logger.exception("Unexpected error importing lease row %s", row)
                error_rows.append({"row": row, "error": "An unexpected error occurred."})

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

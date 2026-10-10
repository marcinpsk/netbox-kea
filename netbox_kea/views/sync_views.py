# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
import csv
import logging
from datetime import datetime, timezone
from typing import Any

import requests
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import Http404, HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.http.request import HttpRequest
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View
from netaddr import AddrFormatError, IPAddress

from .. import event_scope, forms
from ..constants import Family
from ..ipam_reconciliation import (
    RESERVATION,
    ReservationPhase,
    claim_permissions,
    reconcile,
    reconcile_permissions,
)
from ..kea import KeaException
from ..leases import (
    ExactLeaseResult,
    LeaseFound,
    LeaseIdentity,
    LeaseLookupFailed,
    is_current,
    parse_selection,
    shown_lease,
)
from ..models import Server
from ..reservation_transfer import (
    ReservationTransferDiagnostic,
    ReservationTransferError,
    parse_reservation_document,
    resolve_import_proposal,
)
from ..signals import lease_added
from ..subnet_catalogue import CatalogueUnavailable, MutationScope, for_synchronization
from ..sync_permissions import sync_gate
from ..utilities import (
    LeaseCSVError,
    kea_error_hint,
    parse_lease_csv,
)
from ._base import ConditionalLoginRequiredMixin, _KeaChangeMixin
from .leases import _LEASES_TAB, _add_lease_journal, lease_sync_gates
from .reservation_mutations import _confirmed_side_effects, _identity_from_request, _reservation_target_scope
from .reservations import _RESERVATIONS_TAB

logger = logging.getLogger(__name__)


def _row_error(request: HttpRequest, message: str) -> HttpResponse:
    """Return *message* as the error badge that replaces the row button which posted *request*."""
    # A 200 reply: htmx swaps no error reply, and NetBox's DEBUG htmx script replaces the page with a 4xx/5xx one.
    return render(request, "netbox_kea/inc/row_action_error.html", {"message": message})


class _BaseSyncView(ConditionalLoginRequiredMixin, View):
    """Claim one fresh Current Lease: an address as an IP Address, a delegated prefix as a Prefix."""

    dhcp_version: Family

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        selected = request.POST.get("ip_address", "").strip()
        if not selected:
            return _row_error(request, "ip_address is required")
        try:
            label, identity = parse_selection(self.dhcp_version, selected)
        except ValueError:
            return _row_error(request, "Invalid lease address or delegated prefix")

        gate = lease_sync_gates(request.user)[identity.kind]
        if not gate.allowed:
            return _row_error(request, gate.reason)

        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)

        observed = self._fetch_live_data(server, identity)
        if isinstance(observed, LeaseLookupFailed):
            logger.warning("Kea returned a malformed lease%s for %s", self.dhcp_version, label)
            return _row_error(request, "Sync error: see server logs for details.")
        if not isinstance(observed, LeaseFound):
            return _row_error(request, "Could not fetch live data from Kea.")
        lease = observed.lease
        # The label holds the kind and the prefix length, so a changed allocation does not match.
        if shown_lease(lease).label != label:
            return _row_error(request, "The lease changed in Kea. Reload the lease list.")
        if not is_current(lease, datetime.now(tz=timezone.utc)):
            return _row_error(request, "The lease is not current in Kea, so it was not synchronized.")
        try:
            from ..ipam_reconciliation import claim

            result = claim(server, self.dhcp_version, [lease], force=True)
            outcome: str
            if identity.kind == "delegated-prefix":
                outcome = next(iter(result.prefixes.values())).outcome
            else:
                outcome = next(iter(result.addresses.values())).outcome
            if outcome == "error":
                return _row_error(request, "Sync error: see server logs for details.")
        except event_scope.EventDispatchError:
            raise
        except (RuntimeError, ValueError, ValidationError, DatabaseError):
            logger.exception("Sync error for lease %s", label)
            return _row_error(request, "Sync error: see server logs for details.")

        return render(
            request,
            "netbox_kea/inc/claim_results.html",
            {"claim_result": result},
        )

    def _fetch_live_data(self, server: "Server", identity: LeaseIdentity) -> ExactLeaseResult | None:
        """Read the live Lease with *identity* from Kea, or ``None`` when the read fails."""
        try:
            client = server.get_client(version=self.dhcp_version)
            return client.lease_get(identity)
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Failed to fetch live lease%s data for %s", self.dhcp_version, identity.address)
            return None


class ServerLease4SyncView(_BaseSyncView):
    """Claim a DHCPv4 lease in the Server's sync VRF."""

    dhcp_version: Family = 4


class ServerLease6SyncView(_BaseSyncView):
    """Claim a DHCPv6 lease in the Server's sync VRF."""

    dhcp_version: Family = 6


class _BaseReservationSyncView(ConditionalLoginRequiredMixin, View):
    """Synchronize one exact typed Reservation and all its allocation addresses."""

    dhcp_version: Family

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        gate = sync_gate(request.user, claim_permissions(RESERVATION))
        if not gate.allowed:
            return _row_error(request, gate.reason)
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        try:
            with _reservation_target_scope(server, self.dhcp_version, subnet_id, identity) as (
                reservation,
                _client,
                catalogue,
            ):
                from ..ipam_reconciliation import claim
                from ..sync import reservation_synchronization_state

                result = claim(server, self.dhcp_version, [reservation], force=True, catalogue=catalogue)
                state = reservation_synchronization_state(
                    reservation, synchronized_addresses=result.synchronized_addresses
                )
        except event_scope.EventDispatchError:
            raise
        except Http404:
            return _row_error(request, "The Reservation or its Subnet is no longer in Kea. Reload the list.")
        except KeaException as exc:
            logger.exception("Kea error synchronizing a DHCPv%s Reservation", self.dhcp_version)
            return _row_error(request, f"Reservation synchronization failed: {kea_error_hint(exc)}")
        except (requests.RequestException, DatabaseError, RuntimeError, ValidationError, ValueError):
            logger.exception("Could not synchronize a DHCPv%s Reservation", self.dhcp_version)
            return _row_error(request, "Reservation synchronization failed. See server logs.")
        return render(
            request,
            "netbox_kea/inc/claim_results.html",
            {
                "claim_result": result,
                "record": {
                    "sync_state": state,
                    "netbox_ip_url": result.primary.get_absolute_url() if result.primary else "",
                    "sync_url": None,
                },
            },
        )


class ServerReservation4SyncView(_BaseReservationSyncView):
    """Synchronize one canonical DHCPv4 Reservation target."""

    dhcp_version = 4


class ServerReservation6SyncView(_BaseReservationSyncView):
    """Synchronize one canonical DHCPv6 Reservation target."""

    dhcp_version = 6


class _BaseBulkReservationSyncView(ConditionalLoginRequiredMixin, View):
    """Fetch one full typed Snapshot and synchronize every valid Reservation."""

    dhcp_version: Family = 4  # overridden in subclasses

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        gate = sync_gate(request.user, reconcile_permissions(server, ReservationPhase.source))
        if not gate.allowed:
            return HttpResponseForbidden(gate.reason)

        catalogue = None
        try:
            catalogue = for_synchronization(server, self.dhcp_version)
        except CatalogueUnavailable:
            logger.exception("Could not read the Subnet Catalogue for server %s", server.pk)

        report = reconcile(server, self.dhcp_version, [ReservationPhase(catalogue)])
        summary = (
            f"Bulk sync: {report.created} created, {report.updated} updated, "
            f"{len(report.conflicts)} conflicts skipped, {report.cleaned} stale links cleaned, "
            f"{report.errors} errors."
        )
        if report.skipped_reservations:
            summary += f" {len(report.skipped_reservations)} not applicable."
        if report.quarantined_reservations:
            summary += f" {report.quarantined_reservations} quarantined."
        if report.reservation_traversal_truncated:
            summary += " Reservation list was read only in part."
        if report.disagreements:
            summary += f" {len(report.disagreements)} owner disagreements."
        if not report.complete:
            summary += " Reservation phase incomplete; cleanup skipped."
        if not report.complete or report.conflicts or report.disagreements:
            messages.warning(request, summary)
        else:
            messages.success(request, summary)
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
        server = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)

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
        visible = NbIP.objects.restrict(request.user, "view")
        nb_ip = visible.filter(address__net_host=ip_str, vrf_id=server.sync_vrf_id).first()
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

    dhcp_version: Family
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
                "tab": _RESERVATIONS_TAB,
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
            except KeaException as exc:
                logger.exception("Kea rejected Reservation document entry %s", index)
                failure = {"position": f"reservations[{index}]", "message": kea_error_hint(exc)}
                break
            except (requests.RequestException, RuntimeError, ValueError):
                logger.exception("Reservation document entry %s failed", index)
                failure = {
                    "position": f"reservations[{index}]",
                    "message": "The Reservation could not be created. See server logs.",
                }
                break
            created += 1
            try:
                _confirmed_side_effects(request, instance, "created", mutation_result, catalogue)
            except (ValidationError, ValueError, RuntimeError, requests.RequestException):
                logger.exception("Side effects failed for created Reservation document entry %s", index)
                failure = {
                    "position": f"reservations[{index}]",
                    "message": "The Reservation was created, but a follow-up action failed. See server logs.",
                    "applied": True,
                }
                break
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

        failed = 1 if failure is not None and not failure.get("applied", False) else 0
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
    """Upload a CSV file and create one Lease for each row via ``lease_add``.

    **GET**: render the upload form.
    **POST**: parse each row into a typed creation request → loop :meth:`KeaClient.lease_add` → show summary.
    A row that is not a valid request rejects the whole file before Kea sees any row.
    """

    dhcp_version: Family
    form_class: type

    template_name = "netbox_kea/server_lease_bulk_import.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render the CSV upload form."""
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
                "return_url": reverse(f"plugins:netbox_kea:server_leases{self.dhcp_version}", args=[instance.pk]),
                "result": result,
                "tab": _LEASES_TAB,
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Parse uploaded CSV and create leases in Kea."""
        instance = get_object_or_404(Server.objects.restrict(request.user, "view"), pk=pk)
        form = self.form_class(request.POST, request.FILES)

        if not form.is_valid():
            return self._render(request, instance, form, None)

        csv_file = form.cleaned_data["csv_file"]
        try:
            content = csv_file.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            form.add_error("csv_file", "File must be UTF-8 encoded.")
            return self._render(request, instance, form, None)

        try:
            rows = parse_lease_csv(self.dhcp_version, content)
        except LeaseCSVError as exc:
            # The message names the file line and the column, never a value of the file.
            logger.info("Refused a lease CSV import: %s", exc)
            form.add_error("csv_file", str(exc))
            return self._render(request, instance, form, None)
        except (ValueError, csv.Error):
            logger.exception("CSV parse error in lease bulk import")
            form.add_error("csv_file", "CSV parsing failed. Check the file format and column headers.")
            return self._render(request, instance, form, None)

        try:
            client = instance.get_client(version=self.dhcp_version)
        except ValueError:
            logger.exception("Failed to get Kea client for server %s", instance.pk)
            form.add_error(None, "Failed to connect to Kea server.")
            return self._render(request, instance, form, None)
        created: list[str] = []
        error_rows: list[dict[str, Any]] = []

        for line, creation in rows:
            failed = {"line": line, "address": str(creation.address)}
            try:
                client.lease_add(creation)
            except KeaException as exc:
                error_rows.append({**failed, "error": kea_error_hint(exc)})
            except requests.RequestException:
                logger.exception("Connection error importing lease CSV line %s", line)
                error_rows.append({**failed, "error": "Connection error: could not reach the Kea server."})
            except (RuntimeError, ValueError):
                logger.exception("Data error importing lease CSV line %s", line)
                error_rows.append({**failed, "error": "Invalid response from Kea: could not parse the server reply."})
            else:
                created.append(str(creation.address))
                # A bulk import reads nothing back, so no observed Lease goes with the confirmed request.
                lease_added.send_robust(
                    sender=None,
                    server=instance,
                    creation=creation,
                    lease=None,
                    dhcp_version=self.dhcp_version,
                    request=request,
                )

        if created:
            try:
                _add_lease_journal(instance, request.user, "added", created)
            except DatabaseError:
                logger.exception("Failed to record the lease import journal for server %s", instance.pk)
        result = {
            "created": len(created),
            "errors": len(error_rows),
            "error_rows": error_rows,
            "total": len(created) + len(error_rows),
        }
        return self._render(request, instance, self.form_class(), result)


class ServerLease4BulkImportView(_BaseBulkLeaseImportView):
    """Bulk import DHCPv4 leases from a CSV file."""

    dhcp_version = 4
    form_class = forms.Lease4BulkImportForm


class ServerLease6BulkImportView(_BaseBulkLeaseImportView):
    """Bulk import DHCPv6 leases from a CSV file."""

    dhcp_version = 6
    form_class = forms.Lease6BulkImportForm

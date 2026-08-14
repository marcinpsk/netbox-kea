# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Typed Reservation list, filtering, export, and presentation helpers."""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Literal
from urllib.parse import urlencode

import requests
from django.contrib import messages
from django.core.exceptions import BadRequest
from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from netbox.views import generic
from utilities.views import register_model_view

from .. import constants, forms, tables
from ..kea import KeaClient, KeaException, LeaseQueryGuardError
from ..models import Server
from ..reservation_transfer import export_reservation_document
from ..reservations import (
    InSubnetReservationScope,
    Reservation,
    ReservationIdentity,
    ReservationSnapshot,
    ReservationSynchronizationState,
)
from ..subnet_catalogue import display as subnet_catalogue
from ..utilities import OptionalViewTab

logger = logging.getLogger(__name__)

_LEASE_IDENTIFIER_KEYS: dict[int, tuple[str, ...]] = {
    4: ("hw-address", "client-id"),
    6: ("duid", "hw-address"),
}
_RESERVATION_PAGE_SIZE = 100


def _build_reservation_options_formset(post_data: Any) -> tuple[Any, bool]:
    """Build the Reservation options formset and reject a truncated management form."""
    if "options-TOTAL_FORMS" in post_data:
        formset = forms.ReservationOptionsFormSet(data=post_data, prefix="options")
        return formset, formset.is_valid()
    if any(key.startswith("options-") for key in post_data):
        formset = forms.ReservationOptionsFormSet(data=post_data, prefix="options")
        formset.is_valid()
        return formset, False
    return forms.ReservationOptionsFormSet(prefix="options"), True


def _normalized_lease_identifiers(lease: dict[str, Any], version: int) -> set[tuple[str, str]]:
    """Return the supported normalized identities carried by one lease."""
    identities: set[tuple[str, str]] = set()
    for identifier_type in _LEASE_IDENTIFIER_KEYS[version]:
        value = lease.get(identifier_type)
        if not value:
            continue
        try:
            identity = ReservationIdentity(identifier_type, value)
        except ValueError:
            continue
        identities.add((identity.identifier_type, identity.value))
    return identities


def _enrich_reservations_with_lease_status(
    client: KeaClient,
    reservations: list[dict[str, Any]],
    version: int,
) -> None:
    """Add active-lease state to rows that retain their typed Reservation value."""
    scoped_rows = [row for row in reservations if isinstance(row["reservation"].scope, InSubnetReservationScope)]
    subnet_ids = {row["reservation"].scope.subnet.subnet_id for row in scoped_rows}
    if not subnet_ids:
        return

    lease_ips: dict[int, set[str]] = {}
    lease_identities: dict[int, set[tuple[str, str]]] = {}
    indeterminate: set[int] = set()
    hook_unavailable = False

    def fetch(subnet_id: int):
        with client.clone() as worker_client:
            try:
                leases = worker_client.lease_search(version, constants.BY_SUBNET_ID, subnet_id, state=0)
            except KeaException as exc:
                return None if exc.response.get("result") == 2 else False
            except (LeaseQueryGuardError, requests.RequestException, RuntimeError, ValueError):
                return False
        addresses = {lease["ip-address"] for lease in leases if isinstance(lease.get("ip-address"), str)}
        identities: set[tuple[str, str]] = set()
        for lease in leases:
            identities.update(_normalized_lease_identifiers(lease, version))
        return addresses, identities

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(subnet_ids), 10)) as executor:
            futures = {executor.submit(fetch, subnet_id): subnet_id for subnet_id in subnet_ids}
            for future in concurrent.futures.as_completed(futures):
                subnet_id = futures[future]
                result = future.result()
                if result is None:
                    hook_unavailable = True
                elif result is False:
                    indeterminate.add(subnet_id)
                else:
                    lease_ips[subnet_id], lease_identities[subnet_id] = result
    except Exception:  # noqa: BLE001
        logger.debug("Reservation lease enrichment failed", exc_info=True)
        return

    if hook_unavailable:
        return
    for row in reservations:
        reservation: Reservation = row["reservation"]
        if not isinstance(reservation.scope, InSubnetReservationScope):
            row["has_active_lease"] = None
            continue
        subnet_id = reservation.scope.subnet.subnet_id
        if subnet_id in indeterminate:
            continue
        identity_key = (reservation.identity.identifier_type, reservation.identity.value)
        address_matches = any(str(address) in lease_ips.get(subnet_id, ()) for address in reservation.addresses)
        identity_matches = reservation.identity.identifier_type in _LEASE_IDENTIFIER_KEYS[
            version
        ] and identity_key in lease_identities.get(subnet_id, ())
        row["has_active_lease"] = address_matches or identity_matches


def _filter_reservations(
    reservations: list[dict[str, Any]],
    q: str,
    subnet_id: int | None,
    version: int,
    scope: str = "",
) -> list[dict[str, Any]]:
    """Filter normalized typed Reservation rows in memory."""
    if version not in (4, 6):
        raise ValueError("Reservation family must be 4 or 6.")
    result = reservations
    if subnet_id is not None:
        result = [row for row in result if row["subnet_id"] == subnet_id]
    if scope:
        result = [row for row in result if row["scope_kind"] == scope]
    if not q:
        return result
    query = q.casefold()
    return [
        row
        for row in result
        if any(query in address.casefold() for address in row["ip_addresses"])
        or query in row["hostname"].casefold()
        or query in row["identifier"].casefold()
        or any(query in (option.name or "").casefold() or query in option.data.casefold() for option in row["options"])
    ]


def _reservation_table_record(reservation: Reservation, server: Server) -> dict[str, Any]:
    """Build one presentation row from a typed Reservation."""
    if isinstance(reservation.scope, InSubnetReservationScope):
        scope_kind = "in-subnet"
        subnet_id = reservation.scope.subnet.subnet_id
        subnet_cidr = reservation.scope.subnet.cidr
    else:
        scope_kind = "global"
        subnet_id = 0
        subnet_cidr = ""
    addresses = [str(address) for address in reservation.addresses]
    record = {
        "family": reservation.family,
        "scope_kind": scope_kind,
        "subnet_id": subnet_id,
        "subnet_cidr": subnet_cidr,
        "identifier_type": reservation.identity.identifier_type,
        "identifier": reservation.identity.value,
        "ip_address": addresses[0] if addresses else "",
        "ip_addresses": addresses,
        "extra_ips": addresses[1:],
        "prefixes": [str(prefix) for prefix in reservation.delegated_prefixes],
        "hostname": reservation.hostname,
        "options": reservation.options,
        "reservation": reservation,
        "server_name": server.name,
        "server_pk": server.pk,
    }
    if addresses:
        record["_ip_sort_key"] = int(reservation.addresses[0])
    return record


def _empty_reservation_snapshot(version: Literal[4, 6]) -> ReservationSnapshot:
    return ReservationSnapshot(family=version, records=(), diagnostics=(), complete=False, next_cursor=None)


def _fetch_reservation_page(server: Server, version: int, cursor: str | None) -> ReservationSnapshot:
    catalogue = subnet_catalogue(server, version)
    client = server.get_client(version=version)
    return client.reservation_page(version, catalogue, cursor=cursor, limit=_RESERVATION_PAGE_SIZE)


def _fetch_reservation_snapshot(server: Server, version: int) -> ReservationSnapshot:
    catalogue = subnet_catalogue(server, version)
    client = server.get_client(version=version)
    return client.reservation_snapshot(version, catalogue, page_size=_RESERVATION_PAGE_SIZE)


def _next_reservation_page_url(request: HttpRequest, cursor: str | None) -> str | None:
    if cursor is None:
        return None
    query = request.GET.copy()
    query["cursor"] = cursor
    query.pop("page", None)
    return f"{request.path}?{query.urlencode()}"


def _attach_reservation_action_urls(
    reservations: list[dict[str, Any]],
    server_pk: int,
    version: int,
    *,
    can_change: bool,
) -> None:
    """Attach canonical Scope plus Identity mutation URLs to eligible rows."""
    for row in reservations:
        row["edit_url"] = None
        row["delete_url"] = None
        if not can_change or row["scope_kind"] != "in-subnet":
            continue
        query = urlencode({"identifier_type": row["identifier_type"], "identifier": row["identifier"]})
        try:
            args = [server_pk, row["subnet_id"]]
            edit = reverse(f"plugins:netbox_kea:server_reservation{version}_edit", args=args)
            delete = reverse(f"plugins:netbox_kea:server_reservation{version}_delete", args=args)
        except NoReverseMatch:
            logger.debug("No Reservation action URL for Subnet ID %r", row["subnet_id"])
            continue
        row["edit_url"] = f"{edit}?{query}"
        row["delete_url"] = f"{delete}?{query}"


def _enrich_reservations_with_badges(
    reservations: list[dict[str, Any]],
    server: Server,
    version: int,
    can_change: bool = False,
) -> None:
    """Add active-lease and aggregate NetBox synchronization state to typed rows."""
    from ..sync import bulk_fetch_netbox_ips, is_kea_managed_ip, reservation_synchronization_state

    try:
        client = server.get_client(version=version)
        _enrich_reservations_with_lease_status(client, reservations, version)
    except (KeaException, requests.RequestException, RuntimeError, ValueError):
        logger.debug("Failed to enrich Reservation lease state for server %s", server.pk, exc_info=True)

    addresses = [str(address) for row in reservations for address in row["reservation"].addresses]
    try:
        netbox_ips = bulk_fetch_netbox_ips(addresses)
        synchronized = frozenset(address for address, ip in netbox_ips.items() if is_kea_managed_ip(ip))
    except DatabaseError:
        logger.exception("Could not read NetBox IPAM state for Reservation badges")
        netbox_ips = {}
        synchronized = None

    for row in reservations:
        reservation: Reservation = row["reservation"]
        if synchronized is None and isinstance(reservation.scope, InSubnetReservationScope) and reservation.addresses:
            state = ReservationSynchronizationState.unknown(
                len(reservation.addresses),
                "NetBox IPAM state could not be read.",
            )
        else:
            state = reservation_synchronization_state(reservation, synchronized)
        row["sync_state"] = state
        row["sync_state_label"] = state.label
        row["sync_synchronized"] = state.synchronized
        row["sync_total"] = state.total
        row["sync_reason"] = state.reason
        matched = [
            netbox_ips[str(address)]
            for address in reservation.addresses
            if synchronized is not None and str(address) in synchronized and str(address) in netbox_ips
        ]
        row["netbox_ip_url"] = matched[0].get_absolute_url() if matched else None
        row["sync_url"] = None
        if (
            can_change
            and isinstance(reservation.scope, InSubnetReservationScope)
            and reservation.addresses
            and state.label in ("Not Synchronized", "Partially Synchronized")
        ):
            query = urlencode(
                {
                    "identifier_type": reservation.identity.identifier_type,
                    "identifier": reservation.identity.value,
                }
            )
            base = reverse(
                f"plugins:netbox_kea:server_reservation{version}_sync",
                args=[server.pk, reservation.scope.subnet.subnet_id],
            )
            row["sync_url"] = f"{base}?{query}"


def _reservation_list_context(
    request: HttpRequest,
    server: Server,
    version: Literal[4, 6],
) -> dict[str, Any]:
    """Fetch and present one bounded typed Reservation page."""
    hook_available = True
    snapshot = _empty_reservation_snapshot(version)
    try:
        snapshot = _fetch_reservation_page(server, version, request.GET.get("cursor"))
    except KeaException as exc:
        if exc.response.get("result") == 2:
            hook_available = False
        else:
            logger.exception("Failed to fetch DHCPv%s Reservations", version)
            messages.error(request, "Failed to load Reservations from Kea.")
    except (requests.RequestException, RuntimeError, ValueError):
        logger.exception("Unexpected error fetching DHCPv%s Reservations", version)
        messages.error(request, "Failed to load Reservations from Kea.")

    reservations = [_reservation_table_record(record, server) for record in snapshot.records]
    search_form = forms.ReservationSearchForm(request.GET or None)
    if search_form.is_valid():
        reservations = _filter_reservations(
            reservations,
            q=search_form.cleaned_data.get("q", ""),
            subnet_id=search_form.cleaned_data.get("subnet_id"),
            version=version,
            scope=search_form.cleaned_data.get("scope", ""),
        )
    can_change = Server.objects.restrict(request.user, "change").filter(pk=server.pk).exists()
    _enrich_reservations_with_badges(reservations, server, version, can_change=can_change)
    for reservation in reservations:
        reservation["can_change"] = can_change and reservation["scope_kind"] == "in-subnet"
    _attach_reservation_action_urls(reservations, server.pk, version, can_change=can_change)

    table_class = tables.ReservationTable4 if version == 4 else tables.ReservationTable6
    table = table_class(reservations, user=request.user)
    table.configure(request)
    return {
        "table": table,
        "dhcp_version": version,
        "hook_available": hook_available,
        "search_form": search_form,
        "snapshot_complete": snapshot.complete,
        "reservation_diagnostics": snapshot.diagnostics,
        "next_page_url": _next_reservation_page_url(request, snapshot.next_cursor),
        "add_url": reverse(f"plugins:netbox_kea:server_reservation{version}_add", args=[server.pk])
        if can_change
        else None,
        "bulk_sync_url": reverse(f"plugins:netbox_kea:server_reservation{version}_bulk_sync", args=[server.pk])
        if can_change
        else None,
        "import_url": reverse(f"plugins:netbox_kea:server_reservation{version}_bulk_import", args=[server.pk])
        if can_change
        else None,
    }


def _reservation_export_response(request: HttpRequest, server: Server, version: Literal[4, 6]) -> HttpResponse:
    """Export a full typed Snapshot only when its traversal is complete."""
    format_name = request.GET.get("export")
    if format_name not in ("yaml", "json"):
        raise BadRequest("Reservation export format must be YAML or JSON.")
    try:
        snapshot = _fetch_reservation_snapshot(server, version)
    except (KeaException, requests.RequestException, RuntimeError, ValueError):
        logger.exception("Could not export DHCPv%s Reservations", version)
        return HttpResponse("The Reservation Snapshot could not be exported.", status=502)
    if not snapshot.complete:
        return HttpResponse("The Reservation Snapshot is incomplete and cannot be exported.", status=409)
    content = export_reservation_document(snapshot.records, format_name)
    response = HttpResponse(
        content,
        content_type="application/json" if format_name == "json" else "application/yaml",
    )
    response["Content-Disposition"] = f'attachment; filename="kea-dhcpv{version}-reservations.{format_name}"'
    return response


_RESERVATIONS_TAB = OptionalViewTab(
    label="Reservations", weight=1040, is_enabled=lambda server: server.dhcp4 or server.dhcp6
)


@register_model_view(Server, "reservations4")
class ServerReservations4View(generic.ObjectView):
    """Show bounded DHCPv4 Reservation Snapshots."""

    queryset = Server.objects.all()
    tab = _RESERVATIONS_TAB
    template_name = "netbox_kea/server_reservations.html"

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Redirect v6-only servers, export, or render the list."""
        instance = self.get_object(**kwargs)
        if not instance.dhcp4 and instance.dhcp6:
            return redirect(reverse("plugins:netbox_kea:server_reservations6", args=[instance.pk]))
        if "export" in request.GET:
            return _reservation_export_response(request, instance, 4)
        return super().get(request, **kwargs)

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Return the DHCPv4 typed list context."""
        return _reservation_list_context(request, instance, 4)


@register_model_view(Server, "reservations6")
class ServerReservations6View(generic.ObjectView):
    """Show bounded DHCPv6 Reservation Snapshots."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservations.html"

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Export or render the DHCPv6 list."""
        instance = self.get_object(**kwargs)
        if "export" in request.GET:
            return _reservation_export_response(request, instance, 6)
        return super().get(request, **kwargs)

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Return the DHCPv6 typed list context and shared tab."""
        return {**_reservation_list_context(request, instance, 6), "tab": _RESERVATIONS_TAB}

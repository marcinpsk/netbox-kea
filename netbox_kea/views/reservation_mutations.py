# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
import ipaddress
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Literal
from urllib.parse import urlencode

import requests
from django.contrib import messages
from django.core import signing
from django.core.exceptions import BadRequest, ValidationError
from django.db import DatabaseError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from netbox.views import generic

from .. import constants, forms, subnet_catalogue
from ..constants import Family, IPAddressValue
from ..dhcp_options import DHCPOption
from ..ipam_reconciliation import claim
from ..kea import KeaClient, KeaException
from ..models import Server
from ..pools import addresses_in_pools
from ..published_name import published_name, stored_hostname
from ..reservations import (
    ClearValue,
    FieldChange,
    InSubnetReservationScope,
    IPv4Reservation,
    IPv6Reservation,
    Reservation,
    ReservationCapabilities,
    ReservationChange,
    ReservationConflict,
    ReservationIdentity,
    ReservationMutationResult,
    SetValue,
    Unchanged,
    reservation_fingerprint,
    reservation_identifier_types,
)
from ..signals import reservation_created, reservation_deleted, reservation_updated
from ..subnet_catalogue import CatalogueSnapshot, CatalogueUnavailable, MutationScope, VerifiedSubnet
from ..utilities import kea_error_hint
from ._base import _diagnostic_messages, _KeaChangeMixin, _safe_return_url
from .reservations import _RESERVATIONS_TAB, _build_reservation_options_formset, _configured_capabilities

logger = logging.getLogger(__name__)

_FINGERPRINT_SALT = "netbox_kea.reservation-managed-facts"
FLEX_ID_DOCUMENTATION_URL = (
    "https://kea.readthedocs.io/en/latest/arm/hooks.html#flex-id-flexible-identifiers-for-host-reservations"
)


def _warn_addresses_in_pools(
    request: HttpRequest,
    subnet: VerifiedSubnet,
    addresses: tuple[IPAddressValue, ...],
) -> None:
    """Add a non-blocking warning for each address in *addresses* that falls within a pool of *subnet*."""
    if not addresses:
        return
    if subnet.configuration is None:
        messages.warning(
            request,
            f"The pool overlap check did not run. The configuration of Subnet {subnet.cidr} is unavailable, "
            "so this Reservation was not checked against its pools.",
        )
        return
    for address, pool in addresses_in_pools(addresses, subnet.configuration.pools):
        messages.warning(
            request,
            f"IP {address} is within existing pool {pool.range}. "
            "Kea allows this. Reservations take priority over pool allocation.",
        )


def _in_subnet_scope(reservation: Reservation) -> InSubnetReservationScope:
    """Return the verified Subnet scope required by mutation routes."""
    if not isinstance(reservation.scope, InSubnetReservationScope):
        raise RuntimeError("Reservation mutation requires an In-Subnet Reservation.")
    return reservation.scope


def _options_from_formset(
    options_formset: Any,
    current: tuple[DHCPOption, ...] = (),
) -> tuple[DHCPOption, ...]:
    """Apply visible form changes while preserving unexposed option facts."""
    options: list[DHCPOption] = []
    remaining = list(enumerate(current))
    for row in getattr(options_formset, "cleaned_data", []) or []:
        if not row:
            continue
        original_index = row.get("original_index")
        existing: DHCPOption | None
        if original_index is not None:
            matching_position = next(
                (
                    position
                    for position, (current_index, _option) in enumerate(remaining)
                    if original_index == current_index
                ),
                None,
            )
            if matching_position is None:
                raise ValueError("The Reservation option source does not match the current Reservation.")
            _, existing = remaining.pop(matching_position)
            if row.get("DELETE"):
                continue
            if not row.get("name"):
                continue
            displayed_name = existing.name or (str(existing.code) if existing.code is not None else "")
            if row["name"] != displayed_name:
                existing = None
        else:
            if not row.get("name") or row.get("DELETE"):
                continue
            matching_positions = [
                position
                for position, (_current_index, option) in enumerate(remaining)
                if row["name"] == (option.name or (str(option.code) if option.code is not None else ""))
            ]
            if len(matching_positions) > 1:
                raise ValueError("The Reservation option name is ambiguous without its source position.")
            existing = remaining.pop(matching_positions[0])[1] if matching_positions else None
        submitted_always_send = bool(row.get("always_send"))
        if existing is not None:
            always_send = (
                existing.always_send if submitted_always_send == bool(existing.always_send) else submitted_always_send
            )
            options.append(
                replace(
                    existing,
                    data=row["data"],
                    always_send=always_send,
                )
            )
            continue
        options.append(
            DHCPOption(
                code=None,
                name=row["name"],
                space=None,
                data=row["data"],
                csv_format=None,
                always_send=True if submitted_always_send else None,
                never_send=None,
            )
        )
    return tuple(options)


def _options_initial(reservation: Reservation) -> list[dict[str, Any]]:
    return [
        {
            "original_index": index,
            "name": option.name or (str(option.code) if option.code is not None else ""),
            "data": option.data,
            "always_send": bool(option.always_send),
        }
        for index, option in enumerate(reservation.options)
    ]


def _signed_fingerprint(reservation: Reservation, suffix: str | None) -> str:
    scope = _in_subnet_scope(reservation)
    return signing.dumps(
        {
            "family": reservation.family,
            "subnet_id": scope.subnet.subnet_id,
            "identifier_type": reservation.identity.identifier_type,
            "identifier": reservation.identity.value,
            "fingerprint": reservation_fingerprint(reservation),
            # The form shows the published name under this suffix.
            "qualifying_suffix": suffix,
        },
        salt=_FINGERPRINT_SALT,
        compress=True,
    )


def _first_address(addresses: tuple[IPAddressValue, ...]) -> IPAddressValue | None:
    """Return the address that selects the Pool suffix: the first one, as for a synchronized Reservation."""
    return addresses[0] if addresses else None


def _shown_suffix(reservation: Reservation, catalogue: CatalogueSnapshot) -> str | None:
    """Return the suffix under which the edit form shows the hostname of *reservation*; None without a hostname."""
    if not reservation.hostname:
        return None
    return catalogue.subnet_qualifying_suffix(
        _in_subnet_scope(reservation).subnet, _first_address(reservation.addresses)
    )


def _hostname_change(
    current: Reservation,
    entered: str,
    addresses: tuple[IPAddressValue, ...],
    shown_suffix: str | None,
    catalogue: CatalogueSnapshot,
) -> FieldChange[str]:
    """Return the hostname change that makes Kea publish the *entered* name at the submitted *addresses*.

    A name that publishes the same name as the current hostname leaves the stored hostname unchanged.
    """
    if not current.hostname and not entered:
        return Unchanged()
    if current.hostname and shown_suffix != _shown_suffix(current, catalogue):
        raise ReservationConflict("The DDNS qualifying suffix of the Subnet changed after the edit form was opened.")
    suffix = catalogue.subnet_qualifying_suffix(_in_subnet_scope(current).subnet, _first_address(addresses))
    stored = stored_hostname(entered, suffix)
    if published_name(stored, suffix) == published_name(current.hostname, suffix):
        return Unchanged()
    return _change(current.hostname, stored, "")


def _stored_for(
    catalogue: CatalogueSnapshot, subnet: VerifiedSubnet, name: str, addresses: tuple[IPAddressValue, ...]
) -> str:
    """Return the hostname to store so that Kea publishes *name* at *addresses* in *subnet*."""
    if not name:
        return ""
    return stored_hostname(name, catalogue.subnet_qualifying_suffix(subnet.identity, _first_address(addresses)))


def _payload_from_post(token: str, reservation: Reservation) -> dict[str, Any]:
    try:
        payload = signing.loads(token, salt=_FINGERPRINT_SALT, max_age=86_400)
    except signing.BadSignature as exc:
        raise ReservationConflict("The edit fingerprint is invalid or expired.") from exc
    scope = _in_subnet_scope(reservation)
    target = {
        "family": reservation.family,
        "subnet_id": scope.subnet.subnet_id,
        "identifier_type": reservation.identity.identifier_type,
        "identifier": reservation.identity.value,
    }
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in target.items()):
        raise ReservationConflict("The edit fingerprint does not match this Reservation.")
    fingerprint = payload.get("fingerprint")
    suffix = payload.get("qualifying_suffix")
    if not isinstance(fingerprint, str) or not fingerprint or not isinstance(suffix, str | None):
        raise ReservationConflict("The edit fingerprint is invalid.")
    if fingerprint != reservation_fingerprint(reservation):
        raise ReservationConflict("The Reservation changed after the edit form was opened.")
    return payload


def _identity_from_request(request: HttpRequest, version: Family) -> ReservationIdentity:
    types = request.GET.getlist("identifier_type")
    values = request.GET.getlist("identifier")
    if len(types) != 1 or len(values) != 1:
        raise BadRequest("Exactly one identifier_type and one identifier are required.")
    identifier_type = types[0]
    identifier = values[0]
    if identifier_type not in reservation_identifier_types(version) or not identifier:
        raise BadRequest(f"Invalid DHCPv{version} Reservation Identity.")
    try:
        return ReservationIdentity(identifier_type, identifier)
    except ValueError as exc:
        raise BadRequest(f"Invalid DHCPv{version} Reservation Identity.") from exc


def _read_reservation_target(
    server: Server,
    version: Family,
    subnet: VerifiedSubnet | None,
    catalogue: CatalogueSnapshot,
    identity: ReservationIdentity,
) -> tuple[Reservation, KeaClient, CatalogueSnapshot]:
    """Read one exact Reservation within a verified Subnet."""
    if subnet is None:
        raise Http404("Reservation Subnet not found.")
    scope = InSubnetReservationScope(subnet.identity)
    client = server.get_client(version=version)
    reservation = client.reservation_by_identity(version, catalogue, scope, identity)
    if reservation is None:
        raise Http404("Reservation not found.")
    return reservation, client, catalogue


@contextmanager
def _reservation_target_scope(
    server: Server,
    version: Family,
    subnet_id: int,
    identity: ReservationIdentity,
) -> Iterator[tuple[Reservation, KeaClient, CatalogueSnapshot]]:
    """Keep one Reservation target and its Catalogue Snapshot live through mutation."""
    with MutationScope(server, version) as mutation_scope:
        subnet = mutation_scope.find_by_id(subnet_id)
        catalogue = mutation_scope.snapshot
        if catalogue is None:
            raise RuntimeError("The Subnet Catalogue is unavailable.")
        yield _read_reservation_target(server, version, subnet, catalogue, identity)


def _load_target(
    server: Server,
    version: Family,
    subnet_id: int,
    identity: ReservationIdentity,
) -> Reservation:
    """Return one live Reservation without invalidating the display snapshots."""
    observation = subnet_catalogue.read_identity(server, version)
    subnet = observation.find_by_id(subnet_id)
    reservation, _client, _catalogue = _read_reservation_target(server, version, subnet, observation.snapshot, identity)
    return reservation


def _journal_mutation(
    server: Server,
    user: Any,
    action: str,
    reservation: Reservation,
) -> None:
    try:
        from extras.models import JournalEntry

        addresses = ", ".join(str(address) for address in reservation.addresses) or "no address"
        JournalEntry.objects.create(
            assigned_object=server,
            created_by=user,
            kind="info",
            comments=(
                f"Reservation {action}: {reservation.identity.identifier_type} "
                f"{reservation.identity.value}; {addresses}"
            ),
        )
    except (ImportError, DatabaseError, ValidationError):
        logger.exception("Could not record the confirmed Reservation mutation in the journal")


def _confirmed_side_effects(
    request: HttpRequest,
    server: Server,
    action: Literal["created", "updated", "deleted"],
    result: ReservationMutationResult,
    catalogue: CatalogueSnapshot,
    sync_to_netbox: bool = False,
) -> None:
    reservation = result.intended or result.previous
    if reservation is None:
        raise RuntimeError("A confirmed Reservation mutation requires a before or after record.")
    _journal_mutation(server, request.user, action, reservation)
    signal = {
        "created": reservation_created,
        "updated": reservation_updated,
        "deleted": reservation_deleted,
    }[action]
    signal.send_robust(
        sender=None,
        server=server,
        before=result.previous,
        after=result.intended,
        dhcp_version=reservation.family,
        request=request,
    )
    if result.persistence == "failed":
        messages.warning(
            request,
            " ".join(("Kea applied the change, but could not persist it to disk.", *result.persistence_diagnostics)),
        )
    elif result.persistence == "not-requested":
        messages.info(request, "Kea applied the change. Configuration persistence is disabled for this server.")
    if sync_to_netbox and result.intended is not None and not result.intended.addresses:
        messages.info(request, f"Reservation {action}. Nothing to sync to NetBox because it reserves no IP address.")
    elif sync_to_netbox and result.intended is not None:
        permission_checker = getattr(request.user, "has_perm", None)
        has_ipam_write_permission = (
            callable(permission_checker)
            and permission_checker("ipam.add_ipaddress")
            and permission_checker("ipam.change_ipaddress")
        )
        if not has_ipam_write_permission:
            logger.warning("User %r requested Reservation IPAM sync without IPAM write permission", request.user)
            messages.warning(
                request, f"Reservation {action}, but it was not synced to NetBox. IPAM permission is required."
            )
        else:
            try:
                outcome = claim(server, reservation.family, [result.intended], force=True, catalogue=catalogue)
                if any(not address.synchronized for address in outcome.addresses.values()):
                    messages.warning(request, "The Reservation changed, but NetBox IPAM synchronization failed.")
            except (CatalogueUnavailable, DatabaseError, ValidationError, ValueError):
                logger.exception("Could not synchronize a confirmed Reservation mutation to NetBox IPAM")
                messages.warning(request, "The Reservation changed, but NetBox IPAM synchronization failed.")
    if result.verification == "failed":
        messages.warning(request, "Kea applied the change, but NetBox Kea could not verify the final Reservation.")


def _change(current: Any, submitted: Any, empty: Any):
    if submitted == current:
        return Unchanged()
    if submitted == empty:
        return ClearValue()
    return SetValue(submitted)


def _entered_address(value: str, family: Family) -> IPAddressValue | None:
    """Return the first address of an address field, or None when the field holds none or no valid one."""
    first = value.split(",", maxsplit=1)[0].strip()
    try:
        address = ipaddress.ip_address(first)
    except ValueError:
        return None
    return address if address.version == family else None


def _published_name_preview(
    server: Server, family: Family, cidr: str, address: IPAddressValue | None, hostname: str
) -> dict[str, Any]:
    """Return the preview facts of the name that Kea publishes for *hostname* at *address* in the Subnet *cidr*."""
    catalogue = subnet_catalogue.display(server, family)
    try:
        subnet = catalogue.find_by_cidr(cidr)
    except ValueError:
        subnet = None
    if subnet is None:
        return {"subnet_known": False}
    try:
        suffix = catalogue.subnet_qualifying_suffix(subnet.identity, address)
    except CatalogueUnavailable:
        return {"subnet_known": True}
    return {
        "subnet_known": True,
        "suffix": suffix,
        "published": published_name(stored_hostname(hostname, suffix), suffix),
    }


class _ReservationPublishedNameView(_KeaChangeMixin, View):
    """Render the name that Kea publishes for the hostname in the Reservation form, from the cached catalogue."""

    dhcp_version: Family

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(Server.objects.restrict(request.user, "change"), pk=pk)
        hostname = request.GET.get("hostname", "").strip()
        context: dict[str, Any] = {"hostname": hostname}
        if hostname:
            field = "ip_address" if self.dhcp_version == 4 else "ip_addresses"
            context.update(
                _published_name_preview(
                    server,
                    self.dhcp_version,
                    request.GET.get("subnet_cidr", "").strip(),
                    _entered_address(request.GET.get(field, ""), self.dhcp_version),
                    hostname,
                )
            )
        return render(request, "netbox_kea/inc/reservation_published_name.html", context)


class _ReservationMutationView(_KeaChangeMixin, generic.ObjectView):
    queryset = Server.objects.all()
    tab = _RESERVATIONS_TAB
    template_name = "netbox_kea/server_reservation_form.html"
    dhcp_version: Family
    form_class: type[forms.Reservation4Form] | type[forms.Reservation6Form]
    form_action: str

    def _return_url(self, server: Server) -> str:
        """Return the Reservation search that linked here, else the Reservation list."""
        return _safe_return_url(
            self.request, reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[server.pk])
        )

    def _mutation_unavailable_response(
        self,
        request: HttpRequest,
        server: Server,
        capabilities: ReservationCapabilities | None,
    ) -> HttpResponse | None:
        if capabilities is not None and capabilities.mutation_available:
            return None
        messages.error(request, "Reservation mutation capabilities are unavailable.")
        return redirect(self._return_url(server))

    def _form_context(
        self,
        server: Server,
        form: Any,
        options_formset: Any,
        capabilities: ReservationCapabilities | None,
        *,
        subnet_choices: tuple[tuple[str, int], ...] = (),
        subnet_cmds_available: bool = True,
        published_name_query: str = "",
    ) -> dict[str, Any]:
        preview_url = reverse(
            f"plugins:netbox_kea:server_reservation{self.dhcp_version}_published_name", args=[server.pk]
        )
        return {
            "object": server,
            "form": form,
            "options_formset": options_formset,
            "return_url": self._return_url(server),
            "action": self.form_action,
            "dhcp_version": self.dhcp_version,
            "tab": self.tab,
            "subnet_choices": subnet_choices,
            "subnet_cmds_available": subnet_cmds_available,
            "subnet_datalist_id": constants.RESERVATION_SUBNET_DATALIST_ID,
            "reservation_capabilities": capabilities,
            "mutation_available": bool(capabilities and capabilities.mutation_available),
            "flex_id_documentation_url": FLEX_ID_DOCUMENTATION_URL,
            "published_name_url": f"{preview_url}?{published_name_query}" if published_name_query else preview_url,
        }

    def _render(
        self,
        request: HttpRequest,
        server: Server,
        form: Any,
        options_formset: Any,
        capabilities: ReservationCapabilities | None,
        **context: Any,
    ) -> HttpResponse:
        return render(
            request,
            self.template_name,
            self._form_context(server, form, options_formset, capabilities, **context),
        )


class _ReservationAddView(_ReservationMutationView):
    form_action = "Add"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        capabilities = _configured_capabilities(server, self.dhcp_version)
        snapshot = subnet_catalogue.display(server, self.dhcp_version)
        _diagnostic_messages(
            request, snapshot.diagnostics, messages.ERROR if snapshot.unavailable else messages.WARNING
        )
        initial_fields = (
            ("subnet_cidr", "ip_address", "identifier_type", "identifier", "hostname")
            if self.dhcp_version == 4
            else ("subnet_cidr", "ip_addresses", "prefixes", "identifier_type", "identifier", "hostname")
        )
        initial = {field: request.GET.get(field, "") for field in initial_fields if request.GET.get(field)}
        form = self.form_class(initial=initial, capabilities=capabilities)
        return self._render(
            request,
            server,
            form,
            forms.ReservationOptionsFormSet(prefix="options"),
            capabilities,
            subnet_choices=snapshot.subnet_choices,
            subnet_cmds_available=snapshot.subnet_cmds_available,
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        capabilities = _configured_capabilities(server, self.dhcp_version)
        unavailable_response = self._mutation_unavailable_response(request, server, capabilities)
        if unavailable_response is not None:
            return unavailable_response
        form = self.form_class(data=request.POST, capabilities=capabilities)
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        if form.is_valid() and options_valid:
            try:
                result, catalogue = self._create(
                    request, server, form.cleaned_data, _options_from_formset(options_formset)
                )
                _confirmed_side_effects(
                    request,
                    server,
                    "created",
                    result,
                    catalogue,
                    sync_to_netbox=bool(form.cleaned_data.get("sync_to_netbox")),
                )
                messages.success(request, "Reservation created.")
                return redirect(self._return_url(server))
            except KeaException as exc:
                logger.exception("Kea rejected a DHCPv%s Reservation create", self.dhcp_version)
                messages.error(request, kea_error_hint(exc))
            except (requests.RequestException, RuntimeError, ValueError):
                logger.exception("Could not create a DHCPv%s Reservation", self.dhcp_version)
                messages.error(request, "The Reservation could not be created. See server logs.")
        snapshot = subnet_catalogue.display(server, self.dhcp_version)
        _diagnostic_messages(
            request, snapshot.diagnostics, messages.ERROR if snapshot.unavailable else messages.WARNING
        )
        return self._render(
            request,
            server,
            form,
            options_formset,
            capabilities,
            subnet_choices=snapshot.subnet_choices,
            subnet_cmds_available=snapshot.subnet_cmds_available,
        )

    def _create(
        self,
        request: HttpRequest,
        server: Server,
        cleaned_data: dict[str, Any],
        options: tuple[DHCPOption, ...],
    ) -> tuple[ReservationMutationResult, CatalogueSnapshot]:
        """Create the Reservation. Return the result and the catalogue that verified its Subnet."""
        with MutationScope(server, self.dhcp_version) as mutation_scope:
            subnet = mutation_scope.find_by_cidr(cleaned_data["subnet_cidr"])
            if subnet is None:
                raise ValueError("The submitted Subnet is not present in the live Subnet Catalogue.")
            catalogue = mutation_scope.snapshot
            if catalogue is None:
                raise RuntimeError("The Subnet Catalogue is unavailable.")
            scope = InSubnetReservationScope(subnet.identity)
            identity = ReservationIdentity(cleaned_data["identifier_type"], cleaned_data["identifier"])
            hostname = cleaned_data.get("hostname", "")
            if self.dhcp_version == 4:
                ipv4_addresses = (
                    (ipaddress.IPv4Address(cleaned_data["ip_address"]),) if cleaned_data.get("ip_address") else ()
                )
                reservation: Reservation = IPv4Reservation(
                    scope=scope,
                    identity=identity,
                    addresses=ipv4_addresses,
                    hostname=_stored_for(catalogue, subnet, hostname, ipv4_addresses),
                    options=options,
                )
            else:
                ipv6_addresses = tuple(
                    ipaddress.IPv6Address(value)
                    for value in (cleaned_data.get("ip_addresses") or "").split(",")
                    if value
                )
                ipv6_prefixes = tuple(
                    ipaddress.IPv6Network(value) for value in (cleaned_data.get("prefixes") or "").split(",") if value
                )
                reservation = IPv6Reservation(
                    scope=scope,
                    identity=identity,
                    addresses=ipv6_addresses,
                    delegated_prefixes=ipv6_prefixes,
                    hostname=_stored_for(catalogue, subnet, hostname, ipv6_addresses),
                    options=options,
                )
            _warn_addresses_in_pools(request, subnet, reservation.addresses)
            client = server.get_client(version=self.dhcp_version)
            return client.reservation_create(reservation, catalogue), catalogue


class _ReservationEditView(_ReservationMutationView):
    form_action = "Edit"

    def _render(
        self,
        request: HttpRequest,
        server: Server,
        form: Any,
        options_formset: Any,
        capabilities: ReservationCapabilities | None,
        **context: Any,
    ) -> HttpResponse:
        # The Subnet field is disabled, so the preview request carries the Subnet in its URL.
        query = urlencode({"subnet_cidr": form.initial["subnet_cidr"]})
        return super()._render(
            request, server, form, options_formset, capabilities, published_name_query=query, **context
        )

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        try:
            reservation = _load_target(server, self.dhcp_version, subnet_id, identity)
            suffix = _shown_suffix(reservation, subnet_catalogue.display(server, self.dhcp_version))
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Could not load the Reservation edit target")
            messages.error(request, "The Reservation could not be loaded. See server logs.")
            return redirect(self._return_url(server))
        capabilities = _configured_capabilities(server, self.dhcp_version)
        form = self._form_for(reservation, suffix, capabilities)
        return self._render(
            request,
            server,
            form,
            forms.ReservationOptionsFormSet(initial=_options_initial(reservation), prefix="options"),
            capabilities,
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        return_url = self._return_url(server)
        capabilities = _configured_capabilities(server, self.dhcp_version)
        unavailable_response = self._mutation_unavailable_response(request, server, capabilities)
        if unavailable_response is not None:
            return unavailable_response
        try:
            with _reservation_target_scope(server, self.dhcp_version, subnet_id, identity) as (
                current,
                client,
                catalogue,
            ):
                form = self.form_class(
                    data=request.POST,
                    initial=self._initial(current, _shown_suffix(current, catalogue)),
                    capabilities=capabilities,
                )
                for field in ("subnet_cidr", "identifier_type", "identifier"):
                    form.fields[field].disabled = True
                options_formset, options_valid = _build_reservation_options_formset(request.POST)
                if form.is_valid() and options_valid:
                    try:
                        payload = _payload_from_post(form.cleaned_data["managed_fingerprint"], current)
                        change = self._change(
                            current,
                            form.cleaned_data,
                            _options_from_formset(options_formset, current.options),
                            payload["qualifying_suffix"],
                            catalogue,
                        )
                        result = client.reservation_change(current, payload["fingerprint"], change, catalogue)
                        _confirmed_side_effects(
                            request,
                            server,
                            "updated",
                            result,
                            catalogue,
                            sync_to_netbox=bool(form.cleaned_data.get("sync_to_netbox")),
                        )
                        messages.success(request, "Reservation updated.")
                        return redirect(return_url)
                    except ReservationConflict as exc:
                        form.add_error(None, f"{exc} Reload the form before you try again.")
                    except KeaException as exc:
                        logger.exception("Kea rejected a Reservation update")
                        messages.error(request, kea_error_hint(exc))
                    except (requests.RequestException, RuntimeError, ValueError):
                        logger.exception("Could not update the Reservation")
                        messages.error(request, "The Reservation could not be updated. See server logs.")
        except Http404:
            raise
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Could not reload the Reservation edit target")
            messages.error(request, "The Reservation could not be reloaded. Edit stopped.")
            return redirect(return_url)
        return self._render(request, server, form, options_formset, capabilities)

    def _initial(self, reservation: Reservation, suffix: str | None) -> dict[str, Any]:
        scope = _in_subnet_scope(reservation)
        initial = {
            "subnet_cidr": scope.subnet.cidr,
            "identifier_type": reservation.identity.identifier_type,
            "identifier": reservation.identity.value,
            "hostname": published_name(reservation.hostname, suffix or ""),
            "managed_fingerprint": _signed_fingerprint(reservation, suffix),
        }
        if reservation.family == 4:
            initial["ip_address"] = str(reservation.addresses[0]) if reservation.addresses else ""
        else:
            initial["ip_addresses"] = ",".join(str(address) for address in reservation.addresses)
            initial["prefixes"] = ",".join(str(prefix) for prefix in reservation.delegated_prefixes)
        return initial

    def _form_for(self, reservation: Reservation, suffix: str | None, capabilities: ReservationCapabilities | None):
        form = self.form_class(initial=self._initial(reservation, suffix), capabilities=capabilities)
        for field in ("subnet_cidr", "identifier_type", "identifier"):
            form.fields[field].disabled = True
        return form

    def _change(
        self,
        current: Reservation,
        cleaned_data: dict[str, Any],
        options: tuple[DHCPOption, ...],
        shown_suffix: str | None,
        catalogue: CatalogueSnapshot,
    ) -> ReservationChange:
        addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
        prefixes: tuple[ipaddress.IPv6Network, ...]
        if current.family == 4:
            addresses = (ipaddress.IPv4Address(cleaned_data["ip_address"]),) if cleaned_data.get("ip_address") else ()
            prefixes = current.delegated_prefixes
        else:
            addresses = tuple(
                ipaddress.IPv6Address(value) for value in (cleaned_data.get("ip_addresses") or "").split(",") if value
            )
            prefixes = tuple(
                ipaddress.IPv6Network(value) for value in (cleaned_data.get("prefixes") or "").split(",") if value
            )
        return ReservationChange(
            addresses=_change(current.addresses, addresses, ()),
            delegated_prefixes=_change(current.delegated_prefixes, prefixes, ()),
            hostname=_hostname_change(current, cleaned_data.get("hostname", ""), addresses, shown_suffix, catalogue),
            options=_change(current.options, options, ()),
        )


class _ReservationDeleteView(_ReservationMutationView):
    template_name = "netbox_kea/server_reservation_delete.html"
    form_action = "Delete"

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        try:
            reservation = _load_target(server, self.dhcp_version, subnet_id, identity)
        except Http404:
            raise
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Could not load the Reservation delete target")
            messages.error(request, "The Reservation could not be loaded. See server logs.")
            return redirect(self._return_url(server))
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "reservation_label": f"{reservation.identity.identifier_type} {reservation.identity.value}",
                "subnet_id": subnet_id,
                "dhcp_version": self.dhcp_version,
                "return_url": self._return_url(server),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        return_url = self._return_url(server)
        capabilities = _configured_capabilities(server, self.dhcp_version)
        unavailable_response = self._mutation_unavailable_response(request, server, capabilities)
        if unavailable_response is not None:
            return unavailable_response
        try:
            with _reservation_target_scope(server, self.dhcp_version, subnet_id, identity) as (
                reservation,
                client,
                catalogue,
            ):
                result = client.reservation_delete(reservation, catalogue)
                _confirmed_side_effects(request, server, "deleted", result, catalogue)
                messages.success(request, "Reservation deleted.")
        except ReservationConflict:
            messages.error(request, "The Reservation changed or no longer exists.")
        except KeaException as exc:
            logger.exception("Kea rejected a Reservation delete")
            messages.error(request, kea_error_hint(exc))
        except (requests.RequestException, RuntimeError, ValueError):
            logger.exception("Could not delete the Reservation")
            messages.error(request, "The Reservation could not be deleted. See server logs.")
        return redirect(return_url)


class ServerReservation4AddView(_ReservationAddView):
    """Create one typed DHCPv4 In-Subnet Reservation."""

    dhcp_version = 4
    form_class = forms.Reservation4Form


class ServerReservation6AddView(_ReservationAddView):
    """Create one typed DHCPv6 In-Subnet Reservation."""

    dhcp_version = 6
    form_class = forms.Reservation6Form


class ServerReservation4EditView(_ReservationEditView):
    """Edit one DHCPv4 Reservation by immutable Scope and Identity."""

    dhcp_version = 4
    form_class = forms.Reservation4Form


class ServerReservation6EditView(_ReservationEditView):
    """Edit one DHCPv6 Reservation by immutable Scope and Identity."""

    dhcp_version = 6
    form_class = forms.Reservation6Form


class ServerReservation4PublishedNameView(_ReservationPublishedNameView):
    """Preview the name that Kea publishes for a DHCPv4 Reservation hostname."""

    dhcp_version = 4


class ServerReservation6PublishedNameView(_ReservationPublishedNameView):
    """Preview the name that Kea publishes for a DHCPv6 Reservation hostname."""

    dhcp_version = 6


class ServerReservation4DeleteView(_ReservationDeleteView):
    """Delete one DHCPv4 Reservation by immutable Scope and Identity."""

    dhcp_version = 4
    form_class = forms.Reservation4Form


class ServerReservation6DeleteView(_ReservationDeleteView):
    """Delete one DHCPv6 Reservation by immutable Scope and Identity."""

    dhcp_version = 6
    form_class = forms.Reservation6Form

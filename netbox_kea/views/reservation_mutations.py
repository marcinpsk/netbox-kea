import ipaddress
import logging
from dataclasses import replace
from typing import Any, Literal

import requests
from django.contrib import messages
from django.core import signing
from django.core.exceptions import BadRequest, ValidationError
from django.db import DatabaseError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from netbox.views import generic

from .. import constants, forms
from ..dhcp_options import DHCPOption
from ..kea import KeaException
from ..models import Server
from ..reservations import (
    ClearValue,
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
from ..subnet_catalogue import MutationScope
from ..sync import sync_reservation_to_netbox
from ..utilities import fetch_subnet_choices, kea_error_hint
from ._base import _KeaChangeMixin
from .reservations import _RESERVATIONS_TAB, _build_reservation_options_formset, _configured_capabilities
from .subnets import _warn_reservation_pool_overlap

logger = logging.getLogger(__name__)

_FINGERPRINT_SALT = "netbox_kea.reservation-managed-facts"
FLEX_ID_DOCUMENTATION_URL = (
    "https://kea.readthedocs.io/en/latest/arm/hooks.html#flex-id-flexible-identifiers-for-host-reservations"
)


def _options_from_formset(
    options_formset: Any,
    current: tuple[DHCPOption, ...] = (),
) -> tuple[DHCPOption, ...]:
    """Apply visible form changes while preserving unexposed option facts."""
    options: list[DHCPOption] = []
    for index, row in enumerate(getattr(options_formset, "cleaned_data", []) or []):
        if not row or not row.get("name") or row.get("DELETE"):
            continue
        submitted_always_send = bool(row.get("always_send"))
        if index < len(current):
            existing = current[index]
            displayed_name = existing.name or (str(existing.code) if existing.code is not None else "")
            if row["name"] == displayed_name:
                always_send = (
                    existing.always_send
                    if submitted_always_send == bool(existing.always_send)
                    else submitted_always_send
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
            "name": option.name or (str(option.code) if option.code is not None else ""),
            "data": option.data,
            "always_send": bool(option.always_send),
        }
        for option in reservation.options
    ]


def _signed_fingerprint(reservation: Reservation) -> str:
    return signing.dumps(
        {
            "family": reservation.family,
            "subnet_id": reservation.scope.subnet.subnet_id,
            "identifier_type": reservation.identity.identifier_type,
            "identifier": reservation.identity.value,
            "fingerprint": reservation_fingerprint(reservation),
        },
        salt=_FINGERPRINT_SALT,
        compress=True,
    )


def _fingerprint_from_post(token: str, reservation: Reservation) -> str:
    try:
        payload = signing.loads(token, salt=_FINGERPRINT_SALT, max_age=86_400)
    except signing.BadSignature as exc:
        raise ReservationConflict("The edit fingerprint is invalid or expired.") from exc
    target = {
        "family": reservation.family,
        "subnet_id": reservation.scope.subnet.subnet_id,
        "identifier_type": reservation.identity.identifier_type,
        "identifier": reservation.identity.value,
    }
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in target.items()):
        raise ReservationConflict("The edit fingerprint does not match this Reservation.")
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ReservationConflict("The edit fingerprint is invalid.")
    return fingerprint


def _identity_from_request(request: HttpRequest, version: Literal[4, 6]) -> ReservationIdentity:
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


def _load_target(
    server: Server,
    version: Literal[4, 6],
    subnet_id: int,
    identity: ReservationIdentity,
) -> tuple[Reservation, Any]:
    with MutationScope(server, version) as mutation_scope:
        subnet = mutation_scope.find_by_id(subnet_id)
        if subnet is None:
            raise Http404("Reservation Subnet not found.")
        scope = InSubnetReservationScope(subnet.identity)
        catalogue = mutation_scope.snapshot
        if catalogue is None:
            raise RuntimeError("The Subnet Catalogue is unavailable.")
        client = server.get_client(version=version)
        reservation = client.reservation_by_identity(version, catalogue, scope, identity)
    if reservation is None:
        raise Http404("Reservation not found.")
    return reservation, catalogue


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
    except (ImportError, DatabaseError):
        logger.exception("Could not record the confirmed Reservation mutation in the journal")


def _confirmed_side_effects(
    request: HttpRequest,
    server: Server,
    action: Literal["created", "updated", "deleted"],
    result: ReservationMutationResult,
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
        messages.warning(request, "Kea applied the change, but could not persist it to disk.")
    elif result.persistence == "not-requested":
        messages.info(request, "Kea applied the change. Configuration persistence is disabled for this server.")
    if sync_to_netbox and result.intended is not None and not result.intended.addresses:
        messages.info(request, f"Reservation {action}. Nothing to sync to NetBox because it reserves no IP address.")
    elif (
        sync_to_netbox
        and result.intended is not None
        and not (request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress"))
    ):
        logger.warning("User %r requested Reservation IPAM sync without IPAM write permission", request.user)
        messages.warning(
            request, f"Reservation {action}, but it was not synced to NetBox. IPAM permission is required."
        )
    elif sync_to_netbox and result.intended is not None and result.intended.addresses:
        try:
            sync_reservation_to_netbox(result.intended, cleanup=False, force=True)
        except (DatabaseError, ValidationError, ValueError, requests.RequestException):
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


class _ReservationMutationView(_KeaChangeMixin, generic.ObjectView):
    queryset = Server.objects.all()
    tab = _RESERVATIONS_TAB
    template_name = "netbox_kea/server_reservation_form.html"
    dhcp_version: Literal[4, 6]
    form_class: type[forms.Reservation4Form] | type[forms.Reservation6Form]
    form_action: str

    def _form_context(
        self,
        server: Server,
        form: Any,
        options_formset: Any,
        capabilities: ReservationCapabilities | None,
        *,
        subnet_choices: list[tuple[str, int]] | None = None,
        subnet_cmds_available: bool = True,
        lease_diff: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "object": server,
            "form": form,
            "options_formset": options_formset,
            "return_url": reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[server.pk]),
            "action": self.form_action,
            "dhcp_version": self.dhcp_version,
            "tab": self.tab,
            "subnet_choices": subnet_choices or [],
            "subnet_cmds_available": subnet_cmds_available,
            "subnet_datalist_id": constants.RESERVATION_SUBNET_DATALIST_ID,
            "reservation_capabilities": capabilities,
            "mutation_available": bool(capabilities and capabilities.mutation_available),
            "flex_id_documentation_url": FLEX_ID_DOCUMENTATION_URL,
            "lease_diff": lease_diff,
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
        subnet_choices, subnet_cmds_available = fetch_subnet_choices(server, self.dhcp_version)
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
            subnet_choices=subnet_choices,
            subnet_cmds_available=subnet_cmds_available,
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        capabilities = _configured_capabilities(server, self.dhcp_version)
        form = self.form_class(data=request.POST, capabilities=capabilities)
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        if form.is_valid() and options_valid:
            try:
                result = self._create(request, server, form.cleaned_data, _options_from_formset(options_formset))
                _confirmed_side_effects(
                    request,
                    server,
                    "created",
                    result,
                    sync_to_netbox=bool(form.cleaned_data.get("sync_to_netbox")),
                )
                messages.success(request, "Reservation created.")
                return redirect(reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[server.pk]))
            except KeaException as exc:
                logger.exception("Kea rejected a DHCPv%s Reservation create", self.dhcp_version)
                messages.error(request, kea_error_hint(exc))
            except (requests.RequestException, RuntimeError, ValueError):
                logger.exception("Could not create a DHCPv%s Reservation", self.dhcp_version)
                messages.error(request, "The Reservation could not be created. See server logs.")
        subnet_choices, subnet_cmds_available = fetch_subnet_choices(server, self.dhcp_version)
        return self._render(
            request,
            server,
            form,
            options_formset,
            capabilities,
            subnet_choices=subnet_choices,
            subnet_cmds_available=subnet_cmds_available,
        )

    def _create(
        self,
        request: HttpRequest,
        server: Server,
        cleaned_data: dict[str, Any],
        options: tuple[DHCPOption, ...],
    ) -> ReservationMutationResult:
        with MutationScope(server, self.dhcp_version) as mutation_scope:
            subnet = mutation_scope.find_by_cidr(cleaned_data["subnet_cidr"])
            if subnet is None:
                raise ValueError("The submitted Subnet is not present in the live Subnet Catalogue.")
            catalogue = mutation_scope.snapshot
            if catalogue is None:
                raise RuntimeError("The Subnet Catalogue is unavailable.")
            scope = InSubnetReservationScope(subnet.identity)
            identity = ReservationIdentity(cleaned_data["identifier_type"], cleaned_data["identifier"])
            if self.dhcp_version == 4:
                addresses = (
                    (ipaddress.IPv4Address(cleaned_data["ip_address"]),) if cleaned_data.get("ip_address") else ()
                )
                reservation: Reservation = IPv4Reservation(
                    scope=scope,
                    identity=identity,
                    addresses=addresses,
                    hostname=cleaned_data.get("hostname", ""),
                    options=options,
                )
            else:
                addresses = tuple(
                    ipaddress.IPv6Address(value)
                    for value in (cleaned_data.get("ip_addresses") or "").split(",")
                    if value
                )
                prefixes = tuple(
                    ipaddress.IPv6Network(value) for value in (cleaned_data.get("prefixes") or "").split(",") if value
                )
                reservation = IPv6Reservation(
                    scope=scope,
                    identity=identity,
                    addresses=addresses,
                    delegated_prefixes=prefixes,
                    hostname=cleaned_data.get("hostname", ""),
                    options=options,
                )
            client = server.get_client(version=self.dhcp_version)
            try:
                for address in reservation.addresses:
                    _warn_reservation_pool_overlap(
                        request,
                        client,
                        self.dhcp_version,
                        subnet.identity.subnet_id,
                        str(address),
                    )
            except (KeaException, requests.RequestException, RuntimeError, ValueError):
                logger.debug("Could not check Reservation pool overlap", exc_info=True)
            return client.reservation_create(reservation, catalogue)


class _ReservationEditView(_ReservationMutationView):
    form_action = "Edit"

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        try:
            reservation, _catalogue = _load_target(server, self.dhcp_version, subnet_id, identity)
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Could not load the Reservation edit target")
            messages.error(request, "The Reservation could not be loaded. See server logs.")
            return redirect(reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[server.pk]))
        capabilities = _configured_capabilities(server, self.dhcp_version)
        form = self._form_for(reservation, capabilities)
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
        return_url = reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[server.pk])
        try:
            current, catalogue = _load_target(server, self.dhcp_version, subnet_id, identity)
        except Http404:
            raise
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Could not reload the Reservation edit target")
            messages.error(request, "The Reservation could not be reloaded. Edit stopped.")
            return redirect(return_url)
        capabilities = _configured_capabilities(server, self.dhcp_version)
        form = self.form_class(data=request.POST, initial=self._initial(current), capabilities=capabilities)
        for field in ("subnet_cidr", "identifier_type", "identifier"):
            form.fields[field].disabled = True
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        if form.is_valid() and options_valid:
            try:
                fingerprint = _fingerprint_from_post(form.cleaned_data["managed_fingerprint"], current)
                change = self._change(
                    current, form.cleaned_data, _options_from_formset(options_formset, current.options)
                )
                client = server.get_client(version=self.dhcp_version)
                result = client.reservation_change(current, fingerprint, change, catalogue)
                _confirmed_side_effects(
                    request,
                    server,
                    "updated",
                    result,
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
        return self._render(request, server, form, options_formset, capabilities)

    def _initial(self, reservation: Reservation) -> dict[str, Any]:
        initial = {
            "subnet_cidr": reservation.scope.subnet.cidr,
            "identifier_type": reservation.identity.identifier_type,
            "identifier": reservation.identity.value,
            "hostname": reservation.hostname,
            "managed_fingerprint": _signed_fingerprint(reservation),
        }
        if reservation.family == 4:
            initial["ip_address"] = str(reservation.addresses[0]) if reservation.addresses else ""
        else:
            initial["ip_addresses"] = ",".join(str(address) for address in reservation.addresses)
            initial["prefixes"] = ",".join(str(prefix) for prefix in reservation.delegated_prefixes)
        return initial

    def _form_for(self, reservation: Reservation, capabilities: ReservationCapabilities | None):
        form = self.form_class(initial=self._initial(reservation), capabilities=capabilities)
        for field in ("subnet_cidr", "identifier_type", "identifier"):
            form.fields[field].disabled = True
        return form

    def _change(
        self,
        current: Reservation,
        cleaned_data: dict[str, Any],
        options: tuple[DHCPOption, ...],
    ) -> ReservationChange:
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
            hostname=_change(current.hostname, cleaned_data.get("hostname", ""), ""),
            options=_change(current.options, options, ()),
        )


class _ReservationDeleteView(_ReservationMutationView):
    template_name = "netbox_kea/server_reservation_delete.html"
    form_action = "Delete"

    def get(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        try:
            reservation, _catalogue = _load_target(server, self.dhcp_version, subnet_id, identity)
        except Http404:
            raise
        except (KeaException, requests.RequestException, RuntimeError, ValueError):
            logger.exception("Could not load the Reservation delete target")
            messages.error(request, "The Reservation could not be loaded. See server logs.")
            return redirect(reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[server.pk]))
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "reservation_label": f"{reservation.identity.identifier_type} {reservation.identity.value}",
                "subnet_id": subnet_id,
                "dhcp_version": self.dhcp_version,
                "return_url": reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[server.pk]),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int) -> HttpResponse:
        server = self.get_object(pk=pk)
        identity = _identity_from_request(request, self.dhcp_version)
        return_url = reverse(f"plugins:netbox_kea:server_reservations{self.dhcp_version}", args=[server.pk])
        capabilities = _configured_capabilities(server, self.dhcp_version)
        if capabilities is None or not capabilities.mutation_available:
            messages.error(request, "Reservation mutation capabilities are unavailable.")
            return redirect(return_url)
        try:
            reservation, catalogue = _load_target(server, self.dhcp_version, subnet_id, identity)
            client = server.get_client(version=self.dhcp_version)
            result = client.reservation_delete(reservation, catalogue)
            _confirmed_side_effects(request, server, "deleted", result)
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


class ServerReservation4DeleteView(_ReservationDeleteView):
    """Delete one DHCPv4 Reservation by immutable Scope and Identity."""

    dhcp_version = 4
    form_class = forms.Reservation4Form


class ServerReservation6DeleteView(_ReservationDeleteView):
    """Delete one DHCPv6 Reservation by immutable Scope and Identity."""

    dhcp_version = 6
    form_class = forms.Reservation6Form

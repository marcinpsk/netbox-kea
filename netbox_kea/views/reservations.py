import concurrent.futures
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import BadRequest, ValidationError
from django.db import DatabaseError
from django.db.utils import OperationalError, ProgrammingError
from django.http import Http404, HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from netbox.views import generic
from utilities.views import register_model_view

from .. import constants, forms, tables
from ..kea import KeaClient, KeaException, PartialPersistError, iter_reservations
from ..models import Server
from ..signals import reservation_created, reservation_deleted, reservation_updated
from ..sync import sync_reservation_to_netbox
from ..utilities import (
    OptionalViewTab,
    _enrich_reservation_sort_key,
    kea_error_hint,
    subnet_sort_key,
)
from ._base import _KeaChangeMixin
from .subnets import _warn_reservation_pool_overlap

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identifier key constants
# ---------------------------------------------------------------------------
#: All identifier keys that Kea supports across DHCPv4 and DHCPv6.  Used when
#: clearing identifier fields before writing an updated reservation.
_ALL_IDENTIFIER_KEYS: tuple[str, ...] = (
    "hw-address",
    "duid",
    "client-id",
    "flex-id",
    "circuit-id",
    "remote-id",
)

#: Identifier types supported per DHCP version, in preference order.  Shared with the
#: forms and the CSV importer so all three accept the same set.
_V4_IDENTIFIER_TYPES: tuple[str, ...] = constants.RESERVATION_IDENTIFIER_TYPES[4]
_V6_IDENTIFIER_TYPES: tuple[str, ...] = constants.RESERVATION_IDENTIFIER_TYPES[6]


@dataclass(frozen=True)
class _ReservationLookup:
    """How to address one reservation in Kea: by IP, or by client identifier.

    A host reservation that reserves no address cannot be keyed by IP — the whole
    reason issue #110 existed — so edit and delete additionally accept an identifier
    key.  ``KeaClient.reservation_get``/``reservation_del`` already accept either.
    """

    subnet_id: int
    ip_address: str = ""
    identifier_type: str = ""
    identifier: str = ""

    @property
    def by_identifier(self) -> bool:
        """True when this lookup keys on the client identifier rather than an address."""
        return not self.ip_address

    @property
    def label(self) -> str:
        """Human-readable subject for messages and confirmation pages."""
        if self.ip_address:
            return self.ip_address
        return f"{self.identifier_type} {self.identifier}"

    def to_kwargs(self) -> dict[str, Any]:
        """Return the ``reservation_get`` / ``reservation_del`` keyword arguments."""
        if self.ip_address:
            return {"subnet_id": self.subnet_id, "ip_address": self.ip_address}
        return {
            "subnet_id": self.subnet_id,
            "identifier_type": self.identifier_type,
            "identifier": self.identifier,
        }


def _identifier_lookup_from_request(request: HttpRequest, subnet_id: int, version: int) -> _ReservationLookup:
    """Build an identifier-keyed lookup from the query string, or raise ``BadRequest``.

    The identifier travels as a query parameter rather than a path segment: reversing
    then takes only integers (so the route can never fail to reverse, which is what
    took the reservations tab down), and ``urlencode`` handles values a ``<str:>``
    segment would mangle — Django treats ``/`` as safe when reversing, so a ``flex-id``
    containing one would silently produce a broken URL.

    Everything the view will forward to Kea is validated here so all four routes
    behave identically on malformed input.
    """
    allowed = _V6_IDENTIFIER_TYPES if version == 6 else _V4_IDENTIFIER_TYPES
    types = request.GET.getlist("identifier_type")
    values = request.GET.getlist("identifier")
    if len(types) != 1 or len(values) != 1:
        raise BadRequest("Exactly one identifier_type and one identifier query parameter are required.")
    identifier_type, identifier = types[0].strip(), values[0].strip()
    if not identifier_type or not identifier:
        raise BadRequest("identifier_type and identifier must not be empty.")
    if identifier_type not in allowed:
        raise BadRequest(f"Unsupported identifier type for DHCPv{version}.")
    if len(identifier) > constants.max_identifier_length(identifier_type):
        raise BadRequest("Identifier is too long.")
    return _ReservationLookup(subnet_id=subnet_id, identifier_type=identifier_type, identifier=identifier)


def _apply_v6_addresses_and_prefixes(reservation: dict[str, Any], cleaned_data: dict[str, Any]) -> None:
    """Write the submitted addresses and delegated prefixes onto *reservation*.

    A blank field removes the key rather than writing an empty list, so a DHCPv6
    reservation can drop its addresses (becoming prefix-only) or its prefixes without
    Kea being sent an empty collection.  Disabled fields resolve to their initial
    value, so on the IP-keyed route this writes back what was reloaded from Kea.
    """
    addresses = [ip.strip() for ip in (cleaned_data.get("ip_addresses") or "").split(",") if ip.strip()]
    if addresses:
        reservation["ip-addresses"] = addresses
    else:
        reservation.pop("ip-addresses", None)
    prefixes = [prefix.strip() for prefix in (cleaned_data.get("prefixes") or "").split(",") if prefix.strip()]
    if prefixes:
        reservation["prefixes"] = prefixes
    else:
        reservation.pop("prefixes", None)


def _deleted_journal_entry(lookup: _ReservationLookup) -> dict[str, Any]:
    """Journal payload describing which reservation was deleted.

    An identifier-keyed delete has no address to record, so the identifier goes in
    instead — otherwise the entry would not say what was removed.
    """
    entry: dict[str, Any] = {"subnet-id": lookup.subnet_id}
    if lookup.ip_address:
        entry["ip-address"] = lookup.ip_address
    if lookup.identifier:
        entry[lookup.identifier_type] = lookup.identifier
    return entry


def _send_reservation_deleted(
    server: "Server",
    lookup: _ReservationLookup,
    version: int,
    request: HttpRequest,
) -> None:
    """Fire ``reservation_deleted`` with the same kwargs regardless of route.

    Receivers get ``ip_address``, ``identifier_type`` and ``identifier`` on every
    delete, each ``None`` where it does not apply, rather than a payload whose shape
    depends on which URL the operator happened to use.
    """
    reservation_deleted.send_robust(
        sender=None,
        server=server,
        ip_address=lookup.ip_address or None,
        identifier_type=lookup.identifier_type or None,
        identifier=lookup.identifier or None,
        dhcp_version=version,
        request=request,
    )


class _ReservationLookupMixin:
    """Shared addressing and field-freezing policy for the edit/delete views.

    Routes differ only in how they address a reservation and which form fields that
    makes immutable, so both are class attributes rather than branches:

    - IP-keyed routes freeze the address along with the subnet and identifier, as
      they always have.
    - Identifier-keyed routes freeze the subnet and identifier only, leaving the
      address editable — Kea's ``reservation-update`` replaces the whole host keyed
      by identifier, so a reservation can gain or lose its fixed address without the
      URL that addresses it changing.
    """

    #: 4 or 6; set by each concrete view.
    dhcp_version: int = 4
    #: Form fields this route fixes and the user may not change.  Empty on the delete
    #: views, which have no form.
    immutable_fields: frozenset[str] = frozenset()
    #: Identifier-keyed subclasses flip this; the base routes key on the address.
    lookup_by_identifier: bool = False

    def get_lookup(self, request: HttpRequest, subnet_id: int, **kwargs) -> _ReservationLookup:
        """Return how this route addresses the reservation."""
        if self.lookup_by_identifier:
            return _identifier_lookup_from_request(request, subnet_id, self.dhcp_version)
        return _ReservationLookup(subnet_id=subnet_id, ip_address=kwargs["ip_address"])

    def apply_immutable_fields(self, form) -> None:
        """Disable the fields this route fixes.

        Django resolves a disabled field to its ``initial`` value and ignores what the
        browser submitted, which is what stops a hand-crafted POST from changing the
        key the URL addresses.  Callers must seed ``initial`` first.
        """
        for name in self.immutable_fields:
            # A stale name here would silently stop freezing a key field.
            if name not in form.fields:
                raise KeyError(f"{type(self).__name__}.immutable_fields names unknown form field {name!r}")
            form.fields[name].disabled = True


#: All known identifier keys (hyphen and underscore variants) for journal
#: log extraction — includes normalised forms that Kea may return after
#: ``format_leases()`` processing.
_JOURNAL_IDENTIFIER_KEYS: tuple[str, ...] = (
    "hw-address",
    "hw_address",
    "duid",
    "client-id",
    "client_id",
    "circuit-id",
    "circuit_id",
    "flex-id",
    "flex_id",
    "remote-id",
    "remote_id",
)


def _build_reservation_options_formset(post_data: Any) -> tuple[Any, bool]:
    """Build a ReservationOptionsFormSet from POST data.

    If the management form fields are absent (legacy callers, tests), returns an
    empty unbound formset treated as valid with no options.

    If any ``options-`` keys are present but ``options-TOTAL_FORMS`` is absent
    (partial/truncated submission), returns an unbound formset with is_valid=False.

    Returns:
        (formset, is_valid)

    """
    if "options-TOTAL_FORMS" in post_data:
        fs = forms.ReservationOptionsFormSet(data=post_data, prefix="options")
        return fs, fs.is_valid()
    # Detect partial submission: some options-* keys exist but management form is missing
    if any(k.startswith("options-") for k in post_data):
        fs = forms.ReservationOptionsFormSet(data=post_data, prefix="options")
        fs.is_valid()  # populate errors so the template can show management-form error
        return fs, False
    return forms.ReservationOptionsFormSet(prefix="options"), True


def _merge_reservation_option_data(reservation: dict[str, Any], options_formset: Any) -> None:
    """Apply the submitted options formset onto *reservation* in place.

    An unbound formset, or a bound one reporting no initial and no submitted forms, means the
    options section was not part of this submission, so existing ``option-data`` is left alone.
    """
    option_data = [
        {"name": f["name"], "data": f["data"], **({"always-send": True} if f.get("always_send") else {})}
        for f in (getattr(options_formset, "cleaned_data", []) or [])
        if f and f.get("name") and not f.get("DELETE")
    ]
    submitted = bool(options_formset.is_bound) and bool(
        options_formset.total_form_count() or options_formset.initial_form_count()
    )
    if option_data:
        reservation["option-data"] = option_data
    elif submitted or not reservation.get("option-data"):
        reservation.pop("option-data", None)


def _add_reservation_journal(server: "Server", user: Any, action: str, reservation: dict) -> None:
    """Create a JournalEntry on *server* recording a reservation CRUD event.

    Silently skips if JournalEntry is unavailable (older NetBox or import error).

    Args:
        server: The Server instance the journal entry is attached to.
        user: The request.user who performed the action.
        action: Human-readable action name: "created", "updated", or "deleted".
        reservation: The reservation dict (Kea format, may be hyphenated or underscored keys).

    """
    try:
        from extras.models import JournalEntry

        ip = reservation.get("ip-address") or reservation.get("ip_address", "")
        ips = reservation.get("ip-addresses") or reservation.get("ip_addresses", [])
        if ips and not ip:
            ip = ips[0] if isinstance(ips, list) else ips
        hostname = reservation.get("hostname", "")
        identifier = next(
            (reservation.get(key, "") for key in _JOURNAL_IDENTIFIER_KEYS if reservation.get(key)),
            "",
        )
        parts = [f"Reservation {action}: {ip}"]
        if hostname:
            parts.append(f"hostname: {hostname}")
        if identifier:
            parts.append(f"identifier: {identifier}")
        JournalEntry.objects.create(
            assigned_object=server,
            created_by=user,
            kind="info",
            comments="; ".join(parts),
        )
    except ImportError:
        pass  # JournalEntry unavailable on older NetBox versions
    except (ProgrammingError, OperationalError):
        logger.debug("Failed to create reservation journal entry", exc_info=True)
    except DatabaseError:
        logger.debug("Unexpected DB error creating reservation journal entry", exc_info=True)


def _run_reservation_success_side_effects(
    request: "HttpRequest",
    server: "Server",
    reservation: dict,
    dhcp_version: int,
    action: str,
    sync_to_netbox: bool,
    partial_persist: bool = False,
) -> None:
    """Run journal, signal, and optional IPAM sync after a successful reservation add/update.

    Args:
        request: The current HTTP request.
        server: The Kea Server instance.
        reservation: The reservation dict in Kea format.
        dhcp_version: 4 or 6.
        action: "created" or "updated".
        sync_to_netbox: Whether to sync the reservation to NetBox IPAM.
        partial_persist: If True, appends a config-write-failed warning message.

    """
    signal = reservation_created if action == "created" else reservation_updated
    _add_reservation_journal(server, request.user, action, reservation)
    signal.send_robust(
        sender=None,
        server=server,
        reservation=reservation,
        dhcp_version=dhcp_version,
        request=request,
    )
    _v4_ip = reservation.get("ip-address") or ""
    _v6_ips = reservation.get("ip-addresses")
    has_address = bool(_v4_ip) or bool(isinstance(_v6_ips, list) and _v6_ips)
    if sync_to_netbox and not has_address:
        # Checked before the permission test: there is nothing to write to IPAM, so
        # "requires IPAM permission" would be a misleading reason to report.
        messages.info(request, f"Reservation {action}. Nothing to sync to NetBox — it reserves no IP address.")
    elif sync_to_netbox and not (
        request.user.has_perm("ipam.add_ipaddress") and request.user.has_perm("ipam.change_ipaddress")
    ):
        # The sync below uses force=True, overriding the foreign-IP guard and
        # writing IPAM records. change_server permission alone is not enough —
        # require IPAM write permission (mirrors the per-row/bulk sync endpoints).
        logger.warning("User %r ticked sync-to-NetBox without IPAM write permission — sync skipped", request.user)
        messages.warning(request, f"Reservation {action}, but it was not synced to NetBox (requires IPAM permission).")
    elif sync_to_netbox:
        if isinstance(_v6_ips, list) and len(_v6_ips) > 1:
            ip_label = f"{len(_v6_ips)} addresses"
        else:
            ip_label = _v4_ip or (_v6_ips[0] if isinstance(_v6_ips, list) and _v6_ips else "")
        try:
            # The user explicitly ticked "sync to NetBox" on this form, so this is
            # an explicit single-record sync → override the foreign-IP guard.
            _, nb_created, _ = sync_reservation_to_netbox(reservation, cleanup=False, force=True)
            nb_msg = "created" if nb_created else "updated"
            messages.info(request, f"NetBox IPAddress {ip_label} {nb_msg}.")
        except (ValueError, DatabaseError, ValidationError, requests.RequestException):
            logger.exception("Failed to sync DHCPv%s reservation %s to NetBox", dhcp_version, ip_label)
            messages.warning(request, f"Reservation {action}, but NetBox IPAM sync failed.")
    if partial_persist:
        messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")


#: Lease keys that carry a client identifier, per DHCP version.  A DHCPv4 lease reports
#: ``hw-address`` and ``client-id``; a DHCPv6 lease reports ``duid`` and, when Kea could
#: derive one, ``hw-address``.  The opaque identifiers (circuit-id, flex-id, remote-id)
#: appear on no lease, so a reservation keyed by one has nothing to match on.
_LEASE_IDENTIFIER_KEYS: dict[int, tuple[str, ...]] = {
    4: ("hw-address", "client-id"),
    6: ("duid", "hw-address"),
}

_HEX_DIGITS = frozenset("0123456789abcdef")


def _normalise_identifier_value(value: Any) -> str:
    """Return *value* as lower-case colon-delimited hex octets, or ``""`` if it is not hex.

    Kea and the operator write the same identifier several ways ("AA-BB-CC", "aabbcc"),
    so both sides of an identifier match go through this.
    """
    if not isinstance(value, str):
        return ""
    compact = value.replace(":", "").replace("-", "").lower()
    if not compact or len(compact) % 2 or not set(compact) <= _HEX_DIGITS:
        return ""
    return ":".join(compact[i : i + 2] for i in range(0, len(compact), 2))


def _lease_identifiers(lease: dict[str, Any], version: int) -> set[str]:
    """Return the normalised client identifiers *lease* carries."""
    found = {_normalise_identifier_value(lease.get(key)) for key in _LEASE_IDENTIFIER_KEYS[version]}
    found.discard("")
    return found


def _reservation_lease_identifier(reservation: dict[str, Any], version: int) -> str:
    """Return the normalised identifier this reservation can be matched to a lease by.

    Empty when the reservation is keyed by something no lease reports.
    """
    identifier_type, identifier = _get_reservation_identifier(reservation, version)
    if identifier_type not in _LEASE_IDENTIFIER_KEYS[version]:
        return ""
    return _normalise_identifier_value(identifier)


def _enrich_reservations_with_lease_status(client: "KeaClient", reservations: list[dict], version: int) -> None:  # noqa: C901
    """Enrich each reservation dict with ``has_active_lease`` (bool | None).

    Queries ``lease4-get-all`` / ``lease6-get-all`` per unique subnet to find
    active leases.  Sets ``r["has_active_lease"] = True/False`` for each
    reservation.  Leaves ``has_active_lease`` unset (None) if the ``lease_cmds``
    hook is unavailable or an unexpected error occurs, so the template can
    distinguish "unknown" from "no lease".

    A reservation that reserves no address has no IP to match a lease on, so it is
    matched by client identifier instead — within its own subnet only, since the same
    client holding a lease in another subnet is a different reservation's row.

    Args:
        client: Connected KeaClient for the server.
        reservations: List of reservation dicts (mutated in-place).
        version: DHCP version (4 or 6).

    """
    if not reservations:
        return

    service = f"dhcp{version}"
    lease_cmd = f"lease{version}-get-all"
    unique_subnet_ids = {r.get("subnet-id") for r in reservations if isinstance(r.get("subnet-id"), (int, str))}

    active_lease_ips: set[str] = set()
    # Normalised lease identifiers per subnet, for the address-less rows.
    subnet_lease_identifiers: dict[Any, set[str]] = {}
    hook_unavailable = False

    def _fetch_leases_for_subnet(sid: int) -> tuple[list[str], set[str]] | None | bool:
        """Return (lease IPs, client identifiers), None if the lease_cmds hook is not loaded, or False on error."""
        with client.clone() as worker_client:  # requests.Session is not thread-safe
            try:
                resp = worker_client.command(
                    lease_cmd,
                    service=[service],
                    arguments={"subnets": [sid]},
                    check=(0, 3),
                )
                if not resp or not isinstance(resp[0], dict):
                    return False  # malformed envelope — indeterminate state
                if resp[0].get("result") != 3:
                    raw_args = resp[0].get("arguments")
                    if not isinstance(raw_args, dict):
                        return False  # malformed payload — indeterminate state
                    args = raw_args
                    leases = args.get("leases") or []
                    if not isinstance(leases, list):
                        return False  # malformed payload — indeterminate state
                    valid = [lease for lease in leases if isinstance(lease, dict)]
                    identifiers: set[str] = set()
                    for lease in valid:
                        identifiers |= _lease_identifiers(lease, version)
                    return [lease.get("ip-address", "") for lease in valid], identifiers
                return [], set()
            except KeaException as exc:
                if exc.response.get("result") == 2:
                    return None  # hook not loaded
                logger.debug("lease fetch failed for subnet %s (KeaException result != 2): %s", sid, exc)
                return False  # error sentinel — state is indeterminate
            except (requests.RequestException, ValueError):  # noqa: BLE001
                logger.debug("lease fetch failed for subnet %s (unexpected error)", sid)
                return False  # error sentinel

    if not unique_subnet_ids:
        return

    indeterminate_subnet_ids: set[int] = set()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(unique_subnet_ids), 10)) as executor:
            futures = {executor.submit(_fetch_leases_for_subnet, sid): sid for sid in unique_subnet_ids}
            for future in concurrent.futures.as_completed(futures):
                sid = futures[future]
                result = future.result()
                if result is None:
                    hook_unavailable = True
                elif result is False:
                    indeterminate_subnet_ids.add(sid)
                else:
                    ips, identifiers = result
                    active_lease_ips.update(ips)
                    subnet_lease_identifiers.setdefault(sid, set()).update(identifiers)
    except Exception:  # noqa: BLE001
        logger.debug("Enrichment task failed", exc_info=True)
        return

    if hook_unavailable:
        return

    for r in reservations:
        subnet_id_r = r.get("subnet-id")
        if subnet_id_r in indeterminate_subnet_ids:
            # Cannot determine lease state for this subnet — leave has_active_lease unset
            continue
        # Check all address fields: single "ip-address" (v4/v6), normalised "ip_address",
        # and "ip-addresses" list (DHCPv6 reservations with multiple addresses).
        addrs: list[str] = []
        single = r.get("ip-address") or r.get("ip_address")
        if single:
            addrs.append(single)
        raw_ips = r.get("ip-addresses")
        if isinstance(raw_ips, list):
            addrs.extend(raw_ips)
        if addrs:
            r["has_active_lease"] = any(a in active_lease_ips for a in addrs)
            continue
        identifier = _reservation_lease_identifier(r, version)
        in_own_subnet = subnet_lease_identifiers.get(subnet_id_r) or frozenset()
        r["has_active_lease"] = bool(identifier) and identifier in in_own_subnet


def _filter_reservations(
    reservations: list[dict[str, Any]], q: str, subnet_id: int | None, version: int
) -> list[dict[str, Any]]:
    """Filter a list of reservation dicts by free-text query and/or subnet ID.

    Filtering is done in-memory (client-side) because ``reservation-get-page``
    does not support server-side search.

    Args:
        reservations: List of reservation dicts (Kea wire format + normalised keys).
        q: Free-text query; matched case-insensitively against IP, hostname, and
           ``hw-address`` (DHCPv4) or ``duid`` (DHCPv6).
        subnet_id: If non-None, only reservations in this subnet ID are returned.
        version: 4 or 6 — determines which identifier field to search.

    """
    result = reservations
    if subnet_id is not None:
        result = [r for r in result if r.get("subnet-id") == subnet_id or r.get("subnet_id") == subnet_id]
    if q:
        q_lower = q.lower()

        def _s(val: Any) -> str:
            return str(val).lower() if val else ""

        if version == 4:
            result = [
                r
                for r in result
                if q_lower in _s(r.get("ip_address", r.get("ip-address", "")))
                or q_lower in _s(r.get("hostname", ""))
                or q_lower in _s(r.get("hw-address", ""))
                or q_lower in _s(r.get("client-id", ""))
                or q_lower in _s(r.get("circuit-id", ""))
                or q_lower in _s(r.get("flex-id", ""))
            ]
        else:
            result = [
                r
                for r in result
                if q_lower in _s(r.get("ip_address", ""))
                or any(q_lower in _s(ip) for ip in r.get("ip-addresses", []))
                or q_lower in _s(r.get("hostname", ""))
                or q_lower in _s(r.get("duid", ""))
                or q_lower in _s(r.get("hw-address", ""))
                or q_lower in _s(r.get("client-id", ""))
                or q_lower in _s(r.get("flex-id", ""))
            ]
    return result


# Single consolidated "Reservations" tab shared by the v4 and v6 views (see the
# analogous _LEASES_TAB in views/leases.py for the mechanism).
_RESERVATIONS_TAB = OptionalViewTab(label="Reservations", weight=1040, is_enabled=lambda s: s.dhcp4 or s.dhcp6)


@register_model_view(Server, "reservations4")
class ServerReservations4View(generic.ObjectView):
    """DHCPv4 reservations view; owns the shared Reservations tab."""

    queryset = Server.objects.all()
    tab = _RESERVATIONS_TAB
    template_name = "netbox_kea/server_reservations.html"

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Redirect to the v6 view on v6-only servers so the merged tab works."""
        instance = self.get_object(**kwargs)
        if not instance.dhcp4 and instance.dhcp6:
            return redirect(reverse("plugins:netbox_kea:server_reservations6", args=[instance.pk]))
        return super().get(request, **kwargs)

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Fetch reservations from Kea, apply search filters, and build the table."""
        server: Server = instance
        hook_available = True
        reservations: list[dict] = []
        try:
            client = server.get_client(version=4)
            reservations = list(iter_reservations(client, "dhcp4"))
        except KeaException as exc:
            if exc.response.get("result") == 2:
                hook_available = False
            else:
                logger.exception("Failed to fetch DHCPv4 reservations")
                messages.error(request, "Failed to load reservations from Kea.")
                reservations = []
        except (requests.RequestException, ValueError):
            logger.exception("Unexpected error fetching DHCPv4 reservations")
            messages.error(request, "Failed to load reservations from Kea.")
            reservations = []

        # Inject server_pk so the actions template column can build edit/delete URLs.
        for r in reservations:
            r["server_pk"] = server.pk
            r.setdefault("ip_address", r.get("ip-address", ""))
            r.setdefault("subnet_id", r.get("subnet-id", 0))
            _normalise_reservation_identifier(r, 4)
            _enrich_reservation_sort_key(r)

        # Apply search filter before enrichment to avoid unnecessary Kea API calls.
        search_form = forms.ReservationSearchForm(request.GET or None)
        if search_form.is_valid():
            reservations = _filter_reservations(
                reservations,
                q=search_form.cleaned_data.get("q", ""),
                subnet_id=search_form.cleaned_data.get("subnet_id"),
                version=4,
            )

        can_change = Server.objects.restrict(request.user, "change").filter(pk=server.pk).exists()
        # Enrich reservations with lease status + NetBox IPAM badges.
        _enrich_reservations_with_badges(reservations, server, 4, can_change=can_change)
        for r in reservations:
            r["can_change"] = can_change
        _attach_reservation_action_urls(reservations, server.pk, 4, can_change=can_change)

        table = tables.ReservationTable4(reservations, user=request.user)
        table.configure(request)
        return {
            "table": table,
            "dhcp_version": 4,
            "hook_available": hook_available,
            "search_form": search_form,
            "add_url": reverse("plugins:netbox_kea:server_reservation4_add", args=[server.pk]) if can_change else None,
            "bulk_sync_url": reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[server.pk])
            if can_change
            else None,
            "import_url": reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[server.pk])
            if can_change
            else None,
        }


@register_model_view(Server, "reservations6")
class ServerReservations6View(generic.ObjectView):
    """DHCPv6 reservations view (rendered under the shared Reservations tab)."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservations.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Fetch DHCPv6 reservations from Kea, apply search filters, and build the table."""
        server: Server = instance
        hook_available = True
        reservations: list[dict] = []
        try:
            client = server.get_client(version=6)
            reservations = list(iter_reservations(client, "dhcp6"))
        except KeaException as exc:
            if exc.response.get("result") == 2:
                hook_available = False
            else:
                logger.exception("Failed to fetch DHCPv6 reservations")
                messages.error(request, "Failed to load reservations from Kea.")
                reservations = []
        except (requests.RequestException, ValueError):
            logger.exception("Unexpected error fetching DHCPv6 reservations")
            messages.error(request, "Failed to load reservations from Kea.")
            reservations = []

        for r in reservations:
            r["server_pk"] = server.pk
            raw_ip_addrs = r.get("ip-addresses")
            if isinstance(raw_ip_addrs, list):
                ip_addrs = [ip for ip in raw_ip_addrs if isinstance(ip, str) and ip]
            elif isinstance(raw_ip_addrs, str) and raw_ip_addrs:
                ip_addrs = [raw_ip_addrs]
            else:
                ip_addrs = []
            r["ip-addresses"] = ip_addrs
            r["ip_address"] = ip_addrs[0] if ip_addrs else ""
            r["extra_ips"] = ip_addrs[1:]
            r.setdefault("subnet_id", r.get("subnet-id", 0))
            _normalise_reservation_identifier(r, 6)
            _normalise_reservation_prefixes(r)
            _enrich_reservation_sort_key(r)

        # Apply search filter before enrichment to avoid unnecessary Kea API calls.
        search_form = forms.ReservationSearchForm(request.GET or None)
        if search_form.is_valid():
            reservations = _filter_reservations(
                reservations,
                q=search_form.cleaned_data.get("q", ""),
                subnet_id=search_form.cleaned_data.get("subnet_id"),
                version=6,
            )

        can_change = Server.objects.restrict(request.user, "change").filter(pk=server.pk).exists()
        # Enrich reservations with lease status + NetBox IPAM badges.
        _enrich_reservations_with_badges(reservations, server, 6, can_change=can_change)
        for r in reservations:
            r["can_change"] = can_change
        _attach_reservation_action_urls(reservations, server.pk, 6, can_change=can_change)

        table = tables.ReservationTable6(reservations, user=request.user)
        table.configure(request)
        return {
            "table": table,
            "dhcp_version": 6,
            # Highlight the shared Reservations tab (this view has no class tab).
            "tab": _RESERVATIONS_TAB,
            "hook_available": hook_available,
            "search_form": search_form,
            "add_url": reverse("plugins:netbox_kea:server_reservation6_add", args=[server.pk]) if can_change else None,
            "bulk_sync_url": reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[server.pk])
            if can_change
            else None,
            "import_url": reverse("plugins:netbox_kea:server_reservation6_bulk_import", args=[server.pk])
            if can_change
            else None,
        }


#: The subnet list changes rarely, but the reservation add form fetches it on every GET
#: and again on every re-render after a validation error. Cache it per server+version.
_SUBNET_CHOICES_TTL = 300  # seconds (5 minutes)


def _subnet_choices_cache_key(server: Server, version: int) -> str:
    return f"netbox_kea:reservation_subnet_choices:{server.pk}:{version}"


def fetch_subnet_choices(server: Server, version: int) -> tuple[list[tuple[str, int]], bool]:
    """Return ``(choices, subnet_cmds_available)`` for the reservation form's Subnet CIDR field.

    Reads the subnet list through ``subnet_cmds``, the same source
    :meth:`KeaClient.subnet_id_from_cidr` resolves the submitted CIDR against, so the
    form cannot suggest a subnet that submitting would then fail to resolve. The lease
    search builds its own list from ``config-get`` instead, because that datalist only
    feeds a search box and must keep working without the hook.

    ``subnet_cmds_available`` is False only when Kea reports the command unsupported;
    the form warns about that. Any other failure degrades to no suggestions, which
    still leaves the field usable by typing.
    """
    cache_key = _subnet_choices_cache_key(server, version)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        choices = server.get_client(version=version).list_subnets(version)
    except KeaException as exc:
        if exc.response.get("result") == 2:
            return [], False
        logger.debug("Could not list dhcp%s subnets on server %s", version, server.pk, exc_info=True)
        return [], True
    except (requests.RequestException, ValueError, RuntimeError):
        logger.debug("Could not list dhcp%s subnets on server %s", version, server.pk, exc_info=True)
        return [], True
    choices.sort(key=subnet_sort_key)
    result = (choices, True)
    cache.set(cache_key, result, _SUBNET_CHOICES_TTL)
    return result


def _legacy_subnet_cidr(subnet_choices: list[tuple[str, int]], raw_subnet_id: str) -> str:
    """Resolve a legacy ``?subnet_id=`` prefill parameter to its subnet CIDR.

    Returns ``""`` when the value is not a plain ASCII integer or no subnet matches.
    ``str.isdigit()`` alone accepts Unicode digits ``int()`` cannot parse (e.g. ``"²"``).
    """
    if not (raw_subnet_id.isascii() and raw_subnet_id.isdigit()):
        return ""
    subnet_id = int(raw_subnet_id)
    for cidr, sid in subnet_choices:
        if sid == subnet_id:
            return cidr
    return ""


class _ReservationFormViewMixin:
    """Shared template, tab and context for the reservation add/edit form views."""

    template_name = "netbox_kea/server_reservation_form.html"
    tab = _RESERVATIONS_TAB
    dhcp_version: int
    form_action: str

    def form_context(
        self,
        server: Server,
        form: Any,
        options_formset: Any,
        return_url: str,
        subnet_choices: Any = None,
        subnet_cmds_available: bool = True,
    ) -> dict[str, Any]:
        """Build the context the reservation form template expects."""
        return {
            "object": server,
            "form": form,
            "options_formset": options_formset,
            "dhcp_version": self.dhcp_version,
            "action": self.form_action,
            "return_url": return_url,
            "tab": self.tab,
            "subnet_choices": subnet_choices,
            "subnet_cmds_available": subnet_cmds_available,
            "subnet_datalist_id": constants.RESERVATION_SUBNET_DATALIST_ID,
        }

    def render_form(
        self,
        request: HttpRequest,
        server: Server,
        form: Any,
        options_formset: Any,
        return_url: str,
        subnet_choices: Any = None,
        subnet_cmds_available: bool = True,
    ) -> HttpResponse:
        """Render the reservation form template with the shared context."""
        return render(
            request,
            self.template_name,
            self.form_context(server, form, options_formset, return_url, subnet_choices, subnet_cmds_available),
        )

    def render_add_form(
        self, request: HttpRequest, server: Server, form: Any, options_formset: Any, return_url: str
    ) -> HttpResponse:
        """Render the add form with the server's subnet suggestions attached."""
        subnet_choices, subnet_cmds_available = fetch_subnet_choices(server, self.dhcp_version)
        return self.render_form(
            request, server, form, options_formset, return_url, subnet_choices, subnet_cmds_available
        )

    def resolve_subnet_id(self, request: HttpRequest, client: KeaClient, form: Any, subnet_cidr: str) -> int | None:
        """Resolve *subnet_cidr* to its Kea subnet id.

        Returns ``None`` when the lookup fails or matches nothing, after recording the
        reason as a message (lookup failure) or a field error (no match). The caller
        re-renders the form in both cases.
        """
        try:
            subnet_id = client.subnet_id_from_cidr(self.dhcp_version, subnet_cidr)
        except KeaException as exc:
            logger.exception("Failed to look up subnet CIDR %s in Kea", subnet_cidr)
            messages.error(request, kea_error_hint(exc))
            return None
        except requests.RequestException:
            logger.exception("Network error looking up subnet CIDR %s in Kea", subnet_cidr)
            messages.error(request, "Network error communicating with Kea: see server logs.")
            return None
        except (ValueError, RuntimeError):
            logger.exception("Malformed Kea response looking up subnet CIDR %s", subnet_cidr)
            messages.error(request, "Invalid response from Kea: see server logs.")
            return None
        if subnet_id is None:
            form.add_error("subnet_cidr", f"No subnet matching {subnet_cidr} found in Kea.")
        return subnet_id


class ServerReservation4AddView(_ReservationFormViewMixin, _KeaChangeMixin, generic.ObjectView):
    """Add a DHCPv4 host reservation."""

    queryset = Server.objects.all()
    dhcp_version = 4
    form_action = "Add"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render add form, optionally pre-filled from query parameters."""
        server = self.get_object(pk=pk)
        initial = {
            k: request.GET.get(k, "")
            for k in ("subnet_cidr", "ip_address", "identifier_type", "identifier", "hostname")
        }
        subnet_choices, subnet_cmds_available = fetch_subnet_choices(server, self.dhcp_version)
        if not initial.get("subnet_cidr"):
            # Legacy prefill links pass the raw subnet id; map it to the CIDR.
            initial["subnet_cidr"] = _legacy_subnet_cidr(subnet_choices, request.GET.get("subnet_id", ""))
        initial = {k: v for k, v in initial.items() if v}
        return self.render_form(
            request,
            server,
            forms.Reservation4Form(initial=initial),
            forms.ReservationOptionsFormSet(prefix="options"),
            reverse("plugins:netbox_kea:server_reservations4", args=[pk]),
            subnet_choices,
            subnet_cmds_available,
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate form and create reservation in Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation4Form(data=request.POST)
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        if form.is_valid() and options_valid:
            cd = form.cleaned_data

            def _rerender() -> HttpResponse:
                return self.render_add_form(request, server, form, options_formset, return_url)

            try:
                client = server.get_client(version=4)
            except ValueError:
                logger.exception("Failed to create DHCPv4 client for server %s", server.pk)
                messages.error(request, "Failed to connect to Kea: see server logs.")
                return _rerender()
            subnet_id = self.resolve_subnet_id(request, client, form, cd["subnet_cidr"])
            if subnet_id is None:
                return _rerender()
            reservation = {
                "subnet-id": subnet_id,
                cd["identifier_type"]: cd["identifier"],
            }
            # Omit the key entirely rather than sending an empty address: a host may
            # legally reserve only a hostname, options or client classes.
            if cd.get("ip_address"):
                reservation["ip-address"] = cd["ip_address"]
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            option_data = [
                {"name": f["name"], "data": f["data"], **({"always-send": True} if f.get("always_send") else {})}
                for f in (getattr(options_formset, "cleaned_data", []) or [])
                if f and f.get("name") and not f.get("DELETE")
            ]
            if option_data:
                reservation["option-data"] = option_data
            # Advisory warning when the reservation IP is inside an existing pool (non-fatal).
            # Nothing to check when the reservation has no address.
            if cd.get("ip_address"):
                try:
                    _warn_reservation_pool_overlap(request, client, 4, subnet_id, cd["ip_address"])
                except Exception:  # noqa: BLE001
                    logger.debug("Pool overlap check failed for %s", cd.get("ip_address"), exc_info=True)
            subject = cd.get("ip_address") or f"{cd['identifier_type']} {cd['identifier']}"
            try:
                client.reservation_add("dhcp4", reservation)
                messages.success(request, f"Reservation for {subject} created.")
                _run_reservation_success_side_effects(
                    request, server, reservation, 4, "created", bool(cd.get("sync_to_netbox"))
                )
                return redirect(return_url)
            except PartialPersistError:
                _run_reservation_success_side_effects(
                    request, server, reservation, 4, "created", bool(cd.get("sync_to_netbox")), partial_persist=True
                )
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to create DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, kea_error_hint(exc))
            except requests.RequestException:
                logger.exception("Failed to create DHCPv4 reservation for %s (network error)", cd.get("ip_address"))
                messages.error(request, "Network error communicating with Kea: see server logs.")
            except ValueError:
                logger.exception("Failed to create DHCPv4 reservation for %s (parse error)", cd.get("ip_address"))
                messages.error(request, "Failed to create reservation: invalid response from Kea.")
        return self.render_add_form(request, server, form, options_formset, return_url)


class ServerReservation6AddView(_ReservationFormViewMixin, _KeaChangeMixin, generic.ObjectView):
    """Add a DHCPv6 host reservation."""

    queryset = Server.objects.all()
    dhcp_version = 6
    form_action = "Add"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render add form, optionally pre-filled from query parameters."""
        server = self.get_object(pk=pk)
        initial = {
            k: request.GET.get(k, "")
            for k in ("subnet_cidr", "ip_addresses", "prefixes", "identifier_type", "identifier", "hostname")
        }
        subnet_choices, subnet_cmds_available = fetch_subnet_choices(server, self.dhcp_version)
        if not initial.get("subnet_cidr"):
            # Legacy prefill links pass the raw subnet id; map it to the CIDR.
            initial["subnet_cidr"] = _legacy_subnet_cidr(subnet_choices, request.GET.get("subnet_id", ""))
        initial = {k: v for k, v in initial.items() if v}
        return self.render_form(
            request,
            server,
            forms.Reservation6Form(initial=initial),
            forms.ReservationOptionsFormSet(prefix="options"),
            reverse("plugins:netbox_kea:server_reservations6", args=[pk]),
            subnet_choices,
            subnet_cmds_available,
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate form and create DHCPv6 reservation in Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation6Form(data=request.POST)
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        if form.is_valid() and options_valid:
            cd = form.cleaned_data

            def _rerender() -> HttpResponse:
                return self.render_add_form(request, server, form, options_formset, return_url)

            try:
                client = server.get_client(version=6)
            except ValueError:
                logger.exception("Failed to create DHCPv6 client for server %s", server.pk)
                messages.error(request, "Failed to connect to Kea: see server logs.")
                return _rerender()
            subnet_id = self.resolve_subnet_id(request, client, form, cd["subnet_cidr"])
            if subnet_id is None:
                return _rerender()
            reservation: dict[str, Any] = {
                "subnet-id": subnet_id,
                cd["identifier_type"]: cd["identifier"],
            }
            # Omit empty collections rather than sending them: a DHCPv6 host may
            # reserve only delegated prefixes, or only a hostname and options.
            _apply_v6_addresses_and_prefixes(reservation, cd)
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            option_data = [
                {"name": f["name"], "data": f["data"], **({"always-send": True} if f.get("always_send") else {})}
                for f in (getattr(options_formset, "cleaned_data", []) or [])
                if f and f.get("name") and not f.get("DELETE")
            ]
            if option_data:
                reservation["option-data"] = option_data
            # Advisory warning when any reservation IP is inside an existing pool (non-fatal)
            try:
                for ip_str in reservation.get("ip-addresses") or []:
                    if ip_str:
                        _warn_reservation_pool_overlap(request, client, 6, subnet_id, ip_str)
            except Exception:  # noqa: BLE001
                logger.debug("Pool overlap check failed for v6 reservation", exc_info=True)
            try:
                client.reservation_add("dhcp6", reservation)
                messages.success(request, "DHCPv6 reservation created.")
                _run_reservation_success_side_effects(
                    request, server, reservation, 6, "created", bool(cd.get("sync_to_netbox"))
                )
                return redirect(return_url)
            except PartialPersistError:
                _run_reservation_success_side_effects(
                    request, server, reservation, 6, "created", bool(cd.get("sync_to_netbox")), partial_persist=True
                )
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to create DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, kea_error_hint(exc))
            except requests.RequestException:
                logger.exception("Failed to create DHCPv6 reservation for %s (network error)", cd.get("ip_addresses"))
                messages.error(request, "Network error communicating with Kea: see server logs.")
            except ValueError:
                logger.exception("Failed to create DHCPv6 reservation for %s (parse error)", cd.get("ip_addresses"))
                messages.error(request, "Failed to create reservation: invalid response from Kea.")
        return self.render_add_form(request, server, form, options_formset, return_url)


class ServerReservation4EditView(
    _ReservationFormViewMixin, _ReservationLookupMixin, _KeaChangeMixin, generic.ObjectView
):
    """Edit an existing DHCPv4 host reservation, addressed by IP."""

    queryset = Server.objects.all()
    dhcp_version = 4
    form_action = "Edit"
    immutable_fields = frozenset({"subnet_cidr", "ip_address", "identifier_type", "identifier"})

    def _get_reservation(self, server: Server, lookup: _ReservationLookup) -> dict | None:
        client = server.get_client(version=4)
        return client.reservation_get("dhcp4", **lookup.to_kwargs())

    def get(self, request: HttpRequest, pk: int, subnet_id: int, **kwargs) -> HttpResponse:
        """Pre-populate form with existing reservation data."""
        server = self.get_object(pk=pk)
        lookup = self.get_lookup(request, subnet_id, **kwargs)
        ip_address = lookup.ip_address
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        try:
            reservation = self._get_reservation(server, lookup)
        except KeaException as exc:
            logger.exception("Failed to fetch DHCPv4 reservation %s in subnet %s", lookup.label, subnet_id)
            messages.error(request, kea_error_hint(exc))
            return redirect(return_url)
        except (requests.RequestException, ValueError):
            logger.exception("Failed to fetch DHCPv4 reservation %s in subnet %s", lookup.label, subnet_id)
            messages.error(request, "Failed to retrieve reservation: see server logs for details.")
            return redirect(return_url)
        if reservation is None:
            raise Http404(f"Reservation {lookup.label} not found in subnet {subnet_id}")
        identifier_type, identifier = _get_reservation_identifier(reservation, 4)
        try:
            subnet_cidr = server.get_client(version=4).get_subnet_cidr(4, subnet_id)
        except (KeaException, requests.RequestException, ValueError, RuntimeError):
            logger.debug("Could not resolve subnet CIDR for id=%s; falling back to raw id", subnet_id, exc_info=True)
            subnet_cidr = str(subnet_id)
        initial = {
            "subnet_cidr": subnet_cidr,
            "ip_address": reservation.get("ip-address", ip_address),
            "identifier_type": identifier_type,
            "identifier": identifier,
            "hostname": reservation.get("hostname", ""),
        }
        existing_options = reservation.get("option-data", [])
        if not isinstance(existing_options, list):
            existing_options = []
        options_initial = [
            {"name": o.get("name", ""), "data": o.get("data", ""), "always_send": o.get("always-send", False)}
            for o in existing_options
            if isinstance(o, dict)
        ]
        context = self.form_context(
            server,
            forms.Reservation4Form(initial=initial),
            forms.ReservationOptionsFormSet(initial=options_initial, prefix="options"),
            return_url,
        )
        self.apply_immutable_fields(context["form"])
        if ip_address:
            try:
                lease = server.get_client(version=4).lease_get_by_ip(4, ip_address)
                if lease and lease.get("hostname") and lease.get("hostname") != reservation.get("hostname", ""):
                    context["lease_diff"] = {"hostname": lease["hostname"]}
            except (KeaException, requests.RequestException, ValueError):
                logger.debug("Could not fetch lease for reservation edit diff (ip=%s)", ip_address, exc_info=True)
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, pk: int, subnet_id: int, **kwargs) -> HttpResponse:
        """Validate and submit updated reservation to Kea."""
        server = self.get_object(pk=pk)
        lookup = self.get_lookup(request, subnet_id, **kwargs)
        ip_address = lookup.ip_address
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        # Fetch existing before form construction so identifier fields can be seeded and disabled,
        # preventing browser-omitted disabled fields from failing form validation.
        try:
            existing = self._get_reservation(server, lookup)
        except (KeaException, requests.RequestException, ValueError):
            logger.exception("Could not fetch existing DHCPv4 reservation for edit (%s)", lookup.label)
            messages.error(request, "Failed to reload the existing reservation. Edit aborted.")
            return redirect(return_url)
        if existing is None:
            messages.error(request, f"Reservation {lookup.label} no longer exists in subnet {subnet_id}.")
            return redirect(return_url)
        existing_id_type, existing_id_value = _get_reservation_identifier(existing, 4)

        def _build_form(subnet_cidr: str) -> forms.Reservation4Form:
            form = forms.Reservation4Form(
                data=request.POST,
                initial={
                    "subnet_cidr": subnet_cidr,
                    "ip_address": existing.get("ip-address", ip_address),
                    "identifier_type": existing_id_type,
                    "identifier": existing_id_value,
                },
            )
            # Django takes a disabled field's value from initial, so seeding must precede this.
            self.apply_immutable_fields(form)
            return form

        # subnet_cidr is disabled, so validation never uses this value — a real Kea
        # lookup is only worth its cost if the form ends up re-rendered below. The form
        # must then be rebuilt: BoundField.initial is a cached_property that validation
        # already read, so mutating form.initial afterwards does not reach the widget.
        form = _build_form(str(subnet_id))
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        if form.is_valid() and options_valid:
            cd = form.cleaned_data
            # Start from existing data and overwrite user-editable fields (merge not replace).
            reservation: dict[str, Any] = dict(existing)
            reservation["subnet-id"] = subnet_id
            # Disabled fields resolve to their initial value, so this is the URL-derived
            # address on the IP-keyed route and the submitted one where it is editable.
            if cd.get("ip_address"):
                reservation["ip-address"] = cd["ip_address"]
            else:
                reservation.pop("ip-address", None)
            # Replace identifier — remove all known identifier keys first.
            for _id_key in _ALL_IDENTIFIER_KEYS:
                reservation.pop(_id_key, None)
            reservation[cd["identifier_type"]] = cd["identifier"]
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            else:
                reservation.pop("hostname", None)
            _merge_reservation_option_data(reservation, options_formset)
            try:
                client = server.get_client(version=4)
                client.reservation_update("dhcp4", reservation)
                messages.success(request, f"Reservation for {lookup.label} updated.")
                _run_reservation_success_side_effects(
                    request, server, reservation, 4, "updated", bool(cd.get("sync_to_netbox"))
                )
                return redirect(return_url)
            except PartialPersistError:
                _run_reservation_success_side_effects(
                    request, server, reservation, 4, "updated", bool(cd.get("sync_to_netbox")), partial_persist=True
                )
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to update DHCPv4 reservation for %s", lookup.label)
                messages.error(request, kea_error_hint(exc))
            except requests.RequestException:
                logger.exception("Network error updating DHCPv4 reservation for %s", lookup.label)
                messages.error(request, "Network error communicating with Kea: see server logs.")
            except ValueError:
                logger.exception("Invalid Kea response when updating DHCPv4 reservation for %s", lookup.label)
                messages.error(request, "Invalid response from Kea: see server logs.")
        # Re-rendering: replace the placeholder with the real CIDR the user should see.
        try:
            subnet_cidr = server.get_client(version=4).get_subnet_cidr(4, subnet_id)
        except (KeaException, requests.RequestException, ValueError, RuntimeError):
            logger.debug("Could not resolve subnet CIDR for id=%s; falling back to raw id", subnet_id, exc_info=True)
            subnet_cidr = str(subnet_id)
        form = _build_form(subnet_cidr)
        return self.render_form(request, server, form, options_formset, return_url)


class ServerReservation6EditView(
    _ReservationFormViewMixin, _ReservationLookupMixin, _KeaChangeMixin, generic.ObjectView
):
    """Edit an existing DHCPv6 host reservation, addressed by IP."""

    queryset = Server.objects.all()
    dhcp_version = 6
    form_action = "Edit"
    immutable_fields = frozenset({"subnet_cidr", "ip_addresses", "identifier_type", "identifier"})

    def _get_reservation(self, server: Server, lookup: _ReservationLookup) -> dict | None:
        client = server.get_client(version=6)
        return client.reservation_get("dhcp6", **lookup.to_kwargs())

    def get(self, request: HttpRequest, pk: int, subnet_id: int, **kwargs) -> HttpResponse:
        """Pre-populate form with existing DHCPv6 reservation data."""
        server = self.get_object(pk=pk)
        lookup = self.get_lookup(request, subnet_id, **kwargs)
        ip_address = lookup.ip_address
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        try:
            reservation = self._get_reservation(server, lookup)
        except KeaException as exc:
            logger.exception("Failed to fetch DHCPv6 reservation %s in subnet %s", lookup.label, subnet_id)
            messages.error(request, kea_error_hint(exc))
            return redirect(return_url)
        except (requests.RequestException, ValueError):
            logger.exception("Failed to fetch DHCPv6 reservation %s in subnet %s", lookup.label, subnet_id)
            messages.error(request, "Failed to retrieve reservation: see server logs for details.")
            return redirect(return_url)
        if reservation is None:
            raise Http404(f"Reservation {lookup.label} not found in subnet {subnet_id}")
        identifier_type, identifier = _get_reservation_identifier(reservation, 6)
        raw_ip_list = reservation.get("ip-addresses")
        if isinstance(raw_ip_list, list):
            ip_list = [ip for ip in raw_ip_list if isinstance(ip, str) and ip]
        elif isinstance(raw_ip_list, str) and raw_ip_list:
            ip_list = [raw_ip_list]
        else:
            # A prefix-delegation-only reservation has no addresses at all.
            ip_list = [ip_address] if ip_address else []
        try:
            subnet_cidr = server.get_client(version=6).get_subnet_cidr(6, subnet_id)
        except (KeaException, requests.RequestException, ValueError, RuntimeError):
            logger.debug("Could not resolve subnet CIDR for id=%s; falling back to raw id", subnet_id, exc_info=True)
            subnet_cidr = str(subnet_id)
        initial = {
            "subnet_cidr": subnet_cidr,
            "ip_addresses": ",".join(ip_list),
            "prefixes": ",".join(_reservation_prefix_list(reservation)),
            "identifier_type": identifier_type,
            "identifier": identifier,
            "hostname": reservation.get("hostname", ""),
        }
        existing_options = reservation.get("option-data", [])
        if not isinstance(existing_options, list):
            existing_options = []
        options_initial = [
            {"name": o.get("name", ""), "data": o.get("data", ""), "always_send": o.get("always-send", False)}
            for o in existing_options
            if isinstance(o, dict)
        ]
        context = self.form_context(
            server,
            forms.Reservation6Form(initial=initial),
            forms.ReservationOptionsFormSet(initial=options_initial, prefix="options"),
            return_url,
        )
        self.apply_immutable_fields(context["form"])
        if ip_address:
            try:
                lease = server.get_client(version=6).lease_get_by_ip(6, ip_address)
                if lease and lease.get("hostname") and lease.get("hostname") != reservation.get("hostname", ""):
                    context["lease_diff"] = {"hostname": lease["hostname"]}
            except (KeaException, requests.RequestException, ValueError):
                logger.debug("Could not fetch lease for reservation edit diff (ip=%s)", ip_address, exc_info=True)
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, pk: int, subnet_id: int, **kwargs) -> HttpResponse:
        """Validate and submit updated DHCPv6 reservation to Kea."""
        server = self.get_object(pk=pk)
        lookup = self.get_lookup(request, subnet_id, **kwargs)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        # Fetch existing reservation before form construction (#51) so ip_addresses initial is
        # accurate on re-render, and to enable merge-not-replace for all reservation keys (#52).
        try:
            existing = self._get_reservation(server, lookup)
        except (KeaException, requests.RequestException, ValueError):
            logger.exception("Could not fetch existing DHCPv6 reservation for edit (%s)", lookup.label)
            messages.error(
                request, "Failed to reload the existing DHCPv6 reservation. Edit aborted to prevent IP loss."
            )
            return redirect(return_url)
        if existing is None:
            messages.error(request, f"Reservation {lookup.label} no longer exists in subnet {subnet_id}.")
            return redirect(return_url)
        raw_existing_ips = existing.get("ip-addresses")
        if isinstance(raw_existing_ips, list):
            existing_ips = [ip for ip in raw_existing_ips if isinstance(ip, str) and ip]
        elif isinstance(raw_existing_ips, str) and raw_existing_ips:
            existing_ips = [raw_existing_ips]
        else:
            existing_ips = []
        # An IP-keyed edit that reloads no addresses means the reload lost them, and
        # writing that back would delete the reservation's IPs.  An identifier-keyed
        # edit of a prefix-delegation-only host legitimately has none.
        if not existing_ips and not lookup.by_identifier:
            messages.error(
                request, "Failed to reload the existing DHCPv6 reservation. Edit aborted to prevent IP loss."
            )
            return redirect(return_url)
        existing_id_type, existing_id_value = _get_reservation_identifier(existing, 6)

        def _build_form(subnet_cidr: str) -> forms.Reservation6Form:
            form = forms.Reservation6Form(
                data=request.POST,
                initial={
                    "subnet_cidr": subnet_cidr,
                    "ip_addresses": ",".join(existing_ips),
                    "prefixes": ",".join(_reservation_prefix_list(existing)),
                    "identifier_type": existing_id_type,
                    "identifier": existing_id_value,
                },
            )
            # Django takes a disabled field's value from initial, so seeding must precede this.
            self.apply_immutable_fields(form)
            return form

        # subnet_cidr is disabled, so validation never uses this value — a real Kea
        # lookup is only worth its cost if the form ends up re-rendered below. The form
        # must then be rebuilt: BoundField.initial is a cached_property that validation
        # already read, so mutating form.initial afterwards does not reach the widget.
        form = _build_form(str(subnet_id))
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        if form.is_valid() and options_valid:
            cd = form.cleaned_data
            # Start from existing data and overwrite user-editable fields (merge not replace #52).
            reservation: dict[str, Any] = dict(existing)
            reservation["subnet-id"] = subnet_id
            _apply_v6_addresses_and_prefixes(reservation, cd)
            # Replace identifier — remove all known identifier keys first.
            for _id_key in _ALL_IDENTIFIER_KEYS:
                reservation.pop(_id_key, None)
            reservation[cd["identifier_type"]] = cd["identifier"]
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            else:
                reservation.pop("hostname", None)
            _merge_reservation_option_data(reservation, options_formset)
            try:
                client = server.get_client(version=6)
                client.reservation_update("dhcp6", reservation)
                messages.success(request, "DHCPv6 reservation updated.")
                _run_reservation_success_side_effects(
                    request, server, reservation, 6, "updated", bool(cd.get("sync_to_netbox"))
                )
                return redirect(return_url)
            except PartialPersistError:
                _run_reservation_success_side_effects(
                    request, server, reservation, 6, "updated", bool(cd.get("sync_to_netbox")), partial_persist=True
                )
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to update DHCPv6 reservation for %s", lookup.label)
                messages.error(request, kea_error_hint(exc))
            except requests.RequestException:
                logger.exception("Network error updating DHCPv6 reservation for %s", lookup.label)
                messages.error(request, "Network error communicating with Kea: see server logs.")
            except ValueError:
                logger.exception("Invalid Kea response when updating DHCPv6 reservation for %s", lookup.label)
                messages.error(request, "Invalid response from Kea: see server logs.")
        # Re-rendering: replace the placeholder with the real CIDR the user should see.
        try:
            subnet_cidr = server.get_client(version=6).get_subnet_cidr(6, subnet_id)
        except (KeaException, requests.RequestException, ValueError, RuntimeError):
            logger.debug("Could not resolve subnet CIDR for id=%s; falling back to raw id", subnet_id, exc_info=True)
            subnet_cidr = str(subnet_id)
        form = _build_form(subnet_cidr)
        return self.render_form(request, server, form, options_formset, return_url)


class ServerReservation4DeleteView(_ReservationLookupMixin, _KeaChangeMixin, generic.ObjectView):
    """Delete confirmation for a DHCPv4 host reservation, addressed by IP."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_delete.html"
    tab = _RESERVATIONS_TAB
    dhcp_version = 4

    def get(self, request: HttpRequest, pk: int, subnet_id: int, **kwargs) -> HttpResponse:
        """Show deletion confirmation page."""
        server = self.get_object(pk=pk)
        lookup = self.get_lookup(request, subnet_id, **kwargs)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "ip_address": lookup.ip_address,
                "reservation_label": lookup.label,
                "subnet_id": subnet_id,
                "dhcp_version": 4,
                "return_url": reverse("plugins:netbox_kea:server_reservations4", args=[pk]),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, **kwargs) -> HttpResponse:
        """Issue reservation-del to Kea and redirect."""
        server = self.get_object(pk=pk)
        lookup = self.get_lookup(request, subnet_id, **kwargs)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        try:
            client = server.get_client(version=4)
            client.reservation_del("dhcp4", **lookup.to_kwargs())
            messages.success(request, f"Reservation for {lookup.label} deleted.")
            _add_reservation_journal(server, request.user, "deleted", _deleted_journal_entry(lookup))
            _send_reservation_deleted(server, lookup, 4, request)
        except PartialPersistError:
            _add_reservation_journal(server, request.user, "deleted", _deleted_journal_entry(lookup))
            _send_reservation_deleted(server, lookup, 4, request)
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.exception("Failed to delete DHCPv4 reservation for %s", lookup.label)
            messages.error(request, kea_error_hint(exc))
        except requests.RequestException:
            logger.exception("Network error deleting DHCPv4 reservation for %s", lookup.label)
            messages.error(request, "Network error communicating with Kea: see server logs.")
        except ValueError:
            logger.exception("Invalid Kea response when deleting DHCPv4 reservation for %s", lookup.label)
            messages.error(request, "Invalid response from Kea: see server logs.")
        return redirect(return_url)


class ServerReservation6DeleteView(_ReservationLookupMixin, _KeaChangeMixin, generic.ObjectView):
    """Delete confirmation for a DHCPv6 host reservation, addressed by IP."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_delete.html"
    tab = _RESERVATIONS_TAB
    dhcp_version = 6

    def get(self, request: HttpRequest, pk: int, subnet_id: int, **kwargs) -> HttpResponse:
        """Show deletion confirmation page."""
        server = self.get_object(pk=pk)
        lookup = self.get_lookup(request, subnet_id, **kwargs)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "ip_address": lookup.ip_address,
                "reservation_label": lookup.label,
                "subnet_id": subnet_id,
                "dhcp_version": 6,
                "return_url": reverse("plugins:netbox_kea:server_reservations6", args=[pk]),
                "tab": self.tab,
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, **kwargs) -> HttpResponse:
        """Issue reservation-del to Kea and redirect."""
        server = self.get_object(pk=pk)
        lookup = self.get_lookup(request, subnet_id, **kwargs)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        try:
            client = server.get_client(version=6)
            client.reservation_del("dhcp6", **lookup.to_kwargs())
            messages.success(request, f"DHCPv6 reservation for {lookup.label} deleted.")
            _add_reservation_journal(server, request.user, "deleted", _deleted_journal_entry(lookup))
            _send_reservation_deleted(server, lookup, 6, request)
        except PartialPersistError:
            _add_reservation_journal(server, request.user, "deleted", _deleted_journal_entry(lookup))
            _send_reservation_deleted(server, lookup, 6, request)
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.exception("Failed to delete DHCPv6 reservation for %s", lookup.label)
            messages.error(request, kea_error_hint(exc))
        except requests.RequestException:
            logger.exception("Network error deleting DHCPv6 reservation for %s", lookup.label)
            messages.error(request, "Network error communicating with Kea: see server logs.")
        except ValueError:
            logger.exception("Invalid Kea response when deleting DHCPv6 reservation for %s", lookup.label)
            messages.error(request, "Invalid response from Kea: see server logs.")
        return redirect(return_url)


# ---------------------------------------------------------------------------
# Identifier-keyed variants
# ---------------------------------------------------------------------------
# A reservation that reserves no address cannot be addressed by one.  These routes
# key on the client identifier instead, which arrives as a query parameter so the
# URL reverses from integers alone.  The address stays editable here: Kea's
# reservation-update replaces the whole host keyed by its identifier, so a host can
# be given a fixed address, or have one removed, without changing its URL.


class ServerReservation4EditByIdentifierView(ServerReservation4EditView):
    """Edit a DHCPv4 host reservation addressed by its client identifier."""

    lookup_by_identifier = True
    immutable_fields = frozenset({"subnet_cidr", "identifier_type", "identifier"})


class ServerReservation6EditByIdentifierView(ServerReservation6EditView):
    """Edit a DHCPv6 host reservation addressed by its client identifier."""

    lookup_by_identifier = True
    immutable_fields = frozenset({"subnet_cidr", "identifier_type", "identifier"})


class ServerReservation4DeleteByIdentifierView(ServerReservation4DeleteView):
    """Delete a DHCPv4 host reservation addressed by its client identifier."""

    lookup_by_identifier = True


class ServerReservation6DeleteByIdentifierView(ServerReservation6DeleteView):
    """Delete a DHCPv6 host reservation addressed by its client identifier."""

    lookup_by_identifier = True


def _get_reservation_identifier(
    reservation: dict[str, Any],
    version: int,
) -> tuple[str, str]:
    """Extract the identifier type and value from a Kea reservation dict.

    Args:
        reservation: Kea reservation dict (from ``reservation-get``).
        version: DHCP version (4 or 6) to determine identifier priority order.

    Returns:
        ``(identifier_type, identifier_value)`` tuple.

    """
    priority = _V6_IDENTIFIER_TYPES if version == 6 else _V4_IDENTIFIER_TYPES
    for itype in priority:
        if reservation.get(itype):
            return itype, reservation[itype]
    return "hw-address", ""


def _normalise_reservation_identifier(reservation: dict[str, Any], version: int) -> None:
    """In-place: expose the reservation's identifier as ``identifier_type``/``identifier``.

    Each table had one hard-coded identifier column — ``hw-address`` for DHCPv4,
    ``duid`` for DHCPv6 — so a host identified any other way Kea allows (client-id,
    circuit-id, flex-id, remote-id) rendered as a blank cell.  Both keys are empty
    when the reservation carries no identifier at all.
    """
    identifier_type, identifier = _get_reservation_identifier(reservation, version)
    reservation["identifier"] = identifier
    reservation["identifier_type"] = identifier_type if identifier else ""


def _reservation_prefix_list(reservation: dict[str, Any]) -> list[str]:
    """Return a DHCPv6 reservation's delegated ``prefixes`` as a list of strings.

    Mirrors the ``ip-addresses`` coercion: Kea sends a list, but a hand-written
    configuration can carry a bare string.
    """
    raw = reservation.get("prefixes")
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, str) and p]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _normalise_reservation_prefixes(reservation: dict[str, Any]) -> None:
    """In-place counterpart of :func:`_reservation_prefix_list` for table rows.

    A reservation that only delegates prefixes reserves nothing else, so this is the
    only thing its row can show.
    """
    reservation["prefixes"] = _reservation_prefix_list(reservation)


def _attach_reservation_action_urls(
    reservations: list[dict[str, Any]],
    server_pk: int,
    version: int,
    *,
    can_change: bool,
) -> None:
    """In-place: set ``edit_url`` / ``delete_url`` on each reservation row.

    These URLs are built here rather than with ``{% url %}`` in the table's actions
    column on purpose.  Kea omits ``ip-address`` for a reservation that reserves no
    address (an identifier-only host, or a DHCPv6 prefix-delegation-only host), which
    normalises to ``ip_address=""``.  The address-keyed route takes ``<str:ip_address>``
    (``[^/]+``) and cannot reverse an empty string, so the template tag raised
    ``NoReverseMatch`` and a single such row 500'd the whole table (issue #110).  In
    Python the guard is an ordinary conditional and a row that cannot be addressed
    simply gets no action buttons.

    Both keys are always assigned: these dicts come straight from Kea and are mutated
    in place, so leaving them unset would let an unrelated value reach the template.
    *server_pk* is passed by the caller rather than read from the row so the combined
    (multi-server) view cannot mint a URL pointing at the wrong server.

    The reversal is attempted rather than pre-validated: the route converters
    (``<int:subnet_id>``, ``<str:ip_address>``) are the authority on what fits, and
    guessing at them in Python both rejects values they accept (a decimal *string*
    subnet id reverses fine) and misses ones they reject (a negative subnet id, an
    address containing ``/``). A row whose values do not fit the route loses its
    buttons instead of taking the page down with it.
    """
    for r in reservations:
        r["edit_url"] = None
        r["delete_url"] = None
        if not can_change:
            continue
        subnet_id = r.get("subnet_id", r.get("subnet-id"))
        ip_address = r.get("ip_address") or ""
        try:
            if ip_address:
                args = [server_pk, subnet_id, ip_address]
                edit_url = reverse(f"plugins:netbox_kea:server_reservation{version}_edit", args=args)
                delete_url = reverse(f"plugins:netbox_kea:server_reservation{version}_delete", args=args)
            else:
                # No address to key on — address by client identifier instead.
                identifier_type = r.get("identifier_type") or ""
                identifier = r.get("identifier") or ""
                if not identifier:
                    continue
                query = urlencode({"identifier_type": identifier_type, "identifier": identifier})
                args = [server_pk, subnet_id]
                edit_url = f"{reverse(f'plugins:netbox_kea:server_reservation{version}_edit_by_identifier', args=args)}?{query}"
                delete_url = (
                    f"{reverse(f'plugins:netbox_kea:server_reservation{version}_delete_by_identifier', args=args)}"
                    f"?{query}"
                )
        except NoReverseMatch:
            logger.debug("No reservation action URL for subnet-id=%r ip=%r", subnet_id, ip_address)
            continue
        r["edit_url"] = edit_url
        r["delete_url"] = delete_url


def _enrich_reservations_with_badges(
    reservations: list[dict[str, Any]], server: "Server", version: int, can_change: bool = False
) -> None:
    """In-place: add active-lease status and NetBox IPAM badge fields to reservation dicts.

    Adds:
    - ``has_active_lease``: True/False (None if lease_cmds unavailable)
    - ``netbox_ip_url``: absolute URL if IP exists in NetBox IPAM
    - ``sync_url``: POST endpoint URL to create a NetBox IP when absent
    """
    from ..sync import bulk_fetch_netbox_ips

    try:
        client = server.get_client(version=version)
        _enrich_reservations_with_lease_status(client, reservations, version=version)
    except (KeaException, requests.RequestException, ValueError, TimeoutError):
        logger.debug("Failed to enrich reservations with lease status for server %s", server.pk, exc_info=True)

    sync_url = reverse(f"plugins:netbox_kea:server_reservation{version}_sync", args=[server.pk])
    # Build lookup list including extra IPs (IPv6 reservations may have multiple addresses).
    all_lookup_ips: list[str] = []
    for r in reservations:
        primary = r.get("ip_address", "")
        if primary:
            all_lookup_ips.append(primary)
        all_lookup_ips.extend(ip for ip in (r.get("extra_ips") or []) if ip)
    nb_ips = bulk_fetch_netbox_ips(all_lookup_ips)

    for r in reservations:
        candidate_ips = [r.get("ip_address", "")] + list(r.get("extra_ips") or [])
        candidate_ips = [ip for ip in candidate_ips if ip]
        matched = [nb_ips[ip] for ip in candidate_ips if ip in nb_ips]
        if len(matched) == len(candidate_ips) and matched:
            # All IPs synced — show Synced badge with first match URL.
            r["netbox_ip_url"] = matched[0].get_absolute_url()
        elif matched:
            # Partial sync — some IPs exist, some don't.
            r["netbox_ip_url"] = matched[0].get_absolute_url()
            if can_change:
                r["sync_url"] = sync_url
        elif can_change and candidate_ips:
            r["sync_url"] = sync_url

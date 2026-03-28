import concurrent.futures
import logging
from typing import Any

from django.contrib import messages
from django.db.utils import OperationalError, ProgrammingError
from django.http import Http404, HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from netbox.views import generic
from utilities.views import register_model_view

from .. import forms, tables
from ..kea import KeaClient, KeaException, PartialPersistError
from ..models import Server
from ..signals import reservation_created, reservation_deleted, reservation_updated
from ..sync import sync_reservation_to_netbox
from ..utilities import (
    OptionalViewTab,
    _enrich_reservation_sort_key,
    kea_error_hint,
)
from ._base import _KeaChangeMixin
from .subnets import _warn_reservation_pool_overlap

logger = logging.getLogger(__name__)


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
        mac = reservation.get("hw-address") or reservation.get("hw_address", "")
        duid = reservation.get("duid", "")
        identifier = mac or duid
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


def _enrich_reservations_with_lease_status(client: "KeaClient", reservations: list[dict], version: int) -> None:
    """Enrich each reservation dict with ``has_active_lease`` (bool | None).

    Queries ``lease4-get-all`` / ``lease6-get-all`` per unique subnet to find
    active leases.  Sets ``r["has_active_lease"] = True/False`` for each
    reservation.  Leaves ``has_active_lease`` unset (None) if the ``lease_cmds``
    hook is unavailable or an unexpected error occurs, so the template can
    distinguish "unknown" from "no lease".

    Args:
        client: Connected KeaClient for the server.
        reservations: List of reservation dicts (mutated in-place).
        version: DHCP version (4 or 6).

    """
    if not reservations:
        return

    service = f"dhcp{version}"
    lease_cmd = f"lease{version}-get-all"
    unique_subnet_ids = {r.get("subnet-id") for r in reservations if r.get("subnet-id")}

    active_lease_ips: set[str] = set()
    hook_unavailable = False

    def _fetch_leases_for_subnet(sid: int) -> list[str] | None:
        """Return list of lease IPs, or None if the lease_cmds hook is not loaded."""
        worker_client = client.clone()  # requests.Session is not thread-safe
        try:
            resp = worker_client.command(
                lease_cmd,
                service=[service],
                arguments={"subnets": [sid]},
                check=(0, 3),
            )
            if resp[0]["result"] != 3:
                args = resp[0].get("arguments", {})
                return [lease.get("ip-address", "") for lease in args.get("leases", [])]
            return []
        except KeaException as exc:
            if exc.response.get("result") == 2:
                return None  # hook not loaded
            return []
        except Exception:  # noqa: BLE001
            return []

    if not unique_subnet_ids:
        return

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(unique_subnet_ids), 10)) as executor:
            futures = {executor.submit(_fetch_leases_for_subnet, sid): sid for sid in unique_subnet_ids}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is None:
                    hook_unavailable = True
                else:
                    for ip in result:
                        active_lease_ips.add(ip)
    except Exception:  # noqa: BLE001
        return

    if hook_unavailable:
        return

    for r in reservations:
        ip = r.get("ip-address", r.get("ip_address", ""))
        r["has_active_lease"] = ip in active_lease_ips


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
        if version == 4:
            result = [
                r
                for r in result
                if q_lower in r.get("ip_address", r.get("ip-address", "")).lower()
                or q_lower in r.get("hostname", "").lower()
                or q_lower in r.get("hw-address", "").lower()
            ]
        else:
            result = [
                r
                for r in result
                if q_lower in r.get("ip_address", "").lower()
                or any(q_lower in ip.lower() for ip in r.get("ip-addresses", []))
                or q_lower in r.get("hostname", "").lower()
                or q_lower in r.get("duid", "").lower()
            ]
    return result


@register_model_view(Server, "reservations4")
class ServerReservations4View(generic.ObjectView):
    """DHCPv4 reservations tab — lists all reservations from host_cmds hook."""

    queryset = Server.objects.all()
    tab = OptionalViewTab(label="DHCPv4 Reservations", weight=1040, is_enabled=lambda s: s.dhcp4)
    template_name = "netbox_kea/server_reservations.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Fetch reservations from Kea, apply search filters, and build the table."""
        server: Server = instance
        client = server.get_client(version=4)
        hook_available = True
        reservations: list[dict] = []
        try:
            source_index, from_index, limit = 0, 0, 100
            while True:
                page, next_from, next_source = client.reservation_get_page(
                    "dhcp4", source_index=source_index, from_index=from_index, limit=limit
                )
                reservations.extend(page)
                if next_from == 0 and next_source == 0:
                    break
                from_index = next_from
                source_index = next_source
        except KeaException as exc:
            hook_available = False
            if exc.response.get("result") != 2:
                logger.warning("Failed to fetch DHCPv4 reservations: %s", exc)
        except Exception:
            hook_available = False
            logger.debug("Unexpected error fetching DHCPv4 reservations", exc_info=True)

        # Inject server_pk so the actions template column can build edit/delete URLs.
        for r in reservations:
            r["server_pk"] = server.pk
            r.setdefault("ip_address", r.get("ip-address", ""))
            r.setdefault("subnet_id", r.get("subnet-id", 0))
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

        # Enrich reservations with lease status + NetBox IPAM badges.
        _enrich_reservations_with_badges(reservations, server, 4)

        can_change = Server.objects.restrict(request.user, "change").filter(pk=server.pk).exists()
        for r in reservations:
            r["can_change"] = can_change

        table = tables.ReservationTable4(reservations, user=request.user)
        table.configure(request)
        return {
            "table": table,
            "dhcp_version": 4,
            "hook_available": hook_available,
            "search_form": search_form,
            "add_url": reverse("plugins:netbox_kea:server_reservation4_add", args=[server.pk]),
            "bulk_sync_url": reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[server.pk]),
            "import_url": reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[server.pk]),
        }


@register_model_view(Server, "reservations6")
class ServerReservations6View(generic.ObjectView):
    """DHCPv6 reservations tab — lists all reservations from host_cmds hook."""

    queryset = Server.objects.all()
    tab = OptionalViewTab(label="DHCPv6 Reservations", weight=1045, is_enabled=lambda s: s.dhcp6)
    template_name = "netbox_kea/server_reservations.html"

    def get_extra_context(self, request: HttpRequest, instance: Server) -> dict[str, Any]:
        """Fetch DHCPv6 reservations from Kea, apply search filters, and build the table."""
        server: Server = instance
        client = server.get_client(version=6)
        hook_available = True
        reservations: list[dict] = []
        try:
            source_index, from_index, limit = 0, 0, 100
            while True:
                page, next_from, next_source = client.reservation_get_page(
                    "dhcp6", source_index=source_index, from_index=from_index, limit=limit
                )
                reservations.extend(page)
                if next_from == 0 and next_source == 0:
                    break
                from_index = next_from
                source_index = next_source
        except KeaException as exc:
            hook_available = False
            if exc.response.get("result") != 2:
                logger.warning("Failed to fetch DHCPv6 reservations: %s", exc)
        except Exception:
            hook_available = False
            logger.debug("Unexpected error fetching DHCPv6 reservations", exc_info=True)

        for r in reservations:
            r["server_pk"] = server.pk
            r.setdefault("ip_address", (r.get("ip-addresses") or [""])[0])
            r.setdefault("subnet_id", r.get("subnet-id", 0))
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

        # Enrich reservations with lease status + NetBox IPAM badges.
        _enrich_reservations_with_badges(reservations, server, 6)

        can_change = Server.objects.restrict(request.user, "change").filter(pk=server.pk).exists()
        for r in reservations:
            r["can_change"] = can_change

        table = tables.ReservationTable6(reservations, user=request.user)
        table.configure(request)
        return {
            "table": table,
            "dhcp_version": 6,
            "hook_available": hook_available,
            "search_form": search_form,
            "add_url": reverse("plugins:netbox_kea:server_reservation6_add", args=[server.pk]),
            "bulk_sync_url": reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[server.pk]),
            "import_url": reverse("plugins:netbox_kea:server_reservation6_bulk_import", args=[server.pk]),
        }


class ServerReservation4AddView(_KeaChangeMixin, generic.ObjectView):
    """Add a DHCPv4 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_form.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render add form, optionally pre-filled from query parameters."""
        server = self.get_object(pk=pk)
        initial = {
            k: request.GET.get(k, "") for k in ("subnet_id", "ip_address", "identifier_type", "identifier", "hostname")
        }
        initial = {k: v for k, v in initial.items() if v}
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": forms.Reservation4Form(initial=initial),
                "options_formset": forms.ReservationOptionsFormSet(prefix="options"),
                "dhcp_version": 4,
                "action": "Add",
                "return_url": reverse("plugins:netbox_kea:server_reservations4", args=[pk]),
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate form and create reservation in Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation4Form(data=request.POST)
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        if form.is_valid() and options_valid:
            cd = form.cleaned_data
            reservation = {
                "subnet-id": cd["subnet_id"],
                "ip-address": cd["ip_address"],
                cd["identifier_type"]: cd["identifier"],
            }
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            option_data = [
                {"name": f["name"], "data": f["data"], **({"always-send": True} if f.get("always_send") else {})}
                for f in (getattr(options_formset, "cleaned_data", []) or [])
                if f and f.get("name") and not f.get("DELETE")
            ]
            if option_data:
                reservation["option-data"] = option_data
            client = server.get_client(version=4)
            # F4: Warn (non-blocking) when the reservation IP is inside an existing pool
            _warn_reservation_pool_overlap(request, client, 4, cd["subnet_id"], cd["ip_address"])
            try:
                client.reservation_add("dhcp4", reservation)
                messages.success(request, f"Reservation for {cd['ip_address']} created.")
                _add_reservation_journal(server, request.user, "created", reservation)

                reservation_created.send_robust(
                    sender=None,
                    server=server,
                    reservation=reservation,
                    dhcp_version=4,
                    request=request,
                )
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {cd['ip_address']} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv4 reservation %s to NetBox", cd.get("ip_address"))
                        messages.warning(request, "Reservation created, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except PartialPersistError:
                _add_reservation_journal(server, request.user, "created", reservation)
                reservation_created.send_robust(
                    sender=None,
                    server=server,
                    reservation=reservation,
                    dhcp_version=4,
                    request=request,
                )
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {cd['ip_address']} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv4 reservation %s to NetBox", cd.get("ip_address"))
                        messages.warning(request, "Reservation created, but NetBox IPAM sync failed.")
                messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to create DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
                logger.exception("Failed to create DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, "Failed to create reservation: see server logs for details.")
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "options_formset": options_formset,
                "dhcp_version": 4,
                "action": "Add",
                "return_url": return_url,
            },
        )


class ServerReservation6AddView(_KeaChangeMixin, generic.ObjectView):
    """Add a DHCPv6 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_form.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Render add form, optionally pre-filled from query parameters."""
        server = self.get_object(pk=pk)
        initial = {
            k: request.GET.get(k, "")
            for k in ("subnet_id", "ip_addresses", "identifier_type", "identifier", "hostname")
        }
        initial = {k: v for k, v in initial.items() if v}
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": forms.Reservation6Form(initial=initial),
                "options_formset": forms.ReservationOptionsFormSet(prefix="options"),
                "dhcp_version": 6,
                "action": "Add",
                "return_url": reverse("plugins:netbox_kea:server_reservations6", args=[pk]),
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate form and create DHCPv6 reservation in Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation6Form(data=request.POST)
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        if form.is_valid() and options_valid:
            cd = form.cleaned_data
            reservation: dict[str, Any] = {
                "subnet-id": cd["subnet_id"],
                "ip-addresses": [ip.strip() for ip in cd["ip_addresses"].split(",")],
                cd["identifier_type"]: cd["identifier"],
            }
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            option_data = [
                {"name": f["name"], "data": f["data"], **({"always-send": True} if f.get("always_send") else {})}
                for f in (getattr(options_formset, "cleaned_data", []) or [])
                if f and f.get("name") and not f.get("DELETE")
            ]
            if option_data:
                reservation["option-data"] = option_data
            client = server.get_client(version=6)
            # F4: Warn (non-blocking) when any reservation IP is inside an existing pool
            for ip_str in reservation.get("ip-addresses") or []:
                if ip_str:
                    _warn_reservation_pool_overlap(request, client, 6, cd["subnet_id"], ip_str)
            try:
                client.reservation_add("dhcp6", reservation)
                messages.success(request, "DHCPv6 reservation created.")
                _add_reservation_journal(server, request.user, "created", reservation)

                reservation_created.send_robust(
                    sender=None,
                    server=server,
                    reservation=reservation,
                    dhcp_version=6,
                    request=request,
                )
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        primary_ip = (reservation.get("ip-addresses") or [""])[0]
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {primary_ip} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv6 reservation to NetBox")
                        messages.warning(request, "Reservation created, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except PartialPersistError:
                _add_reservation_journal(server, request.user, "created", reservation)
                reservation_created.send_robust(
                    sender=None,
                    server=server,
                    reservation=reservation,
                    dhcp_version=6,
                    request=request,
                )
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        primary_ip = (reservation.get("ip-addresses") or [""])[0]
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {primary_ip} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv6 reservation to NetBox")
                        messages.warning(request, "Reservation created, but NetBox IPAM sync failed.")
                messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to create DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
                logger.exception("Failed to create DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, "Failed to create reservation: see server logs for details.")
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "options_formset": options_formset,
                "dhcp_version": 6,
                "action": "Add",
                "return_url": return_url,
            },
        )


class ServerReservation4EditView(_KeaChangeMixin, generic.ObjectView):
    """Edit an existing DHCPv4 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_form.html"

    def _get_reservation(self, server: Server, subnet_id: int, ip_address: str) -> dict | None:
        client = server.get_client(version=4)
        return client.reservation_get("dhcp4", subnet_id=subnet_id, ip_address=ip_address)

    def get(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Pre-populate form with existing reservation data."""
        server = self.get_object(pk=pk)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        try:
            reservation = self._get_reservation(server, subnet_id, ip_address)
        except KeaException as exc:
            logger.exception("Failed to fetch DHCPv4 reservation %s in subnet %s", ip_address, subnet_id)
            messages.error(request, kea_error_hint(exc))
            return redirect(return_url)
        except Exception:
            logger.exception("Failed to fetch DHCPv4 reservation %s in subnet %s", ip_address, subnet_id)
            messages.error(request, "Failed to retrieve reservation: see server logs for details.")
            return redirect(return_url)
        if reservation is None:
            raise Http404(f"Reservation {ip_address} not found in subnet {subnet_id}")
        identifier_type, identifier = _get_reservation_identifier(reservation, 4)
        initial = {
            "subnet_id": reservation.get("subnet-id", subnet_id),
            "ip_address": reservation.get("ip-address", ip_address),
            "identifier_type": identifier_type,
            "identifier": identifier,
            "hostname": reservation.get("hostname", ""),
        }
        existing_options = reservation.get("option-data", [])
        options_initial = [
            {"name": o.get("name", ""), "data": o.get("data", ""), "always_send": o.get("always-send", False)}
            for o in existing_options
        ]
        context: dict[str, Any] = {
            "object": server,
            "form": forms.Reservation4Form(initial=initial),
            "options_formset": forms.ReservationOptionsFormSet(initial=options_initial, prefix="options"),
            "dhcp_version": 4,
            "action": "Edit",
            "return_url": return_url,
        }
        try:
            lease = server.get_client(version=4).lease_get_by_ip(4, ip_address)
            if lease and lease.get("hostname") and lease.get("hostname") != reservation.get("hostname", ""):
                context["lease_diff"] = {"hostname": lease["hostname"]}
        except Exception:
            pass
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Validate and submit updated reservation to Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation4Form(data=request.POST)
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        if form.is_valid() and options_valid:
            cd = form.cleaned_data
            reservation: dict[str, Any] = {
                "subnet-id": cd["subnet_id"],
                "ip-address": cd["ip_address"],
                cd["identifier_type"]: cd["identifier"],
            }
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            option_data = [
                {"name": f["name"], "data": f["data"], **({"always-send": True} if f.get("always_send") else {})}
                for f in (getattr(options_formset, "cleaned_data", []) or [])
                if f and f.get("name") and not f.get("DELETE")
            ]
            if option_data:
                reservation["option-data"] = option_data
            client = server.get_client(version=4)
            try:
                client.reservation_update("dhcp4", reservation)
                messages.success(request, f"Reservation for {cd['ip_address']} updated.")
                _add_reservation_journal(server, request.user, "updated", reservation)

                reservation_updated.send_robust(
                    sender=None,
                    server=server,
                    reservation=reservation,
                    dhcp_version=4,
                    request=request,
                )
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {cd['ip_address']} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv4 reservation %s to NetBox", cd.get("ip_address"))
                        messages.warning(request, "Reservation updated, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except PartialPersistError:
                # Change is live but config-write failed — run all success-path side effects.
                _add_reservation_journal(server, request.user, "updated", reservation)
                reservation_updated.send_robust(
                    sender=None,
                    server=server,
                    reservation=reservation,
                    dhcp_version=4,
                    request=request,
                )
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {cd['ip_address']} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv4 reservation %s to NetBox", cd.get("ip_address"))
                        messages.warning(request, "Reservation updated, but NetBox IPAM sync failed.")
                messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to update DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
                logger.exception("Failed to update DHCPv4 reservation for %s", cd.get("ip_address"))
                messages.error(request, "Failed to update reservation: see server logs for details.")
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "options_formset": options_formset,
                "dhcp_version": 4,
                "action": "Edit",
                "return_url": return_url,
            },
        )


class ServerReservation6EditView(_KeaChangeMixin, generic.ObjectView):
    """Edit an existing DHCPv6 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_form.html"

    def _get_reservation(self, server: Server, subnet_id: int, ip_address: str) -> dict | None:
        client = server.get_client(version=6)
        return client.reservation_get("dhcp6", subnet_id=subnet_id, ip_address=ip_address)

    def get(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Pre-populate form with existing DHCPv6 reservation data."""
        server = self.get_object(pk=pk)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        try:
            reservation = self._get_reservation(server, subnet_id, ip_address)
        except KeaException as exc:
            logger.exception("Failed to fetch DHCPv6 reservation %s in subnet %s", ip_address, subnet_id)
            messages.error(request, kea_error_hint(exc))
            return redirect(return_url)
        except Exception:
            logger.exception("Failed to fetch DHCPv6 reservation %s in subnet %s", ip_address, subnet_id)
            messages.error(request, "Failed to retrieve reservation: see server logs for details.")
            return redirect(return_url)
        if reservation is None:
            raise Http404(f"Reservation {ip_address} not found in subnet {subnet_id}")
        identifier_type, identifier = _get_reservation_identifier(reservation, 6)
        ip_list = reservation.get("ip-addresses", [ip_address])
        initial = {
            "subnet_id": reservation.get("subnet-id", subnet_id),
            "ip_addresses": ",".join(ip_list),
            "identifier_type": identifier_type,
            "identifier": identifier,
            "hostname": reservation.get("hostname", ""),
        }
        existing_options = reservation.get("option-data", [])
        options_initial = [
            {"name": o.get("name", ""), "data": o.get("data", ""), "always_send": o.get("always-send", False)}
            for o in existing_options
        ]
        context: dict[str, Any] = {
            "object": server,
            "form": forms.Reservation6Form(initial=initial),
            "options_formset": forms.ReservationOptionsFormSet(initial=options_initial, prefix="options"),
            "dhcp_version": 6,
            "action": "Edit",
            "return_url": return_url,
        }
        try:
            lease = server.get_client(version=6).lease_get_by_ip(6, ip_address)
            if lease and lease.get("hostname") and lease.get("hostname") != reservation.get("hostname", ""):
                context["lease_diff"] = {"hostname": lease["hostname"]}
        except Exception:
            pass
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Validate and submit updated DHCPv6 reservation to Kea."""
        server = self.get_object(pk=pk)
        form = forms.Reservation6Form(data=request.POST)
        options_formset, options_valid = _build_reservation_options_formset(request.POST)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        if form.is_valid() and options_valid:
            cd = form.cleaned_data
            reservation: dict[str, Any] = {
                "subnet-id": cd["subnet_id"],
                "ip-addresses": [ip.strip() for ip in cd["ip_addresses"].split(",")],
                cd["identifier_type"]: cd["identifier"],
            }
            if cd.get("hostname"):
                reservation["hostname"] = cd["hostname"]
            option_data = [
                {"name": f["name"], "data": f["data"], **({"always-send": True} if f.get("always_send") else {})}
                for f in (getattr(options_formset, "cleaned_data", []) or [])
                if f and f.get("name") and not f.get("DELETE")
            ]
            if option_data:
                reservation["option-data"] = option_data
            client = server.get_client(version=6)
            try:
                client.reservation_update("dhcp6", reservation)
                messages.success(request, "DHCPv6 reservation updated.")
                _add_reservation_journal(server, request.user, "updated", reservation)

                reservation_updated.send_robust(
                    sender=None,
                    server=server,
                    reservation=reservation,
                    dhcp_version=6,
                    request=request,
                )
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        primary_ip = (reservation.get("ip-addresses") or [""])[0]
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {primary_ip} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv6 reservation to NetBox")
                        messages.warning(request, "Reservation updated, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except PartialPersistError:
                messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
                _add_reservation_journal(server, request.user, "updated", reservation)
                reservation_updated.send_robust(
                    sender=None,
                    server=server,
                    reservation=reservation,
                    dhcp_version=6,
                    request=request,
                )
                if cd.get("sync_to_netbox"):
                    try:
                        _, created = sync_reservation_to_netbox(reservation)
                        primary_ip = (reservation.get("ip-addresses") or [""])[0]
                        msg = "created" if created else "updated"
                        messages.info(request, f"NetBox IPAddress {primary_ip} {msg}.")
                    except Exception:
                        logger.exception("Failed to sync DHCPv6 reservation to NetBox")
                        messages.warning(request, "Reservation updated, but NetBox IPAM sync failed.")
                return redirect(return_url)
            except KeaException as exc:
                logger.exception("Failed to update DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, kea_error_hint(exc))
            except Exception:
                logger.exception("Failed to update DHCPv6 reservation for %s", cd.get("ip_addresses"))
                messages.error(request, "Failed to update reservation: see server logs for details.")
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "form": form,
                "options_formset": options_formset,
                "dhcp_version": 6,
                "action": "Edit",
                "return_url": return_url,
            },
        )


class ServerReservation4DeleteView(_KeaChangeMixin, generic.ObjectView):
    """Delete confirmation for a DHCPv4 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_delete.html"

    def get(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Show deletion confirmation page."""
        server = self.get_object(pk=pk)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "ip_address": ip_address,
                "subnet_id": subnet_id,
                "dhcp_version": 4,
                "return_url": reverse("plugins:netbox_kea:server_reservations4", args=[pk]),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Issue reservation-del to Kea and redirect."""
        server = self.get_object(pk=pk)
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[pk])
        client = server.get_client(version=4)
        try:
            client.reservation_del("dhcp4", subnet_id=subnet_id, ip_address=ip_address)
            messages.success(request, f"Reservation for {ip_address} deleted.")
            _add_reservation_journal(
                server, request.user, "deleted", {"ip-address": ip_address, "subnet-id": subnet_id}
            )

            reservation_deleted.send_robust(
                sender=None,
                server=server,
                ip_address=ip_address,
                dhcp_version=4,
                request=request,
            )
        except PartialPersistError:
            _add_reservation_journal(
                server, request.user, "deleted", {"ip-address": ip_address, "subnet-id": subnet_id}
            )
            reservation_deleted.send_robust(
                sender=None,
                server=server,
                ip_address=ip_address,
                dhcp_version=4,
                request=request,
            )
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.exception("Failed to delete DHCPv4 reservation for %s", ip_address)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to delete DHCPv4 reservation for %s", ip_address)
            messages.error(request, "Failed to delete reservation: see server logs for details.")
        return redirect(return_url)


class ServerReservation6DeleteView(_KeaChangeMixin, generic.ObjectView):
    """Delete confirmation for a DHCPv6 host reservation."""

    queryset = Server.objects.all()
    template_name = "netbox_kea/server_reservation_delete.html"

    def get(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Show deletion confirmation page."""
        server = self.get_object(pk=pk)
        return render(
            request,
            self.template_name,
            {
                "object": server,
                "ip_address": ip_address,
                "subnet_id": subnet_id,
                "dhcp_version": 6,
                "return_url": reverse("plugins:netbox_kea:server_reservations6", args=[pk]),
            },
        )

    def post(self, request: HttpRequest, pk: int, subnet_id: int, ip_address: str) -> HttpResponse:
        """Issue reservation-del to Kea and redirect."""
        server = self.get_object(pk=pk)
        return_url = reverse("plugins:netbox_kea:server_reservations6", args=[pk])
        client = server.get_client(version=6)
        try:
            client.reservation_del("dhcp6", subnet_id=subnet_id, ip_address=ip_address)
            messages.success(request, f"DHCPv6 reservation for {ip_address} deleted.")
            _add_reservation_journal(
                server, request.user, "deleted", {"ip-address": ip_address, "subnet-id": subnet_id}
            )

            reservation_deleted.send_robust(
                sender=None,
                server=server,
                ip_address=ip_address,
                dhcp_version=6,
                request=request,
            )
        except PartialPersistError:
            _add_reservation_journal(
                server, request.user, "deleted", {"ip-address": ip_address, "subnet-id": subnet_id}
            )
            reservation_deleted.send_robust(
                sender=None,
                server=server,
                ip_address=ip_address,
                dhcp_version=6,
                request=request,
            )
            messages.warning(request, "Change applied but may not survive a Kea restart (config-write failed).")
        except KeaException as exc:
            logger.exception("Failed to delete DHCPv6 reservation for %s", ip_address)
            messages.error(request, kea_error_hint(exc))
        except Exception:
            logger.exception("Failed to delete DHCPv6 reservation for %s", ip_address)
            messages.error(request, "Failed to delete reservation: see server logs for details.")
        return redirect(return_url)


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
    v4_types = ["hw-address", "client-id", "circuit-id", "flex-id"]
    v6_types = ["duid", "hw-address", "client-id", "flex-id"]
    priority = v6_types if version == 6 else v4_types
    for itype in priority:
        if itype in reservation:
            return itype, reservation[itype]
    return "hw-address", ""


def _enrich_reservations_with_badges(reservations: list[dict[str, Any]], server: "Server", version: int) -> None:
    """In-place: add active-lease status and NetBox IPAM badge fields to reservation dicts.

    Adds:
    - ``has_active_lease``: True/False (None if lease_cmds unavailable)
    - ``netbox_ip_url``: absolute URL if IP exists in NetBox IPAM
    - ``sync_url``: POST endpoint URL to create a NetBox IP when absent
    """
    from ..sync import bulk_fetch_netbox_ips

    client = server.get_client(version=version)
    _enrich_reservations_with_lease_status(client, reservations, version=version)

    sync_url = reverse(f"plugins:netbox_kea:server_reservation{version}_sync", args=[server.pk])
    nb_ips = bulk_fetch_netbox_ips([r.get("ip_address", "") for r in reservations if r.get("ip_address")])
    for r in reservations:
        nb_ip = nb_ips.get(r.get("ip_address", ""))
        if nb_ip:
            r["netbox_ip_url"] = nb_ip.get_absolute_url()
        else:
            r["sync_url"] = sync_url

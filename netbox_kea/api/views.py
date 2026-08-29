import logging
from typing import cast

import requests
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from .. import constants, filtersets, models
from ..kea import KeaException, LeaseQueryGuardError, lease_query_guard_message
from ..reservations import (
    Family,
    GlobalReservationScope,
    InSubnetReservationScope,
    MalformedReservation,
    Reservation,
    ReservationIdentity,
    ReservationScope,
    ReservationSnapshot,
    reservation_query_mode,
    reservation_record_data,
)
from ..subnet_catalogue import display as subnet_catalogue
from ..utilities import format_leases
from .serializers import ServerSerializer

logger = logging.getLogger(__name__)


def _reservation_snapshot_data(snapshot: ReservationSnapshot) -> dict:
    """Return one normalized REST page without raw rejected Kea data."""
    return {
        "count": len(snapshot.records),
        "results": [reservation_record_data(record) for record in snapshot.records],
        "diagnostics": [
            {
                "code": diagnostic.code,
                "message": diagnostic.message,
                "source_position": diagnostic.source_position,
            }
            for diagnostic in snapshot.diagnostics
        ],
        "complete": snapshot.complete,
        "next_cursor": snapshot.next_cursor,
    }


def _single_reservation_response(version: int, reservation: Reservation | None) -> Response:
    if version not in (4, 6):
        raise ValueError(f"version must be 4 or 6, got {version!r}")
    snapshot = ReservationSnapshot(
        family=cast(Family, version),
        records=(reservation,) if reservation is not None else (),
        diagnostics=(),
        complete=True,
        next_cursor=None,
    )
    return Response(_reservation_snapshot_data(snapshot))


def _parse_subnet_lease_state(raw_state, selector) -> tuple[int | None, str | None]:
    """Return a safe Subnet lease state and an optional parameter error."""
    if raw_state in (None, ""):
        return None, None
    if selector != constants.BY_SUBNET_ID:
        return None, "state requires subnet_id as the selected filter."
    try:
        state = int(raw_state)
    except (TypeError, ValueError):
        return None, "A Subnet query supports only the Active or Declined state."
    if state not in (0, 1):
        return None, "A Subnet query supports only the Active or Declined state."
    return state, None


class ServerViewSet(NetBoxModelViewSet):
    """DRF viewset providing CRUD endpoints for Server objects."""

    queryset = models.Server.objects.prefetch_related("tags").order_by("-pk")
    filterset_class = filtersets.ServerFilterSet
    serializer_class = ServerSerializer

    # ─────────────────────────────────────────────────────────────────────
    # Lease search actions
    # ─────────────────────────────────────────────────────────────────────

    @action(detail=True, methods=["get"], url_path="leases4", url_name="leases4")
    def leases4(self, request, pk=None):
        """Search DHCPv4 leases on this server.

        Query parameters (at least one required):
        - ``ip_address``: exact IP lookup
        - ``hw_address``: lookup by MAC address (requires lease_cmds hook)
        - ``hostname``: lookup by hostname (requires lease_cmds hook)
        - ``subnet_id``: lookup all leases in a subnet (requires lease_cmds hook)
        - ``state``: narrow a subnet lookup to Active (0) or Declined (1)
        """
        return self._lease_search(request, version=4)

    @action(detail=True, methods=["get"], url_path="leases6", url_name="leases6")
    def leases6(self, request, pk=None):
        """Search DHCPv6 leases on this server.

        Query parameters (at least one required):
        - ``ip_address``: exact IP lookup
        - ``duid``: lookup by DUID (requires lease_cmds hook)
        - ``hostname``: lookup by hostname (requires lease_cmds hook)
        - ``subnet_id``: lookup all leases in a subnet (requires lease_cmds hook)
        - ``state``: narrow a subnet lookup to Active (0) or Declined (1)
        """
        return self._lease_search(request, version=6)

    def _lease_search(self, request, version: int) -> Response:
        """Dispatch a lease search to Kea and return JSON results."""
        server = self.get_object()
        params = request.query_params

        ip_address = params.get("ip_address")
        hw_address = params.get("hw_address")
        hostname = params.get("hostname")
        subnet_id = params.get("subnet_id")
        raw_state = params.get("state")
        duid = params.get("duid")  # v6 only

        if not any([ip_address, hw_address, hostname, subnet_id, duid]):
            return Response(
                {
                    "detail": (
                        "At least one filter parameter is required: "
                        "ip_address, hw_address, hostname, subnet_id" + (", duid" if version == 6 else "")
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if duid and version != 6:
            return Response({"detail": "duid is only supported for DHCPv6."}, status=status.HTTP_400_BAD_REQUEST)
        if hw_address and version != 4:
            return Response({"detail": "hw_address is only supported for DHCPv4."}, status=status.HTTP_400_BAD_REQUEST)

        if subnet_id:
            try:
                parsed_subnet_id = int(subnet_id)
            except ValueError:
                return Response({"detail": "subnet_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
            if parsed_subnet_id < 1:
                return Response({"detail": "subnet_id must be positive."}, status=status.HTTP_400_BAD_REQUEST)

        queries = (
            (constants.BY_IP, ip_address),
            (constants.BY_HW_ADDRESS, hw_address),
            (constants.BY_DUID, duid),
            (constants.BY_HOSTNAME, hostname),
            (constants.BY_SUBNET_ID, subnet_id),
        )
        selector, value = next((query for query in queries if query[1]), (None, None))
        lease_state, state_error = _parse_subnet_lease_state(raw_state, selector)
        if state_error is not None:
            return Response({"detail": state_error}, status=status.HTTP_400_BAD_REQUEST)

        try:
            client = server.get_client(version=version)
            leases = client.lease_search(version, selector, value, state=lease_state)
        except LeaseQueryGuardError as exc:
            logger.info("Rejected unsafe Subnet lease API query on server %s", server.name)
            return Response(
                {"detail": lease_query_guard_message(exc, lease_state)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except (requests.ConnectionError, requests.Timeout):
            logger.exception("Kea connection error on server %s", server.name)
            return Response({"detail": "Could not connect to Kea server."}, status=status.HTTP_502_BAD_GATEWAY)
        except KeaException:
            logger.exception("Kea error on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        except ValueError:
            logger.exception("Configuration error for server %s", server.name)
            return Response({"detail": "Server configuration error."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception:
            logger.exception("Unexpected error fetching leases from %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        enriched = format_leases(leases)
        return Response({"count": len(enriched), "results": enriched})

    # ─────────────────────────────────────────────────────────────────────
    # Reservation search actions
    # ─────────────────────────────────────────────────────────────────────

    @action(detail=True, methods=["get"], url_path="reservations4", url_name="reservations4")
    def reservations4(self, request, pk=None):
        """Search DHCPv4 host reservations on this server.

        Select exactly one bounded page, exact identity, scoped address, or
        hostname query. Results use the family-neutral Reservation schema.
        """
        return self._reservation_search(request, version=4)

    @action(detail=True, methods=["get"], url_path="reservations6", url_name="reservations6")
    def reservations6(self, request, pk=None):
        """Search DHCPv6 host reservations on this server.

        Select exactly one bounded page, exact identity, scoped address, or
        hostname query. Results use the family-neutral Reservation schema.
        """
        return self._reservation_search(request, version=6)

    def _reservation_search(self, request, version: int) -> Response:
        """Dispatch a reservation search to Kea and return JSON results."""
        server = self.get_object()
        params = request.query_params

        hostname = params.get("hostname")

        try:
            query_mode = reservation_query_mode(params.keys())
        except ValueError:
            return Response(
                {"detail": "Select exactly one Reservation query: page, identity, scoped address, or hostname."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if query_mode == "page":
            return self._reservation_page_response(server, params, version)
        if query_mode == "identity":
            return self._reservation_identity_response(server, params, version)
        if query_mode == "address":
            return self._reservation_address_response(server, params, version)
        if query_mode == "hostname":
            return self._reservation_hostname_response(server, hostname, version)
        raise AssertionError(f"Unsupported Reservation query mode {query_mode!r}")

    def _reservation_page_response(self, server, params, version: int) -> Response:
        """Return one validated and normalized bounded Reservation page."""
        try:
            limit = int(params.get("limit", 100))
        except (TypeError, ValueError):
            return Response({"detail": "limit must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        if not 1 <= limit <= 500:
            return Response(
                {"detail": "limit must be between 1 and 500."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            catalogue = subnet_catalogue(server, version)
            client = server.get_client(version=version)
            snapshot = client.reservation_page(
                version,
                catalogue,
                cursor=params.get("cursor"),
                limit=limit,
            )
        except requests.RequestException:
            logger.exception("Kea connection error on server %s", server.name)
            return Response({"detail": "Could not connect to Kea server."}, status=status.HTTP_502_BAD_GATEWAY)
        except KeaException:
            logger.exception("Kea error on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        except ValueError:
            logger.info("Rejected invalid Reservation page parameters for server %s", server.name)
            return Response({"detail": "Invalid Reservation page parameters."}, status=status.HTTP_400_BAD_REQUEST)
        except RuntimeError:
            logger.exception("Malformed Kea Reservation page on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(_reservation_snapshot_data(snapshot))

    def _reservation_identity_response(self, server, params, version: int) -> Response:
        """Return one exact Reservation Identity target."""
        identifier_type = params.get("identifier_type")
        identifier = params.get("identifier")
        if not identifier_type or not identifier:
            return Response(
                {"detail": "identifier_type and identifier are both required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            scope_name = params.get("scope")
            if scope_name == "global":
                if "subnet_id" in params:
                    raise ValueError("A Global Reservation query cannot select a Subnet.")
                subnet_id = None
            elif scope_name == "in-subnet":
                subnet_id = int(params.get("subnet_id", ""))
            else:
                raise ValueError("scope must be global or in-subnet.")
            catalogue = subnet_catalogue(server, version)
            if subnet_id is None:
                scope: ReservationScope = GlobalReservationScope()
            else:
                subnet = catalogue.find_by_id(subnet_id)
                if subnet is None:
                    raise ValueError("The Reservation Subnet is not verified.")
                scope = InSubnetReservationScope(subnet.identity)
            identity = ReservationIdentity(identifier_type, identifier)
            client = server.get_client(version=version)
            reservation = client.reservation_by_identity(version, catalogue, scope, identity)
        except MalformedReservation:
            logger.warning("Malformed exact Reservation target on server %s", server.name, exc_info=True)
            return Response(
                {"detail": "Kea returned a malformed Reservation target."}, status=status.HTTP_502_BAD_GATEWAY
            )
        except requests.RequestException:
            logger.exception("Kea connection error on server %s", server.name)
            return Response({"detail": "Could not connect to Kea server."}, status=status.HTTP_502_BAD_GATEWAY)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid Reservation identity selector."}, status=status.HTTP_400_BAD_REQUEST)
        except KeaException:
            logger.exception("Kea error on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        except RuntimeError:
            logger.exception("Malformed Kea Reservation response on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        return _single_reservation_response(version, reservation)

    def _reservation_address_response(self, server, params, version: int) -> Response:
        """Resolve one In-Subnet address to its canonical Reservation."""
        try:
            subnet_id = int(params.get("subnet_id", ""))
            catalogue = subnet_catalogue(server, version)
            subnet = catalogue.find_by_id(subnet_id)
            if subnet is None:
                raise ValueError("The Reservation Subnet is not verified.")
            scope = InSubnetReservationScope(subnet.identity)
            client = server.get_client(version=version)
            reservation = client.reservation_by_address(
                version,
                catalogue,
                scope,
                params.get("ip_address"),
            )
        except MalformedReservation:
            logger.warning("Malformed scoped Reservation target on server %s", server.name, exc_info=True)
            return Response(
                {"detail": "Kea returned a malformed Reservation target."}, status=status.HTTP_502_BAD_GATEWAY
            )
        except requests.RequestException:
            logger.exception("Kea connection error on server %s", server.name)
            return Response({"detail": "Could not connect to Kea server."}, status=status.HTTP_502_BAD_GATEWAY)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid scoped address selector."}, status=status.HTTP_400_BAD_REQUEST)
        except KeaException:
            logger.exception("Kea error on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        except RuntimeError:
            logger.exception("Malformed Kea Reservation response on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        return _single_reservation_response(version, reservation)

    def _reservation_hostname_response(self, server, hostname: str, version: int) -> Response:
        """Return one normalized hostname Reservation Snapshot."""
        try:
            catalogue = subnet_catalogue(server, version)
            client = server.get_client(version=version)
            snapshot = client.reservations_by_hostname(version, catalogue, hostname)
        except requests.RequestException:
            logger.exception("Kea connection error on server %s", server.name)
            return Response({"detail": "Could not connect to Kea server."}, status=status.HTTP_502_BAD_GATEWAY)
        except KeaException:
            logger.exception("Kea error on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        except ValueError:
            return Response({"detail": "Invalid hostname selector."}, status=status.HTTP_400_BAD_REQUEST)
        except RuntimeError:
            logger.exception("Malformed Kea Reservation response on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(_reservation_snapshot_data(snapshot))

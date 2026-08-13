import logging

import requests
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from .. import constants, filtersets, models
from ..kea import KeaClient, KeaException, iter_reservations
from ..utilities import format_leases
from .serializers import ServerSerializer

logger = logging.getLogger(__name__)


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

        if subnet_id is not None:
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

        try:
            client = server.get_client(version=version)
            leases = client.lease_search(version, selector, value)
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

        Query parameters — at least one required:
        - ``ip_address`` + ``subnet_id``: exact lookup by IP
        - ``hw_address`` + ``subnet_id``: lookup by hardware address
        - ``subnet_id`` only: all reservations in that subnet (paginated via reservation-get-page)
        """
        return self._reservation_search(request, version=4)

    @action(detail=True, methods=["get"], url_path="reservations6", url_name="reservations6")
    def reservations6(self, request, pk=None):
        """Search DHCPv6 host reservations on this server.

        Query parameters — at least one required:
        - ``ip_address`` + ``subnet_id``: exact lookup by IP
        - ``duid`` + ``subnet_id``: lookup by DUID
        - ``subnet_id`` only: all reservations in that subnet
        """
        return self._reservation_search(request, version=6)

    def _reservation_search(self, request, version: int) -> Response:
        """Dispatch a reservation search to Kea and return JSON results."""
        server = self.get_object()
        params = request.query_params

        ip_address = params.get("ip_address")
        hw_address = params.get("hw_address")
        subnet_id = params.get("subnet_id")
        duid = params.get("duid")  # v6 only

        if not any([ip_address, hw_address, subnet_id, duid]):
            return Response(
                {
                    "detail": (
                        "At least one filter parameter is required: "
                        "ip_address, hw_address, subnet_id" + (", duid" if version == 6 else "")
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if subnet_id is not None:
            try:
                int(subnet_id)
            except ValueError:
                return Response({"detail": "subnet_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            client = server.get_client(version=version)
            reservations = self._fetch_reservations(client, version, ip_address, hw_address, subnet_id, duid)
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
            logger.exception("Unexpected error fetching reservations from %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"count": len(reservations), "results": reservations})

    def _fetch_reservations(
        self,
        client: KeaClient,
        version: int,
        ip_address: str | None,
        hw_address: str | None,
        subnet_id: str | None,
        duid: str | None,
    ) -> list[dict]:
        """Call the appropriate Kea reservation command and return reservation dicts."""
        service = f"dhcp{version}"

        if ip_address and subnet_id:
            host = client.reservation_get(service, int(subnet_id), ip_address=ip_address)
            return [host] if host else []

        if hw_address and subnet_id:
            host = client.reservation_get(service, int(subnet_id), identifier_type="hw-address", identifier=hw_address)
            return [host] if host else []

        if duid and subnet_id and version == 6:
            host = client.reservation_get(service, int(subnet_id), identifier_type="duid", identifier=duid)
            return [host] if host else []

        if subnet_id:
            # Page through all reservations exhaustively, then filter by subnet_id client-side.
            return [h for h in iter_reservations(client, service) if str(h.get("subnet-id", "")) == str(subnet_id)]

        return []

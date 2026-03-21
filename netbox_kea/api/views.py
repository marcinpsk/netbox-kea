import logging

import requests
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from .. import filtersets, models
from ..kea import KeaClient, KeaException
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

        try:
            client = server.get_client(version=version)
            leases = self._fetch_leases(client, version, ip_address, hw_address, hostname, subnet_id, duid)
        except (requests.ConnectionError, requests.Timeout):
            logger.exception("Kea connection error on server %s", server.name)
            return Response({"detail": "Could not connect to Kea server."}, status=status.HTTP_502_BAD_GATEWAY)
        except KeaException:
            logger.exception("Kea error on server %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception:
            logger.exception("Unexpected error fetching leases from %s", server.name)
            return Response({"detail": "An internal error occurred"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        enriched = format_leases(leases)
        return Response({"count": len(enriched), "results": enriched})

    def _fetch_leases(
        self,
        client: KeaClient,
        version: int,
        ip_address: str | None,
        hw_address: str | None,
        hostname: str | None,
        subnet_id: str | None,
        duid: str | None,
    ) -> list[dict]:
        """Call the appropriate Kea lease command and return raw lease dicts."""
        service = f"dhcp{version}"

        if ip_address:
            resp = client.command(
                f"lease{version}-get",
                service=[service],
                arguments={"ip-address": ip_address},
                check=(0, 3),
            )
            if resp[0]["result"] == 3:
                return []
            args = resp[0].get("arguments")
            return [args] if args else []

        if hw_address:
            resp = client.command(
                f"lease{version}-get-by-hw-address",
                service=[service],
                arguments={"hw-address": hw_address},
                check=(0, 3),
            )
            if resp[0]["result"] == 3:
                return []
            return (resp[0].get("arguments") or {}).get("leases", [])

        if duid and version == 6:
            resp = client.command(
                "lease6-get-by-duid",
                service=[service],
                arguments={"duid": duid},
                check=(0, 3),
            )
            if resp[0]["result"] == 3:
                return []
            return (resp[0].get("arguments") or {}).get("leases", [])

        if hostname:
            resp = client.command(
                f"lease{version}-get-by-hostname",
                service=[service],
                arguments={"hostname": hostname},
                check=(0, 3),
            )
            if resp[0]["result"] == 3:
                return []
            return (resp[0].get("arguments") or {}).get("leases", [])

        if subnet_id:
            resp = client.command(
                f"lease{version}-get-all",
                service=[service],
                arguments={"subnets": [int(subnet_id)]},
                check=(0, 3),
            )
            if resp[0]["result"] == 3:
                return []
            return (resp[0].get("arguments") or {}).get("leases", [])

        return []

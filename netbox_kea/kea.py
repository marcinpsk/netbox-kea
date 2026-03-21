import logging
from collections.abc import Sequence
from typing import Any, TypedDict

import requests
from requests.models import HTTPBasicAuth

logger = logging.getLogger(__name__)


class KeaResponse(TypedDict):
    """Typed dict representing a single Kea API response object."""

    result: int
    arguments: dict[str, Any] | None
    text: str | None


class KeaClient:
    """HTTP client for the Kea Control API."""

    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        verify: bool | str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        timeout: int = 30,
    ):
        """Initialise a Kea HTTP client session.

        Args:
            url: Base URL of the Kea Control Agent or DHCP daemon endpoint.
            username: Optional HTTP Basic Auth username.
            password: Optional HTTP Basic Auth password.
            verify: SSL verification — True/False or path to a CA bundle.
            client_cert: Path to client certificate for mutual TLS.
            client_key: Path to private key matching client_cert.
            timeout: Request timeout in seconds.

        Raises:
            ValueError: If only one of client_cert/client_key is provided.

        """
        if (client_cert is not None and client_key is None) or (client_cert is None and client_key is not None):
            raise ValueError("Key and Cert must be used together.")

        self.url = url
        self.timeout = timeout

        self._session = requests.Session()
        if verify is not None:
            self._session.verify = verify
        if username is not None and password is not None:
            self._session.auth = HTTPBasicAuth(username, password)
        if client_cert is not None and client_key is not None:
            self._session.cert = (client_cert, client_key)

    def command(
        self,
        command: str,
        service: list[str] | None = None,
        arguments: dict[str, Any] | None = None,
        check: None | Sequence[int] = (0,),
    ) -> list[KeaResponse]:
        """Send a command to the Kea API and return the response list.

        Args:
            command: Kea command name (e.g. ``"lease4-get-all"``).
            service: List of target services (e.g. ``["dhcp4"]``). Omit for CA-level commands.
            arguments: Optional command arguments payload.
            check: Sequence of acceptable result codes. Pass ``None`` to skip checking.

        Returns:
            Parsed JSON response as a list of KeaResponse dicts.

        Raises:
            requests.HTTPError: If the HTTP response status is not 2xx.
            KeaException: If any response result code is not in *check*.

        """
        body: dict[str, Any] = {"command": command}

        if service is not None:
            body["service"] = service

        if arguments is not None:
            body["arguments"] = arguments

        resp = self._session.post(self.url, json=body, timeout=self.timeout)
        resp.raise_for_status()
        resp_json = resp.json()
        if not isinstance(resp_json, list):
            raise ValueError(f"Expected list response from Kea API, got {type(resp_json).__name__}")
        if check is not None:
            check_response(resp_json, check)
        return resp_json

    def get_available_commands(self, service: str) -> set[str]:
        """Return the set of commands available on *service* (e.g. ``"dhcp4"``).

        Args:
            service: Kea service name to query (``"dhcp4"`` or ``"dhcp6"``).

        Returns:
            Set of command name strings reported by ``list-commands``.

        """
        resp = self.command("list-commands", service=[service])
        return set(resp[0].get("arguments", []))

    def reservation_get_page(
        self,
        service: str,
        source_index: int = 0,
        from_index: int = 0,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch a page of host reservations from Kea.

        Args:
            service: Target service (``"dhcp4"`` or ``"dhcp6"``).
            source_index: 0 = all sources, 1+ = specific backend source index.
            from_index: Starting offset within the source (use ``next_from`` returned
                by a previous call to continue pagination).
            limit: Maximum number of hosts to return per page.

        Returns:
            A ``(hosts, next_from, next_source_index)`` tuple.  Both ``next_from``
            and ``next_source_index`` are always read from Kea's ``next`` cursor.
            Pass them as ``from_index`` / ``source_index`` on the next call to
            continue paginating; both will be 0 when the source is exhausted.

        Raises:
            KeaException: If Kea returns result code 1 or 2 (error / unknown command).

        """
        resp = self.command(
            "reservation-get-page",
            service=[service],
            arguments={"source-index": source_index, "from": from_index, "limit": limit},
            check=(0, 3),
        )
        if resp[0]["result"] == 3:
            return [], 0, 0
        args = resp[0].get("arguments", {})
        hosts: list[dict[str, Any]] = args.get("hosts", [])
        next_obj = args.get("next") or {}
        return hosts, next_obj.get("from", 0), next_obj.get("source-index", 0)

    def reservation_add(self, service: str, reservation: dict[str, Any]) -> None:
        """Add a host reservation to Kea.

        Args:
            service: Target service (``"dhcp4"`` or ``"dhcp6"``).
            reservation: Reservation dict matching the Kea ``reservation-add`` schema.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        self.command(
            "reservation-add",
            service=[service],
            arguments={"reservation": reservation},
        )

    def reservation_update(self, service: str, reservation: dict[str, Any]) -> None:
        """Update an existing host reservation in Kea.

        Args:
            service: Target service (``"dhcp4"`` or ``"dhcp6"``).
            reservation: Updated reservation dict.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        self.command(
            "reservation-update",
            service=[service],
            arguments={"reservation": reservation},
        )

    def reservation_del(
        self,
        service: str,
        subnet_id: int,
        ip_address: str | None = None,
        identifier_type: str | None = None,
        identifier: str | None = None,
    ) -> None:
        """Delete a host reservation from Kea.

        Args:
            service: Target service (``"dhcp4"`` or ``"dhcp6"``).
            subnet_id: Subnet ID the reservation belongs to.
            ip_address: IP address to identify the reservation. Mutually exclusive with
                *identifier_type* / *identifier*.
            identifier_type: Identifier type (e.g. ``"hw-address"``). Requires *identifier*.
            identifier: Identifier value. Requires *identifier_type*.

        Raises:
            ValueError: If neither *ip_address* nor *identifier_type* is provided.
            KeaException: If Kea returns a non-zero result code.

        """
        if ip_address is not None and identifier_type is not None:
            raise ValueError("ip_address and identifier_type are mutually exclusive; provide exactly one.")
        if ip_address is None and identifier_type is None:
            raise ValueError("Either ip_address or identifier_type+identifier must be provided.")
        if (identifier_type is None) != (identifier is None):
            raise ValueError("identifier_type and identifier must both be provided together.")
        args: dict[str, Any] = {"subnet-id": subnet_id}
        if ip_address is not None:
            args["ip-address"] = ip_address
        else:
            args["identifier-type"] = identifier_type
            args["identifier"] = identifier
        self.command("reservation-del", service=[service], arguments=args)

    def reservation_get(
        self,
        service: str,
        subnet_id: int,
        ip_address: str | None = None,
        identifier_type: str | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a single host reservation from Kea.

        Args:
            service: Target service (``"dhcp4"`` or ``"dhcp6"``).
            subnet_id: Subnet ID to look in.
            ip_address: Lookup by IP address.
            identifier_type: Lookup by identifier type (e.g. ``"hw-address"``).
            identifier: Identifier value.

        Returns:
            The reservation dict, or ``None`` if not found (result code 3).

        Raises:
            KeaException: If Kea returns result code 1 (error).

        """
        if ip_address is not None and identifier_type is not None:
            raise ValueError("ip_address and identifier_type are mutually exclusive; provide exactly one.")
        if ip_address is None and identifier_type is None:
            raise ValueError("Either ip_address or identifier_type+identifier must be provided.")
        if (identifier_type is None) != (identifier is None):
            raise ValueError("identifier_type and identifier must both be provided together.")
        args: dict[str, Any] = {"subnet-id": subnet_id}
        if ip_address is not None:
            args["ip-address"] = ip_address
        else:
            args["identifier-type"] = identifier_type
            args["identifier"] = identifier
        resp = self.command("reservation-get", service=[service], arguments=args, check=(0, 3))
        if resp[0]["result"] == 3:
            return None
        # Kea returns the host fields directly inside "arguments" (not nested under "host")
        return resp[0].get("arguments") or None

    def subnet_add(
        self,
        version: int,
        subnet_cidr: str,
        subnet_id: int | None = None,
        pools: list[str] | None = None,
        gateway: str | None = None,
        dns_servers: list[str] | None = None,
        ntp_servers: list[str] | None = None,
    ) -> None:
        """Add a new subnet to Kea and persist the change.

        Args:
            version: DHCP version (4 or 6).
            subnet_cidr: Subnet in CIDR notation, e.g. ``"10.0.0.0/24"``.
            subnet_id: Optional Kea subnet ID. If ``None``, Kea auto-assigns.
            pools: Optional list of initial pool ranges (e.g. ``["10.0.0.100-10.0.0.200"]``).
            gateway: Optional default gateway IP (sets option ``routers``; DHCPv4 only).
            dns_servers: Optional list of DNS server IPs.
            ntp_servers: Optional list of NTP server hostnames/IPs.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        subnet_def: dict[str, Any] = {"subnet": subnet_cidr}
        if subnet_id is not None:
            subnet_def["id"] = subnet_id
        else:
            # Kea 3.x requires an explicit id — auto-assign max + 1
            try:
                list_resp = self.command(
                    f"subnet{version}-list",
                    service=[service],
                )
                existing = list_resp[0].get("arguments", {}).get("subnets", [])
                max_id = max((s.get("id", 0) for s in existing), default=0)
                subnet_def["id"] = max_id + 1
            except KeaException:
                logger.warning("subnet%s-list failed; falling back to no explicit ID", version)
        if pools:
            subnet_def["pools"] = [{"pool": p} for p in pools]
        option_data: list[dict[str, str]] = []
        if gateway and version == 4:
            option_data.append({"name": "routers", "data": gateway})
        if dns_servers:
            option_data.append(
                {
                    "name": "domain-name-servers" if version == 4 else "dns-servers",
                    "data": ", ".join(dns_servers),
                }
            )
        if ntp_servers:
            option_data.append(
                {
                    "name": "ntp-servers" if version == 4 else "sntp-servers",
                    "data": ", ".join(ntp_servers),
                }
            )
        if option_data:
            subnet_def["option-data"] = option_data
        last_exc: KeaException | None = None
        for _attempt in range(3):
            try:
                self.command(
                    f"subnet{version}-add",
                    service=[service],
                    arguments={subnet_key: [dict(subnet_def)]},
                )
                last_exc = None
                break
            except KeaException as exc:
                if "duplicate" in str(exc).lower() and "id" in subnet_def:
                    subnet_def["id"] += 1
                    last_exc = exc
                else:
                    raise
        if last_exc is not None:
            raise last_exc
        self._persist_config(service)

    def subnet_del(self, version: int, subnet_id: int) -> None:
        """Delete an existing subnet from Kea and persist the change.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to delete.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        self.command(
            f"subnet{version}-del",
            service=[service],
            arguments={"id": subnet_id},
        )
        self._persist_config(service)

    def lease_wipe(self, version: int, subnet_id: int) -> None:
        """Delete all leases in a subnet using the ``lease{v}-wipe`` command.

        Requires the ``lease_cmds`` hook to be loaded on the Kea server.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID whose leases should be wiped.

        Raises:
            KeaException: If Kea returns a non-zero result code (including result=1
                when ``lease_cmds`` is not loaded).

        """
        self.command(
            f"lease{version}-wipe",
            service=[f"dhcp{version}"],
            arguments={"subnet-id": subnet_id},
        )

    def dhcp_disable(self, service: str, max_period: int | None = None) -> None:
        """Temporarily disable DHCP processing on *service*.

        The daemon continues running but stops responding to DHCP requests.
        Pass *max_period* (in seconds) to automatically re-enable after that time;
        omit it to keep the service disabled until :meth:`dhcp_enable` is called.

        Args:
            service: Kea service name, e.g. ``"dhcp4"`` or ``"dhcp6"``.
            max_period: Optional number of seconds before the service auto-re-enables.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        arguments: dict[str, Any] | None = None
        if max_period is not None:
            arguments = {"max-period": max_period}
        self.command("dhcp-disable", service=[service], arguments=arguments)

    def dhcp_enable(self, service: str) -> None:
        """Re-enable DHCP processing on *service* after a :meth:`dhcp_disable` call.

        Args:
            service: Kea service name, e.g. ``"dhcp4"`` or ``"dhcp6"``.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        self.command("dhcp-enable", service=[service])

    def pool_add(self, version: int, subnet_id: int, pool: str) -> None:
        """Add a pool to an existing subnet and persist the change.

        Supports both Kea 2.x (``subnet{v}-pool-add``) and Kea 3.x
        (``subnet{v}-delta-add``). The delta command requires the subnet CIDR,
        which is fetched automatically when the pool-add command is unavailable.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to add the pool to.
            pool: Pool range string (e.g. ``"10.0.0.50-10.0.0.99"`` or CIDR ``"10.0.0.0/28"``).

        Raises:
            KeaException: If Kea returns a non-zero result code for either command.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        available = self.get_available_commands(service)
        if f"subnet{version}-pool-add" in available:
            self.command(
                f"subnet{version}-pool-add",
                service=[service],
                arguments={subnet_key: [{"id": subnet_id, "pools": [{"pool": pool}]}]},
            )
        else:
            subnet_cidr = self._get_subnet_cidr(version, subnet_id)
            self.command(
                f"subnet{version}-delta-add",
                service=[service],
                arguments={subnet_key: [{"id": subnet_id, "subnet": subnet_cidr, "pools": [{"pool": pool}]}]},
            )
        self._persist_config(service)

    def pool_del(self, version: int, subnet_id: int, pool: str) -> None:
        """Remove a pool from an existing subnet and persist the change.

        Supports both Kea 2.x (``subnet{v}-pool-del``) and Kea 3.x
        (``subnet{v}-delta-del``). The delta command requires the subnet CIDR,
        which is fetched automatically when the pool-del command is unavailable.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to remove the pool from.
            pool: Pool range string identifying the pool to delete.

        Raises:
            KeaException: If Kea returns a non-zero result code for either command.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        available = self.get_available_commands(service)
        if f"subnet{version}-pool-del" in available:
            self.command(
                f"subnet{version}-pool-del",
                service=[service],
                arguments={subnet_key: [{"id": subnet_id, "pools": [{"pool": pool}]}]},
            )
        else:
            subnet_cidr = self._get_subnet_cidr(version, subnet_id)
            self.command(
                f"subnet{version}-delta-del",
                service=[service],
                arguments={subnet_key: [{"id": subnet_id, "subnet": subnet_cidr, "pools": [{"pool": pool}]}]},
            )
        self._persist_config(service)

    def _persist_config(self, service: str) -> None:
        """Persist the running config to disk via config-write.

        Logs a warning and raises :exc:`PartialPersistError` when config-write fails. When this
        happens, the mutation is already live in the running config but will be lost on next Kea restart.
        """
        try:
            self.command("config-write", service=[service])
        except KeaException as exc:
            logger.warning(
                "config-write failed for service %s — change is live but not persisted to disk",
                service,
            )
            raise PartialPersistError(service, exc) from exc

    def _get_subnet_cidr(self, version: int, subnet_id: int) -> str:
        """Fetch the CIDR string for *subnet_id* from Kea (e.g. ``"10.0.0.0/24"``).

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to look up.

        Returns:
            Subnet CIDR string.

        Raises:
            KeaException: If the subnet is not found or Kea returns an error.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        resp = self.command(
            f"subnet{version}-get",
            service=[service],
            arguments={"id": subnet_id},
        )
        subnets = resp[0].get("arguments", {}).get(subnet_key, [])
        if not subnets:
            raise KeaException(
                {"result": 3, "text": f"subnet{version}-get returned no subnet for id={subnet_id}", "arguments": None},
                index=0,
            )
        return subnets[0]["subnet"]


class KeaException(Exception):
    """Raised when a Kea API response contains an unexpected result code."""

    def __init__(self, resp: KeaResponse, msg: str | None = None, index: int | None = None) -> None:
        """Initialise with the failing response and optional context."""
        self.index = index
        self.response = resp

        if msg is None:
            msg = f"Kea returned result[{index}] {self.response.get('result')}"
        message = f"{msg}: {self.response.get('text')}"
        super().__init__(message)


class PartialPersistError(KeaException):
    """Raised when a Kea mutation is live but config-write failed.

    The change is applied in memory but will be lost on Kea restart.
    The original :exc:`KeaException` from config-write is stored in ``__cause__``.
    """

    def __init__(self, service: str, cause: Exception) -> None:
        response: KeaResponse = {
            "result": -1,
            "text": f"config-write failed for service {service!r} — change is live but not persisted to disk",
            "arguments": [],
        }
        super().__init__(response, msg=f"partial persist error for {service!r}")
        self.service = service


def check_response(resp: list[KeaResponse], ok_codes: Sequence[int]) -> None:
    """Raise a KeaException for any non 0 responses."""
    for idx, kr in enumerate(resp):
        if kr["result"] not in ok_codes:
            raise KeaException(kr, index=idx)

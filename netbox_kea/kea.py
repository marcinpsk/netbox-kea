import copy
import ipaddress
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

    def clone(self) -> "KeaClient":
        """Return a new KeaClient that shares the same connection settings.

        ``requests.Session`` is not thread-safe, so parallel workers must each
        call ``client.clone()`` rather than sharing a single ``KeaClient``
        instance across threads.
        """
        new = KeaClient.__new__(KeaClient)
        new.url = self.url
        new.timeout = self.timeout
        new._session = requests.Session()
        new._session.auth = self._session.auth
        new._session.verify = self._session.verify
        new._session.cert = self._session.cert
        return new

    def close(self) -> None:
        """Close the underlying requests.Session and release connection resources."""
        self._session.close()

    def __enter__(self) -> "KeaClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

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
        args = resp[0].get("arguments") or {}
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

    def reservation_get_by_ip(self, version: int, ip_address: str) -> dict[str, Any] | None:
        """Fetch a reservation by IP address without requiring the subnet ID.

        Lists all subnets for *version*, filters those whose CIDR contains *ip_address*,
        then calls ``reservation-get`` for each candidate until a match is found.

        Args:
            version: DHCP protocol version (``4`` or ``6``).
            ip_address: IP address to look up.

        Returns:
            The reservation dict, or ``None`` if not found.

        Raises:
            KeaException: If the subnet list call itself fails.

        """
        service = f"dhcp{version}"
        list_resp = self.command(f"subnet{version}-list", service=[service])
        subnets: list[dict[str, Any]] = (
            (list_resp[0].get("arguments") or {}).get("subnets", [])
            if isinstance(list_resp, list) and list_resp and isinstance(list_resp[0], dict)
            else []
        )

        target = ipaddress.ip_address(ip_address)
        for subnet in subnets:
            try:
                network = ipaddress.ip_network(subnet["subnet"], strict=False)
            except (KeyError, ValueError):
                continue
            if target not in network:
                continue
            if "id" not in subnet:
                continue
            reservation = self.reservation_get(service, subnet["id"], ip_address=ip_address)
            if reservation is not None:
                return reservation
        return None

    def subnet_add(  # noqa: C901
        self,
        version: int,
        subnet_cidr: str,
        subnet_id: int | None = None,
        pools: list[str] | None = None,
        gateway: str | None = None,
        dns_servers: list[str] | None = None,
        ntp_servers: list[str] | None = None,
    ) -> int | None:
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
                existing = (
                    (list_resp[0].get("arguments") or {}).get("subnets", [])
                    if isinstance(list_resp, list) and list_resp and isinstance(list_resp[0], dict)
                    else []
                )
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
        try:
            last_exc: KeaException | None = None
            add_resp: list | None = None
            auto_assigned_id = subnet_id is None and "id" in subnet_def
            for _attempt in range(3):
                try:
                    add_resp = self.command(
                        f"subnet{version}-add",
                        service=[service],
                        arguments={subnet_key: [dict(subnet_def)]},
                    )
                    last_exc = None
                    break
                except KeaException as exc:
                    if auto_assigned_id and "duplicate" in str(exc).lower() and "id" in subnet_def:
                        subnet_def["id"] += 1
                        last_exc = exc
                    else:
                        raise
            if last_exc is not None:
                raise last_exc
        except (requests.RequestException, ValueError) as transport_exc:
            found_id = self._find_subnet_id_by_cidr(version, subnet_def["subnet"])
            if found_id is not None:
                err = PartialPersistError(service, transport_exc, subnet_id=found_id)
                raise err from transport_exc
            raise
        # Prefer the authoritative ID Kea echoes back in the add response — it is
        # the only source of truth when subnet{v}-list failed and no explicit id was
        # provided (subnet_def would have no "id" key in that case → returns None).
        if add_resp:
            subnets = (add_resp[0].get("arguments") or {}).get("subnets")
            if subnets:
                kea_id = subnets[0].get("id")
                if kea_id is not None:
                    subnet_def["id"] = kea_id
        try:
            self._persist_config(service)
        except KeaConfigPersistError as exc:
            exc.subnet_id = subnet_def.get("id")
            raise
        except PartialPersistError as exc:
            # Subnet is live; re-raise with the known ID so callers can still
            # perform follow-up operations (e.g. assign to a shared network).
            exc.subnet_id = subnet_def.get("id")
            raise
        return subnet_def.get("id")

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

    def network_add(self, version: int, name: str, options: list[dict] | None = None) -> None:
        """Create a new shared network in Kea and persist the change.

        Args:
            version: DHCP version (4 or 6).
            name: Unique name for the shared network.
            options: Optional list of option-data dicts.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        network_def: dict[str, Any] = {"name": name}
        if options:
            network_def["option-data"] = options
        self.command(
            f"network{version}-add",
            service=[service],
            arguments={"shared-networks": [network_def]},
        )
        self._persist_config(service)

    def network_del(self, version: int, name: str) -> None:
        """Delete a shared network from Kea and persist the change.

        Subnets that were members of the deleted network fall back to the global
        address pool (Kea behaviour).

        Args:
            version: DHCP version (4 or 6).
            name: Name of the shared network to delete.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        self.command(
            f"network{version}-del",
            service=[service],
            arguments={"name": name},
        )
        self._persist_config(service)

    def network_update(
        self,
        version: int,
        name: str,
        description: str | None = None,
        interface: str | None = None,
        relay_addresses: list[str] | None = None,
        options: list[dict] | None = None,
    ) -> None:
        """Update a shared network's properties via config-get → config-test → config-set → config-write.

        Only provided (non-None) fields are modified; others are left unchanged.
        Raises ``KeaException`` if *name* is not found in the config.
        Raises ``KeaConfigTestError`` if config-test validation fails.
        Raises ``PartialPersistError`` if config-write fails after a successful config-set (change
        is live but will not survive restart).
        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"

        resp = self.command("config-get", service=[service])
        # Strip the "hash" key that Kea 2.4+ includes — config-test and config-set reject it.
        raw = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = {k: v for k, v in raw.items() if k != "hash"}

        network: dict[str, Any] | None = None
        for sn in config.get(dhcp_key, {}).get("shared-networks", []):
            if sn.get("name") == name:
                network = sn
                break
        if network is None:
            raise KeaException({"result": 3, "text": f"Shared network '{name}' not found in config"})

        if description is not None:
            network["description"] = description
        if interface is not None:
            if interface:
                network["interface"] = interface
            else:
                network.pop("interface", None)
        if relay_addresses is not None:
            if relay_addresses:
                network["relay"] = {"ip-addresses": relay_addresses}
            else:
                network.pop("relay", None)
        if options is not None:
            network["option-data"] = options

        self._apply_config(service, config)

    def network_subnet_add(self, version: int, name: str, subnet_id: int) -> None:
        """Move an existing subnet into a shared network.

        Args:
            version: DHCP version (4 or 6).
            name: Shared network name.
            subnet_id: Kea subnet ID to assign.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        self.command(
            f"network{version}-subnet-add",
            service=[service],
            arguments={"name": name, "id": subnet_id},
        )
        self._persist_config(service)

    def network_subnet_del(self, version: int, name: str, subnet_id: int) -> None:
        """Remove a subnet from a shared network (subnet remains, reverts to global pool).

        Args:
            version: DHCP version (4 or 6).
            name: Shared network name.
            subnet_id: Kea subnet ID to remove from the network.

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        self.command(
            f"network{version}-subnet-del",
            service=[service],
            arguments={"name": name, "id": subnet_id},
        )
        self._persist_config(service)

    def subnet_update(
        self,
        version: int,
        subnet_id: int,
        subnet_cidr: str,
        pools: list[str] | None = None,
        gateway: str | None = None,
        dns_servers: list[str] | None = None,
        ntp_servers: list[str] | None = None,
        valid_lft: int | None = None,
        min_valid_lft: int | None = None,
        max_valid_lft: int | None = None,
        renew_timer: int | None = None,
        rebind_timer: int | None = None,
    ) -> None:
        """Update an existing subnet's configuration in Kea and persist the change.

        Performs a read-modify-write: fetches the live subnet via ``subnet_get()``, merges
        only the form-managed fields onto it, then sends the complete merged object to
        ``subnet{v}-update``.  This preserves Kea-managed fields — relay config, allocator
        settings, client-class, reservations, and any ``option-data`` entries not owned by
        this form — that Kea would otherwise clear if we sent a partial object.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID of the subnet to update.
            subnet_cidr: Subnet in CIDR notation (immutable identifier, still required by Kea).
            pools: List of pool range strings.  ``None`` = omit (Kea keeps existing);
                ``[]`` = explicitly clear all pools.
            gateway: Default gateway IP (option ``routers``, DHCPv4 only).
            dns_servers: List of DNS server IP strings.
            ntp_servers: List of NTP server hostnames/IPs.
            valid_lft: Preferred lease lifetime in seconds.
            min_valid_lft: Minimum lease lifetime in seconds.
            max_valid_lft: Maximum lease lifetime in seconds.
            renew_timer: T1 renew timer in seconds (sent as ``renew-timer``).
            rebind_timer: T2 rebind timer in seconds (sent as ``rebind-timer``).

        Raises:
            KeaException: If Kea returns a non-zero result code.

        """
        service = f"dhcp{version}"
        subnet_key = f"subnet{version}"
        # Read live subnet so we can merge — Kea's subnet{v}-update replaces the full
        # object, so we must send ALL fields to avoid silently clearing relay, allocator,
        # client-class, reservations, and any option-data not managed by this form.
        subnet_def = self.subnet_get(version, subnet_id)
        subnet_def.pop("metadata", None)  # Kea adds a read-only metadata key in some responses

        # Identity: always authoritative from params
        subnet_def["id"] = subnet_id
        subnet_def["subnet"] = subnet_cidr

        # option-data: preserve entries NOT owned by this form (e.g. domain-name, tftp-server)
        # while replacing/adding/removing the ones the form manages.
        _managed_option_names = {
            "routers",
            "domain-name-servers",
            "dns-servers",
            "ntp-servers",
            "sntp-servers",
        }
        preserved_opts = [o for o in subnet_def.get("option-data", []) if o.get("name") not in _managed_option_names]
        new_opts: list[dict[str, str]] = []
        if gateway and version == 4:
            new_opts.append({"name": "routers", "data": gateway})
        if dns_servers:
            new_opts.append(
                {
                    "name": "domain-name-servers" if version == 4 else "dns-servers",
                    "data": ", ".join(dns_servers),
                }
            )
        if ntp_servers:
            new_opts.append(
                {
                    "name": "ntp-servers" if version == 4 else "sntp-servers",
                    "data": ", ".join(ntp_servers),
                }
            )
        subnet_def["option-data"] = preserved_opts + new_opts

        # pools: replace only when the caller explicitly passes a value
        if pools is not None:
            subnet_def["pools"] = [{"pool": p} for p in pools]

        # Lifetime / timer fields: override only when explicitly provided, otherwise
        # the live value (already present in subnet_def from subnet_get) is kept.
        for value, kea_key in [
            (valid_lft, "valid-lft"),
            (min_valid_lft, "min-valid-lft"),
            (max_valid_lft, "max-valid-lft"),
            (renew_timer, "renew-timer"),
            (rebind_timer, "rebind-timer"),
        ]:
            if value is not None:
                subnet_def[kea_key] = value

        self.command(
            f"subnet{version}-update",
            service=[service],
            arguments={subnet_key: [subnet_def]},
        )
        self._persist_config(service)

    def subnet_update_options(self, version: int, subnet_id: int, options: list[dict]) -> None:
        """Update option-data for a subnet via config-get → config-test → config-write.

        Free Kea has no option-set hook, so the only supported approach is a full
        read-modify-write cycle: fetch the current config, replace the subnet's
        ``option-data`` in the Python dict, then validate and write it back using
        ``config-test`` (with the modified config as ``arguments``) followed by
        ``config-write`` (also with the modified config).

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID.
            options: New ``option-data`` list. Pass ``[]`` to remove all options.

        Raises:
            KeaException: If ``subnet_id`` is not found, or if ``config-test`` fails.
            PartialPersistError: If ``config-write`` fails after successful ``config-test``.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        subnet_key = f"subnet{version}"

        resp = self.command("config-get", service=[service])
        raw = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = raw
        config.pop("hash", None)

        subnet = None
        for s in config.get(dhcp_key, {}).get(subnet_key, []):
            if s.get("id") == subnet_id:
                subnet = s
                break
        if subnet is None:
            for sn in config.get(dhcp_key, {}).get("shared-networks", []):
                for s in sn.get(subnet_key, []):
                    if s.get("id") == subnet_id:
                        subnet = s
                        break
                if subnet is not None:
                    break
        if subnet is None:
            raise KeaException({"result": 3, "text": f"Subnet id {subnet_id} not found in config"})

        subnet["option-data"] = options
        self._apply_config(service, config)

    def server_update_options(self, version: int, options: list[dict]) -> None:
        """Update server-level option-data via config-get → config-test → config-write.

        Replaces the ``option-data`` list at the ``Dhcp{v}`` level (not per-subnet).
        Uses the same read-modify-write pipeline as :meth:`subnet_update_options`.

        Args:
            version: DHCP version (4 or 6).
            options: New ``option-data`` list. Pass ``[]`` to remove all server-level options.

        Raises:
            KeaException: If ``config-test`` fails.
            PartialPersistError: If ``config-write`` fails after successful ``config-test``.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"

        resp = self.command("config-get", service=[service])
        raw = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = raw
        config.pop("hash", None)
        config.setdefault(dhcp_key, {})["option-data"] = options
        self._apply_config(service, config)

    def option_def_list(self, version: int) -> list[dict]:
        """Return the current ``option-def`` list for a DHCP version via ``config-get``.

        Args:
            version: DHCP version (4 or 6).

        Returns:
            List of option-def dicts, or ``[]`` if none are defined.

        Raises:
            KeaException: If ``config-get`` fails.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        resp = self.command("config-get", service=[service])
        raw = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        return raw.get(dhcp_key, {}).get("option-def", [])

    def option_def_add(self, version: int, option_def: dict) -> None:
        """Append a new option-def entry via config-get → config-test → config-write.

        Args:
            version: DHCP version (4 or 6).
            option_def: A dict with keys ``name``, ``code``, ``type``, ``space``,
                and optionally ``array``, ``encapsulate``, ``record-types``.

        Raises:
            KeaException: If ``config-test`` fails.
            PartialPersistError: If ``config-write`` fails after successful ``config-test``.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        resp = self.command("config-get", service=[service])
        raw_args = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw_args, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = copy.deepcopy(raw_args)
        config.pop("hash", None)
        defs = config.setdefault(dhcp_key, {}).setdefault("option-def", [])
        defs.append(option_def)
        self._apply_config(service, config)

    def option_def_del(self, version: int, code: int, space: str) -> None:
        """Remove an option-def entry by code+space via config-get → config-test → config-write.

        Args:
            version: DHCP version (4 or 6).
            code: Option code of the entry to remove.
            space: Option space of the entry to remove.

        Raises:
            KeaConfigTestError: If ``config-test`` fails before the mutation is applied.
            PartialPersistError: If ``config-write`` fails after successful ``config-test``.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        resp = self.command("config-get", service=[service])
        raw_args = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
        if not isinstance(raw_args, dict):
            raise KeaException({"result": -1, "text": f"config-get returned unexpected arguments for {service}"})
        config = copy.deepcopy(raw_args)
        config.pop("hash", None)
        defs = config.get(dhcp_key, {}).get("option-def", [])
        new_defs = [d for d in defs if not (d.get("code") == code and d.get("space") == space)]
        if len(new_defs) == len(defs):
            raise KeaException({"result": 3, "text": f"option-def code={code} space={space} not found"})
        config.setdefault(dhcp_key, {})["option-def"] = new_defs
        self._apply_config(service, config)

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

    def lease_add(self, version: int, lease: dict) -> None:
        """Create a new lease in the Kea lease database using ``lease{v}-add``.

        Args:
            version: DHCP version (4 or 6).
            lease: Full lease dict as expected by the Kea API. For v4, ``ip-address``
                is required. For v6, ``ip-address``, ``duid``, and ``iaid`` are required.

        Raises:
            KeaException: If Kea returns a non-zero result code (e.g. address already
                in use, subnet not found).

        """
        self.command(
            f"lease{version}-add",
            service=[f"dhcp{version}"],
            arguments=lease,
        )

    def lease_update(
        self,
        version: int,
        ip_address: str,
        hostname: str | None = None,
        hw_address: str | None = None,
        valid_lft: int | None = None,
        duid: str | None = None,
    ) -> None:
        """Modify an existing lease in-place using ``lease{v}-update``.

        Fetches the current lease via ``lease{v}-get``, merges the provided
        non-None overrides, then posts the updated lease back.  No
        config-test/write cycle is needed because lease mutations go directly
        to Kea's live lease database.

        Args:
            version: DHCP version (4 or 6).
            ip_address: IP address of the lease to update.
            hostname: Optional new hostname.
            hw_address: Optional new hardware address (v4 only, ``xx:xx:...`` format).
            valid_lft: Optional new valid lifetime in seconds.
            duid: Optional new DUID (v6 only).

        Raises:
            KeaException: If the lease does not exist (result=3) or Kea returns
                an error for the update.

        """
        service = f"dhcp{version}"
        resp = self.command(
            f"lease{version}-get",
            service=[service],
            arguments={"ip-address": ip_address},
        )
        if resp[0]["result"] == 3:
            raise KeaException(resp[0])
        lease = resp[0]["arguments"]
        if hostname is not None:
            lease["hostname"] = hostname
        if hw_address is not None:
            lease["hw-address"] = hw_address
        if valid_lft is not None:
            lease["valid-lft"] = valid_lft
        if duid is not None:
            lease["duid"] = duid
        self.command(
            f"lease{version}-update",
            service=[service],
            arguments=lease,
        )

    def lease_get_by_ip(self, version: int, ip_address: str) -> dict | None:
        """Fetch a single lease by IP address.

        Args:
            version: DHCP version (4 or 6).
            ip_address: IP address to look up.

        Returns:
            Lease dict from ``lease{v}-get`` response arguments, or ``None`` if not found
            (result=3).

        Raises:
            KeaException: If Kea returns any error other than "not found" (result != 0/3).

        """
        service = f"dhcp{version}"
        resp = self.command(
            f"lease{version}-get",
            service=[service],
            arguments={"ip-address": ip_address},
            check=(0, 3),
        )
        if resp[0]["result"] == 3:
            return None
        args = resp[0].get("arguments")
        if not isinstance(args, dict):
            raise ValueError(
                f"lease{version}-get returned result=0 but arguments is {type(args).__name__}, expected dict"
            )
        return args

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

    def _apply_config(self, service: str, config: dict) -> None:
        """Validate, apply, and persist a modified config dict.

        Used by read-modify-write methods (e.g. ``subnet_update_options``,
        ``server_update_options``, ``option_def_add/del``) that mutate a config
        obtained from ``config-get`` and need to push it back.

        Flow: ``config-test`` → ``config-set`` → ``config-write``.

        Args:
            service: Kea service name (e.g. ``"dhcp4"``).
            config: The full config dict (already mutated) to apply.

        Raises:
            KeaConfigTestError: If ``config-test`` fails (result != 2).
            PartialPersistError: If ``config-write`` fails after ``config-set``.

        """
        try:
            self.command("config-test", service=[service], arguments=config)
        except KeaException as exc:
            if exc.response.get("result") == 2:
                logger.debug("config-test not supported for service %s — skipping pre-flight check", service)
            else:
                logger.warning("config-test failed for service %s — aborting config-set", service)
                raise KeaConfigTestError(service, exc) from exc
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                "config-test transport/parse error for service %s — aborting config-set",
                service,
            )
            raise KeaConfigTestError(service, exc) from exc
        try:
            self.command("config-set", service=[service], arguments=config)
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                "config-set transport/parse error for service %s — change may be live but unpersisted", service
            )
            raise AmbiguousConfigSetError(service, exc) from exc
        try:
            self.command("config-write", service=[service])
        except (KeaException, requests.RequestException, ValueError) as exc:
            logger.warning("config-write failed for service %s — change not persisted to disk", service)
            raise PartialPersistError(service, exc) from exc

    def _persist_config(self, service: str) -> None:
        """Validate the current running config and persist it to disk.

        Flow:
        1. ``config-get`` — fetch the live in-memory config (which already reflects
           any mutation applied via Kea-native commands like ``subnet4-delta-add``).
        2. ``config-test`` with that config as ``arguments`` — validate it.  Kea
           requires the config to be passed as arguments; calling ``config-test``
           without arguments always returns result 1 "Missing mandatory 'arguments'
           parameter."  Result 2 (command not supported) is silently skipped.  Any
           other non-zero result raises :exc:`KeaConfigTestError`.
        3. ``config-write`` — persist the validated config to disk.  Failure raises
           :exc:`PartialPersistError` (change is live but will be lost on restart).
        """
        # Step 1: fetch the current in-memory config so we can validate and write it.
        try:
            resp = self.command("config-get", service=[service])
        except (KeaException, requests.RequestException, ValueError):
            logger.warning("config-get failed for service %s — skipping validation, attempting config-write", service)
            resp = None

        config: dict | None = None
        if resp is not None:
            if isinstance(resp, list) and resp and isinstance(resp[0], dict):
                raw = resp[0].get("arguments")
            else:
                raw = resp.get("arguments") if isinstance(resp, dict) else None
            if isinstance(raw, dict):
                config = {k: v for k, v in raw.items() if k != "hash"}
            else:
                logger.warning(
                    "config-get for service %s returned unexpected arguments shape: %s", service, type(raw).__name__
                )

        # Step 2: config-test — pass the live config as arguments (required by Kea).
        if config is not None:
            try:
                self.command("config-test", service=[service], arguments=config)
            except KeaException as exc:
                result = exc.response.get("result")
                if result == 2:
                    logger.debug("config-test not supported for service %s — skipping pre-flight check", service)
                else:
                    logger.warning("config-test failed for service %s — aborting config-write", service)
                    raise KeaConfigPersistError(service, exc) from exc
            except (requests.RequestException, ValueError) as exc:
                logger.warning(
                    "config-test transport error for service %s — aborting config-write", service, exc_info=True
                )
                raise KeaConfigPersistError(service, exc) from exc

        # Step 3: write to disk.
        try:
            self.command("config-write", service=[service])
        except (KeaException, requests.RequestException, ValueError) as exc:
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
        subnets = (resp[0].get("arguments") or {}).get(subnet_key, [])
        if not subnets:
            raise KeaException(
                {"result": 3, "text": f"subnet{version}-get returned no subnet for id={subnet_id}", "arguments": None},
                index=0,
            )
        return subnets[0]["subnet"]

    def subnet_get(self, version: int, subnet_id: int) -> dict:
        """Fetch the full subnet config dict for *subnet_id* from Kea.

        Unlike :meth:`_get_subnet_cidr`, this method returns the complete
        subnet object (id, subnet, pools, option-data, relay, allocator, ...)
        enabling a read-modify-write cycle without losing live-only fields.

        Args:
            version: DHCP version (4 or 6).
            subnet_id: Kea subnet ID to look up.

        Returns:
            A shallow copy of the full subnet dict (nested structures like pools
            and option-data are not deep-copied — callers must not mutate nested
            lists/dicts in place) as returned by Kea.

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
        args = resp[0].get("arguments") or {}
        subnets = args.get(subnet_key, []) if isinstance(args, dict) else []
        if not subnets:
            raise KeaException(
                {"result": 3, "text": f"subnet{version}-get returned no subnet for id={subnet_id}", "arguments": None},
                index=0,
            )
        return dict(subnets[0])

    def _find_subnet_id_by_cidr(self, version: int, cidr: str) -> int | None:
        """Search the running Kea config for a subnet matching *cidr*.

        Returns the Kea subnet ID if found, or ``None`` if the subnet does not
        exist or if the config-get probe itself fails.  Used as a best-effort
        disambiguation probe after a transport error on ``subnet{v}-add`` to
        detect whether the command was actually processed by Kea.

        """
        service = f"dhcp{version}"
        dhcp_key = f"Dhcp{version}"
        subnet_key = f"subnet{version}"
        try:
            resp = self.command("config-get", service=[service])
            raw_args = resp[0].get("arguments") if resp and isinstance(resp[0], dict) else None
            conf = raw_args.get(dhcp_key, {}) if isinstance(raw_args, dict) else {}
            for s in conf.get(subnet_key, []):
                if s.get("subnet") == cidr:
                    return s.get("id")  # None if id absent — callers treat None as "not found"
            for sn in conf.get("shared-networks", []):
                for s in sn.get(subnet_key, []):
                    if s.get("subnet") == cidr:
                        return s.get("id")  # None if id absent — callers treat None as "not found"
        except (KeaException, requests.RequestException, ValueError):
            logger.debug(
                "_find_subnet_id_by_cidr: config-get failed for cidr=%s version=%s",
                cidr,
                version,
                exc_info=True,
            )
        return None


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


class KeaConfigTestError(KeaException):
    """Raised when ``config-test`` fails before any mutation has been applied.

    The Kea configuration is unchanged — no data has been written.
    The original :exc:`KeaException` from config-test is stored in ``__cause__``.

    Used by ``_apply_config`` (read-modify-write methods such as
    ``subnet_update_options`` and ``server_update_options``) where config-test
    is run *before* ``config-set``, so a failure means the running config is
    still intact.
    """

    def __init__(self, service: str, cause: Exception) -> None:
        response: KeaResponse = {
            "result": -1,
            "text": f"config-test failed for service {service!r} — mutation was not applied",
            "arguments": [],
        }
        super().__init__(response, msg=f"config-test error for {service!r}")
        self.service = service


class KeaConfigPersistError(KeaException):
    """Raised when ``_persist_config`` rejects the already-live config via ``config-test``.

    The mutation IS already applied to the running daemon (the change is live in
    memory) but config-test found the resulting config invalid, so config-write
    was skipped.  The change **will be lost on daemon restart**.

    Distinct from :exc:`PartialPersistError` (which is raised when config-write
    itself fails after a successful config-test) and from :exc:`KeaConfigTestError`
    (which is raised before any mutation is applied).
    """

    def __init__(self, service: str, cause: Exception) -> None:
        response: KeaResponse = {
            "result": -1,
            "text": (
                f"config-test rejected the running config for service {service!r} "
                "— mutation is live but config-write was skipped"
            ),
            "arguments": [],
        }
        super().__init__(response, msg=f"config persist error for {service!r}")
        self.service = service


class PartialPersistError(KeaException):
    """Raised when a Kea mutation is live but config-write failed.

    The change is applied in memory but will be lost on Kea restart.
    The original :exc:`KeaException` from config-write is stored in ``__cause__``.

    ``subnet_id`` is set when the partial write occurred during ``subnet_add`` —
    the subnet is live and this ID can still be used for follow-up operations
    (e.g. assigning to a shared network) even though config-write failed.
    """

    def __init__(self, service: str, cause: Exception, subnet_id: int | None = None) -> None:
        response: KeaResponse = {
            "result": -1,
            "text": f"config-write failed for service {service!r} — change is live but not persisted to disk",
            "arguments": [],
        }
        super().__init__(response, msg=f"partial persist error for {service!r}")
        self.service = service
        self.subnet_id: int | None = subnet_id


class AmbiguousConfigSetError(PartialPersistError):
    """Raised when a config-set reply is lost or malformed.

    The change *may* be live but we cannot confirm — the transport or JSON
    parsing failed after sending the config-set command.  Distinct from
    :exc:`PartialPersistError` where we *know* the mutation succeeded but
    config-write failed.

    Inherits from :exc:`PartialPersistError` so existing ``except
    PartialPersistError`` handlers still catch it.  Callers that need to
    distinguish ambiguous-set from definite-write-failure can catch this
    subclass first.
    """

    def __init__(self, service: str, cause: Exception) -> None:
        super().__init__(service, cause)
        ambiguous_text = f"config-set reply lost/malformed for service {service!r} — change may or may not be live"
        self.response["text"] = ambiguous_text
        self.args = (f"partial persist error for {service!r}: {ambiguous_text}",)


def check_response(resp: list[KeaResponse], ok_codes: Sequence[int]) -> None:
    """Raise a KeaException for any non 0 responses."""
    for idx, kr in enumerate(resp):
        if kr["result"] not in ok_codes:
            raise KeaException(kr, index=idx)

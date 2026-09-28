# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Configuration Changes to a Server Configuration, and their typed outcome (ADR 0005).

An operation returns a ``ConfigChangeOutcome`` when the change is live or can be live, and raises
``ConfigChangeRejected`` when it is not live as far as NetBox can tell.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import requests
from django.db import OperationalError, connection, transaction
from urllib3.exceptions import MaxRetryError, NewConnectionError

from .constants import Family, Persistence
from .kea import KeaClient, KeaException, PoolAction, subnet_network
from .server_configuration import Pool
from .subnet_catalogue import CatalogueUnavailable, MutationScope, VerifiedSubnet, mutation

if TYPE_CHECKING:
    from .models import Server

logger = logging.getLogger(__name__)

# How long an operation waits for another operation on the same Kea daemon.
LOCK_WAIT_SECONDS = 10.0

Application = Literal["applied", "unknown"]
RejectionReason = Literal["kea-rejected", "config-test-rejected", "not-sent", "invalid-client-configuration"]

# Kea result 5: Kea could not return to a working configuration.
_RESULT_FATAL = 5
_LOCK_NOT_AVAILABLE = "55P03"


def _int4(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big", signed=True)


# The first key of the two-key advisory lock form, which NetBox's one-key locks cannot reach.
_LOCK_CLASS = _int4("netbox_kea.config_write")


@dataclass(frozen=True)
class ConfigChangeOutcome:
    """The result of a Configuration Change that is live or can be live."""

    application: Application
    persistence: Persistence
    diagnostics: tuple[str, ...] = ()


class ConfigChangeRejected(Exception):
    """A Configuration Change that is not live, as far as NetBox can tell."""

    def __init__(self, reason: RejectionReason, diagnostics: tuple[str, ...]) -> None:
        """Keep the reason and the diagnostics that explain it."""
        super().__init__(reason, *diagnostics)
        self.reason = reason
        self.diagnostics = diagnostics


def add_shared_network(server: Server, family: Family, name: str) -> ConfigChangeOutcome:
    """Add an empty Shared Network named *name*."""
    with _client(server, family) as client, _serialized(client, family):
        if _read_before(lambda: client.shared_network_exists(family, name)):
            raise ConfigChangeRejected("not-sent", (f"Shared Network '{name}' already exists.",))
        application, diagnostics = _mutate(
            client,
            family,
            lambda: client.network_add(family, name),
            not_live=lambda: not client.shared_network_exists(family, name),
        )
        persisted = client.persist(family)
    return ConfigChangeOutcome(application, persisted.persistence, diagnostics + persisted.diagnostics)


def delete_shared_network(server: Server, family: Family, name: str) -> ConfigChangeOutcome:
    """Delete the Shared Network named *name*. Its member Subnets stay."""
    with _client(server, family) as client, _serialized(client, family):
        if not _read_before(lambda: client.shared_network_exists(family, name)):
            raise ConfigChangeRejected("not-sent", (f"Shared Network '{name}' not found.",))
        application, diagnostics = _mutate(
            client,
            family,
            lambda: client.network_del(family, name),
            not_live=lambda: client.shared_network_exists(family, name),
        )
        persisted = client.persist(family)
    return ConfigChangeOutcome(application, persisted.persistence, diagnostics + persisted.diagnostics)


def delete_subnet(server: Server, family: Family, subnet_id: int, cidr: str) -> ConfigChangeOutcome:
    """Delete the Subnet with *subnet_id*, only while that ID names the network *cidr*."""
    with _client(server, family) as client, _serialized(client, family):
        with mutation(server, family) as scope:
            subnet = _subnet_as_seen(scope, subnet_id, cidr)
        application, diagnostics = _mutate(
            client,
            family,
            lambda: client.subnet_del(family, subnet.subnet_id),
            not_live=lambda: _still_there(server, family, subnet) is not None,
        )
        persisted = client.persist(family)
    return ConfigChangeOutcome(application, persisted.persistence, diagnostics + persisted.diagnostics)


def add_pool(server: Server, family: Family, subnet_id: int, cidr: str, pool: Pool) -> ConfigChangeOutcome:
    """Add *pool* to the Subnet with *subnet_id*, only while that ID names the network *cidr*."""
    return _change_pool(server, family, "add", subnet_id, cidr, pool)


def delete_pool(server: Server, family: Family, subnet_id: int, cidr: str, pool: Pool) -> ConfigChangeOutcome:
    """Delete *pool* from the Subnet with *subnet_id*, only while that ID names the network *cidr*."""
    return _change_pool(server, family, "del", subnet_id, cidr, pool)


def _change_pool(
    server: Server, family: Family, action: PoolAction, subnet_id: int, cidr: str, pool: Pool
) -> ConfigChangeOutcome:
    with _client(server, family) as client, _serialized(client, family):
        with mutation(server, family) as scope:
            subnet = _subnet_as_seen(scope, subnet_id, cidr)
        delta = _read_before(lambda: client.pool_uses_delta(family, action))
        application, diagnostics = _mutate(
            client,
            family,
            lambda: client.pool_change(family, action, subnet.subnet_id, subnet.declared_cidr, pool.range, delta=delta),
            # Not live: an added Pool is absent, or a deleted Pool is still there.
            not_live=lambda: (pool in _pools_now(server, family, subnet)) == (action == "del"),
        )
        persisted = client.persist(family)
    return ConfigChangeOutcome(application, persisted.persistence, diagnostics + persisted.diagnostics)


def _client(server: Server, family: Family) -> KeaClient:
    try:
        return server.get_client(version=family)
    except ValueError as exc:
        logger.warning("Could not build a DHCPv%s client for Server %s", family, server.pk, exc_info=True)
        raise ConfigChangeRejected(
            "invalid-client-configuration",
            ("NetBox could not build a Kea client from the Server connection settings.",),
        ) from exc


@contextmanager
def _serialized(client: KeaClient, family: Family) -> Iterator[None]:
    """Hold the advisory lock of one Kea daemon and family until the transaction ends."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout')")
            (previous,) = cursor.fetchone()
            # A lock_timeout without a unit is in milliseconds.
            cursor.execute("SELECT set_config('lock_timeout', %s::text, true)", [round(LOCK_WAIT_SECONDS * 1000)])
            try:
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_CLASS, _int4(f"{family} {client.url}")])
            except OperationalError as exc:
                if getattr(exc.__cause__, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                    raise
                raise ConfigChangeRejected(
                    "not-sent", ("Another change to this Kea server is still running. Try again later.",)
                ) from exc
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])
        yield


def _subnet_as_seen(scope: MutationScope, subnet_id: int, cidr: str) -> VerifiedSubnet:
    """Return the Verified Subnet with *subnet_id* and the network *cidr*: the Subnet that the operator saw."""
    try:
        subnet = scope.find_by_id(subnet_id)
    except CatalogueUnavailable as exc:
        raise ConfigChangeRejected(
            "not-sent", ("NetBox could not confirm Kea's Subnet list, so it did not send the change. Try again later.",)
        ) from exc
    if subnet is None or subnet.network != subnet_network(cidr, scope.family):
        raise ConfigChangeRejected(
            "not-sent",
            (f"Subnet {subnet_id} ({cidr}) changed in Kea. Reload the page and try again.",),
        )
    return subnet


def _still_there(server: Server, family: Family, subnet: VerifiedSubnet) -> VerifiedSubnet | None:
    """Read *subnet* again in a fresh scope. Return it while its ID still names its network, else None."""
    with mutation(server, family) as scope:
        current = scope.find_by_id(subnet.subnet_id)
    return current if current is not None and current.network == subnet.network else None


def _pools_now(server: Server, family: Family, subnet: VerifiedSubnet) -> tuple[Pool, ...]:
    """Return the Pools that *subnet* holds now, and none when the Subnet is gone.

    Raises:
        CatalogueUnavailable: If Kea did not return the configuration facts of the Subnet, so its Pools are unknown.

    """
    current = _still_there(server, family, subnet)
    if current is None:
        return ()
    if current.configuration is None:
        raise CatalogueUnavailable("Kea did not return the configuration facts of the Subnet.")
    return current.configuration.pools


def _read_before(read: Callable[[], bool]) -> bool:
    """Run the read that comes before the change. A failure means that the change was not sent."""
    try:
        return read()
    except KeaException as exc:
        logger.warning("The read before a Configuration Change failed: %s", exc)
        raise ConfigChangeRejected("not-sent", (f"Kea replied: {exc.reply_text}",)) from exc
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        logger.warning("The read before a Configuration Change failed", exc_info=True)
        raise ConfigChangeRejected(
            "not-sent", ("Kea did not return a usable reply to the read before the change.",)
        ) from exc
    except OSError as exc:
        raise _missing_tls_file(exc) from exc


def _mutate(
    client: KeaClient, family: Family, send: Callable[[], None], *, not_live: Callable[[], bool]
) -> tuple[Application, tuple[str, ...]]:
    """Send one command that can change the configuration, and classify what Kea did with it."""
    try:
        send()
    except KeaException as exc:
        logger.warning("A Configuration Change failed: %s", exc)
        diagnostic = f"Kea replied: {exc.reply_text}"
        if exc.response.get("result") == _RESULT_FATAL or client.forwarding_failed(exc, family):
            return "unknown", (diagnostic,)
        try:
            unchanged = not_live()
        except (KeaException, OSError, ValueError, RuntimeError):
            logger.warning("The check read after a failed Configuration Change failed", exc_info=True)
            return "unknown", (diagnostic, "The read after the failure did not succeed.")
        if unchanged:
            raise ConfigChangeRejected("kea-rejected", (diagnostic,)) from exc
        return "unknown", (diagnostic, "The read after the failure shows the change.")
    except requests.RequestException as exc:
        if _never_sent(exc):
            logger.warning("A Configuration Change could not connect to Kea", exc_info=True)
            raise ConfigChangeRejected("not-sent", ("Kea could not be reached.",)) from exc
        logger.warning("The reply to a Configuration Change was lost or unreadable", exc_info=True)
        return "unknown", ("Kea's reply to the change was lost or unreadable.",)
    except OSError as exc:
        raise _missing_tls_file(exc) from exc
    except (ValueError, RuntimeError):
        logger.warning("The reply to a Configuration Change was malformed", exc_info=True)
        return "unknown", ("Kea's reply to the change was lost or unreadable.",)
    return "applied", ()


def _never_sent(exc: requests.RequestException) -> bool:
    """Return whether the request failed while it connected, before any byte reached Kea."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if not isinstance(exc, requests.ConnectionError):
        return False
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if isinstance(current, NewConnectionError):
            return True
        # requests passes urllib3's MaxRetryError as an argument, not as the cause.
        pending.extend(arg for arg in current.args if isinstance(arg, BaseException))
        if isinstance(current, MaxRetryError) and current.reason is not None:
            pending.append(current.reason)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return False


def _missing_tls_file(exc: OSError) -> ConfigChangeRejected:
    """Return the rejection for a TLS file that requests could not find before it sent anything."""
    logger.warning("A TLS file of the Server is missing", exc_info=exc)
    return ConfigChangeRejected(
        "invalid-client-configuration", ("A TLS certificate, key or CA file of the Server could not be found.",)
    )

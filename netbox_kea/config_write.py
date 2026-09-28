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
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import requests
from django.db import DatabaseError, OperationalError, connection, transaction
from urllib3.exceptions import MaxRetryError, NewConnectionError

from .constants import Family, Persistence
from .dhcp_options import DHCPOptionConflict, DHCPOptionNameChange
from .kea import (
    CandidateConfiguration,
    CandidateTargetMissing,
    KeaClient,
    KeaException,
    PoolAction,
    SharedNetworkEdit,
    subnet_network,
)
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

T = TypeVar("T")


def _int4(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big", signed=True)


# The first key of the two-key advisory lock form, which NetBox's one-key locks cannot reach.
_LOCK_CLASS = _int4("netbox_kea.config_write")


SUBNET_LIST_UNCONFIRMED = "NetBox could not confirm Kea's Subnet list, so it did not send the change. Try again later."


def subnet_changed(subnet_id: int, cidr: str) -> str:
    """Return the message for a Subnet ID that no longer names the network *cidr* that the page showed."""
    return f"Subnet {subnet_id} ({cidr}) changed in Kea. Reload the page and try again."


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
        return _persisted(client, family, application, diagnostics)


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
        return _persisted(client, family, application, diagnostics)


def delete_subnet(server: Server, family: Family, subnet_id: int, cidr: str) -> ConfigChangeOutcome:
    """Delete the Subnet with *subnet_id*, only while that ID names the network *cidr*."""
    with _client(server, family) as client, _serialized(client, family):
        with mutation(server, family) as scope:
            subnet = _subnet_as_seen(scope, subnet_id, cidr)

        def still_there() -> bool:
            current = _subnet_now(server, family, subnet.subnet_id)
            return current is not None and current.network == subnet.network

        application, diagnostics = _mutate(
            client, family, lambda: client.subnet_del(family, subnet.subnet_id), not_live=still_there
        )
        return _persisted(client, family, application, diagnostics)


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
        _require_pool_state(subnet, action, pool)
        application, diagnostics = _mutate(
            client,
            family,
            lambda: client.pool_change(family, action, subnet.subnet_id, subnet.declared_cidr, pool.range),
            # Not live: an added Pool is absent, or a deleted Pool is still there.
            not_live=lambda: (pool in _pools_now(server, family, subnet)) == (action == "del"),
        )
        return _persisted(client, family, application, diagnostics)


def _persisted(
    client: KeaClient, family: Family, application: Application, diagnostics: tuple[str, ...]
) -> ConfigChangeOutcome:
    """Run the persist step and return the outcome of the change."""
    persisted = client.persist(family)
    return ConfigChangeOutcome(application, persisted.persistence, diagnostics + persisted.diagnostics)


def set_subnet_options(
    server: Server, family: Family, subnet_id: int, cidr: str, rows: list[dict[str, Any]]
) -> ConfigChangeOutcome:
    """Merge the options form *rows* into the Subnet with *subnet_id*, only while that ID names the network *cidr*.

    Raises:
        DHCPOptionConflict: If a row names a DHCP Option that is missing or ambiguous, or the rows miss one.
        DHCPOptionNameChange: If a row renames a coded DHCP Option.

    """
    network = subnet_network(cidr, family)
    return _read_modify_write(
        server,
        family,
        lambda candidate: candidate.set_subnet_options(subnet_id, network, rows),
        missing=subnet_changed(subnet_id, cidr),
        seen=(subnet_id, cidr),
    )


def set_server_options(server: Server, family: Family, rows: list[dict[str, Any]]) -> ConfigChangeOutcome:
    """Merge the options form *rows* into the server-global DHCP Options.

    Raises:
        DHCPOptionConflict: If a row names a DHCP Option that is missing or ambiguous, or the rows miss one.
        DHCPOptionNameChange: If a row renames a coded DHCP Option.

    """
    return _read_modify_write(server, family, lambda candidate: candidate.set_global_options(rows))


def add_option_definition(server: Server, family: Family, option_def: dict[str, Any]) -> ConfigChangeOutcome:
    """Add the Option Definition *option_def*. Kea's config-test refuses a duplicate."""
    return _read_modify_write(server, family, lambda candidate: candidate.add_option_definition(option_def))


def delete_option_definition(server: Server, family: Family, code: int, space: str) -> ConfigChangeOutcome:
    """Delete the Option Definition with *code* in *space*."""
    return _read_modify_write(
        server,
        family,
        lambda candidate: candidate.delete_option_definition(code, space),
        missing=f"Option Definition {code} in space '{space}' not found.",
    )


def edit_shared_network(server: Server, family: Family, name: str, edit: SharedNetworkEdit) -> ConfigChangeOutcome:
    """Set the fields of the Shared Network *name* that the edit form manages."""
    return _read_modify_write(
        server,
        family,
        lambda candidate: candidate.edit_shared_network(name, edit),
        missing=f"Shared Network '{name}' not found.",
    )


def _read_modify_write(
    server: Server,
    family: Family,
    edit: Callable[[CandidateConfiguration], None],
    *,
    missing: str = "",
    seen: tuple[int, str] | None = None,
) -> ConfigChangeOutcome:
    """Edit the running configuration and send it back with config-set, all under the lock.

    *missing* explains a target that *edit* does not find. *seen* is the Subnet ID and CIDR that the page showed.
    """
    with _client(server, family) as client, _serialized(client, family):
        if seen is not None:
            with mutation(server, family) as scope:
                _subnet_as_seen(scope, *seen)
        candidate = _read_before(lambda: client.config_candidate(family))
        _edit(candidate, edit, missing)
        _test_before(client, family, candidate)
        # config-set commits and then runs the hook initialization, so no failure result proves it is not live.
        application, diagnostics = _mutate(client, family, lambda: client.config_set(candidate), not_live=None)
        return _persisted(client, family, application, diagnostics)


def _edit(candidate: CandidateConfiguration, edit: Callable[[CandidateConfiguration], None], missing: str) -> None:
    """Run *edit* on the candidate. A DHCP Option form error propagates unchanged."""
    try:
        edit(candidate)
    except CandidateTargetMissing as exc:
        raise ConfigChangeRejected("not-sent", (missing,)) from exc
    except (DHCPOptionConflict, DHCPOptionNameChange):
        raise
    except (ValueError, RuntimeError) as exc:
        logger.warning("The configuration that Kea returned cannot be edited", exc_info=True)
        raise ConfigChangeRejected(
            "not-sent", ("Kea returned a configuration that NetBox cannot edit safely.",)
        ) from exc


def _test_before(client: KeaClient, family: Family, candidate: CandidateConfiguration) -> None:
    """Send config-test of the candidate. A Kea without config-test skips it."""

    def test() -> None:
        try:
            client.config_test(candidate)
        except KeaException as exc:
            if exc.unsupported_command:
                logger.debug("config-test is not supported on DHCPv%s, so the candidate was not tested", family)
                return
            if client.forwarding_failed(exc, family):
                raise
            logger.warning("config-test rejected a Configuration Change: %s", exc)
            raise ConfigChangeRejected("config-test-rejected", (f"Kea replied: {exc.reply_text}",)) from exc

    _read_before(test)


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
    body_done = False
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('lock_timeout')")
                (previous,) = cursor.fetchone()
                # A lock_timeout without a unit is in milliseconds.
                cursor.execute("SELECT set_config('lock_timeout', %s::text, true)", [round(LOCK_WAIT_SECONDS * 1000)])
                try:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_CLASS, _int4(f"{family} {client.url}")]
                    )
                except OperationalError as exc:
                    if getattr(exc.__cause__, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                        raise
                    raise ConfigChangeRejected(
                        "not-sent", ("Another change to this Kea server is still running. Try again later.",)
                    ) from exc
                cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])
            yield
            body_done = True
    except DatabaseError:
        if not body_done:
            raise
        # The transaction holds only the lock, and the change is already live in Kea.
        logger.warning("The lock transaction failed to end after the Configuration Change", exc_info=True)


def _subnet_as_seen(scope: MutationScope, subnet_id: int, cidr: str) -> VerifiedSubnet:
    """Return the Verified Subnet with *subnet_id* and the network *cidr*: the Subnet that the operator saw."""
    try:
        subnet = scope.find_by_id(subnet_id)
    except CatalogueUnavailable as exc:
        raise ConfigChangeRejected("not-sent", (SUBNET_LIST_UNCONFIRMED,)) from exc
    if subnet is None or subnet.network != subnet_network(cidr, scope.family):
        raise ConfigChangeRejected("not-sent", (subnet_changed(subnet_id, cidr),))
    return subnet


def _require_pool_state(subnet: VerifiedSubnet, action: PoolAction, pool: Pool) -> None:
    """Reject a Pool delete that the Subnet does not hold, or a Pool add that it holds. Without facts, Kea decides."""
    if subnet.configuration is None:
        return
    held = pool in subnet.configuration.pools
    if action == "del" and not held:
        problem = f"has no Pool {pool.range}"
    elif action == "add" and held:
        problem = f"already has Pool {pool.range}"
    else:
        return
    raise ConfigChangeRejected(
        "not-sent", (f"Subnet {subnet.subnet_id} ({subnet.cidr}) {problem}. Reload the page and try again.",)
    )


def _subnet_now(server: Server, family: Family, subnet_id: int) -> VerifiedSubnet | None:
    """Read the Subnet with *subnet_id* in a fresh scope. Return None when no Subnet has that ID."""
    with mutation(server, family) as scope:
        return scope.find_by_id(subnet_id)


def _pools_now(server: Server, family: Family, subnet: VerifiedSubnet) -> tuple[Pool, ...]:
    """Return the Pools that *subnet* holds now, and none when its ID is gone.

    Raises:
        CatalogueUnavailable: If the ID now names another network, or Kea did not return the configuration facts
            of the Subnet, so the Pools that the command reached are unknown.

    """
    current = _subnet_now(server, family, subnet.subnet_id)
    if current is None:
        return ()
    # The ID now names another network, so this read cannot show the Pools of the Subnet that the command named.
    if current.network != subnet.network:
        raise CatalogueUnavailable(f"Subnet ID {subnet.subnet_id} now names {current.cidr}.")
    if current.configuration is None:
        raise CatalogueUnavailable("Kea did not return the configuration facts of the Subnet.")
    return current.configuration.pools


def _read_before(read: Callable[[], T]) -> T:
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
    client: KeaClient, family: Family, send: Callable[[], None], *, not_live: Callable[[], bool] | None
) -> tuple[Application, tuple[str, ...]]:
    """Send one command that can change the configuration, and classify what Kea did with it.

    *not_live* reads whether a failed command left the configuration unchanged. None: no read can show it.
    """
    try:
        send()
    except KeaException as exc:
        logger.warning("A Configuration Change failed: %s", exc)
        diagnostic = f"Kea replied: {exc.reply_text}"
        if not_live is None or exc.response.get("result") == _RESULT_FATAL or client.forwarding_failed(exc, family):
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

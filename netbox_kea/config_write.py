# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Configuration Changes to a Server Configuration, and their typed outcome (ADR 0005).

An operation returns a ``ConfigChangeOutcome`` when the change is live or can be live, and raises
``ConfigChangeRejected`` when it is not live as far as NetBox can tell.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import requests
from django.db import DatabaseError, OperationalError, connection, transaction
from urllib3.exceptions import MaxRetryError, NewConnectionError

from .constants import Family, IPNetworkValue, Persistence
from .kea import (
    CandidateConfiguration,
    CandidateTargetMissing,
    KeaClient,
    KeaException,
    MalformedConfiguration,
    PoolAction,
    SharedNetworkEdit,
    SubnetEdit,
    SubnetFields,
    subnet_network,
)
from .server_configuration import Pool
from .subnet_catalogue import (
    CatalogueUnavailable,
    MutationScope,
    NewSubnetIdentity,
    SharedNetworkMembership,
    VerifiedSubnet,
    mutation,
)

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
_READ_UNUSABLE = "Kea did not return a usable reply to the read before the change."
_CONFIG_TEST_UNUSABLE = "Kea did not return a usable reply to config-test."


def subnet_changed(subnet_id: int, cidr: str) -> str:
    """Return the message for a Subnet ID that no longer names the network *cidr* that the page showed."""
    return f"Subnet {subnet_id} ({cidr}) changed in Kea. Reload the page and try again."


@dataclass(frozen=True)
class ConfigChangeOutcome:
    """The result of a Configuration Change that is live or can be live."""

    application: Application
    persistence: Persistence
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubnetAddOutcome(ConfigChangeOutcome):
    """The outcome of a Subnet add, with the Subnet ID that the add sent."""

    subnet_id: int = field(kw_only=True)


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


def add_subnet(
    server: Server,
    family: Family,
    cidr: str,
    subnet_id: int | None,
    fields: SubnetFields,
    shared_network: str | None,
) -> SubnetAddOutcome:
    """Add the Subnet *cidr*, and assign it to *shared_network* when one is named.

    *subnet_id* None lets the Subnet Catalogue allocate the ID. When the assignment does not apply, the operation
    deletes the new Subnet again.

    Raises:
        SubnetIdentityConflict: If a Subnet with the CIDR or with the operator's ID exists.
        SubnetIdExhausted: If no free Subnet ID remains.
        CatalogueUnavailable: If the Subnet list is incomplete, so NetBox cannot check the new identity.

    """
    with _client(server, family) as client, _serialized(client, family):
        if shared_network is not None:
            _require_shared_network(client, family, shared_network)
        identity, application, diagnostics = _create_subnet(server, client, family, cidr, subnet_id, fields)
        if application == "unknown":
            diagnostics = (*diagnostics, f"NetBox sent Subnet {identity.subnet_id} ({identity.cidr}).")
        elif shared_network is not None:
            application, diagnostics = _assign_new_subnet(server, client, family, identity, shared_network)
        persisted = client.persist(family)
        return SubnetAddOutcome(
            application, persisted.persistence, diagnostics + persisted.diagnostics, subnet_id=identity.subnet_id
        )


def _require_shared_network(client: KeaClient, family: Family, name: str) -> None:
    if not _read_before(lambda: client.shared_network_exists(family, name)):
        raise ConfigChangeRejected("not-sent", (f"Shared Network '{name}' not found.",))


def _create_subnet(
    server: Server, client: KeaClient, family: Family, cidr: str, requested_id: int | None, fields: SubnetFields
) -> tuple[NewSubnetIdentity, Application, tuple[str, ...]]:
    """Send the Subnet add under an identity from a live scope. Retry once when another Subnet took the allocated ID."""
    with mutation(server, family) as scope:
        identity = scope.prepare_creation(cidr, requested_id)
    taken = False

    def absent() -> bool:
        # Not live only while no Subnet has the ID and the CIDR that the add sent.
        nonlocal taken
        current = _subnet_now(server, family, identity.subnet_id)
        taken = current is not None and current.network != identity.network
        return current is None or taken

    def send() -> tuple[Application, tuple[str, ...]]:
        return _mutate(
            client,
            family,
            lambda: client.subnet_add(family, identity.subnet_id, identity.cidr, fields),
            not_live=absent,
        )

    try:
        application, diagnostics = send()
    except ConfigChangeRejected:
        # The check read, not Kea's error text, shows that another writer took the allocated ID.
        if requested_id is not None or not taken:
            raise
        with mutation(server, family) as scope:
            identity = scope.prepare_creation(cidr)
        application, diagnostics = send()
    return identity, application, diagnostics


def _assign_new_subnet(
    server: Server, client: KeaClient, family: Family, identity: NewSubnetIdentity, name: str
) -> tuple[Application, tuple[str, ...]]:
    """Assign the new Subnet to the Shared Network *name*. Delete the Subnet again when the assignment did not apply."""
    subnet_id, network = identity.subnet_id, identity.network
    delete = _Undo(
        text=f"delete Subnet {subnet_id} again",
        send=lambda: client.subnet_del(family, subnet_id),
        not_live=lambda: _still_there(server, family, subnet_id, network),
        holds=lambda: _member_of(server, family, subnet_id, network, None),
    )
    assign = _Command(
        text=f"assign it to Shared Network '{name}'",
        send=lambda: client.network_subnet_add(family, name, subnet_id),
        not_live=lambda: _not_joined(server, family, subnet_id, name),
    )
    deleted = (
        f"NetBox added Subnet {subnet_id} ({identity.cidr}), but the assignment to Shared Network '{name}' did not "
        "apply, so NetBox deleted the Subnet again."
    )
    added = (f"add Subnet {subnet_id} ({identity.cidr})", delete)
    return _run_steps(client, family, (), assign, applied=(added,), rolled_back=deleted)


def edit_subnet(
    server: Server,
    family: Family,
    subnet_id: int,
    cidr: str,
    edit: SubnetEdit,
    *,
    shown_network: str | None,
    shared_network: str | None,
) -> ConfigChangeOutcome:
    """Set the fields of the Subnet with *subnet_id* that the edit form manages, and move it to *shared_network*.

    The operation acts only while that ID names the network *cidr* in the Shared Network *shown_network*: the
    Subnet that the operator saw. A Shared Network of None means none. The operation removes the Subnet from its
    Shared Network, adds it to the new one, and then updates the fields. When a step did not apply, it undoes the
    membership steps before it.
    """
    with _client(server, family) as client, _serialized(client, family):
        with mutation(server, family) as scope:
            subnet = _subnet_as_seen(scope, subnet_id, cidr, membership=True)
        current = subnet.shared_network.name if subnet.shared_network is not None else None
        if current != shown_network:
            # Another writer moved the Subnet, so a move to *shared_network* would undo that change.
            raise ConfigChangeRejected(
                "not-sent",
                (f"The Shared Network of Subnet {subnet_id} ({cidr}) changed in Kea. Reload the page and try again.",),
            )
        if shared_network is not None and shared_network != current:
            _require_shared_network(client, family, shared_network)
        definition = _read_before(lambda: client.subnet_definition(family, subnet_id))
        if definition.network != subnet.network:
            raise ConfigChangeRejected("not-sent", (subnet_changed(subnet_id, cidr),))
        moves: list[_Step] = []
        if shared_network != current:
            if current is not None:
                moves.append(_leave(server, client, family, subnet, current))
            if shared_network is not None:
                moves.append(_join(server, client, family, subnet, shared_network, len(moves) + 1))
        update = _Command(
            text=f"update the fields of Subnet {subnet_id}",
            send=lambda: client.subnet_update(definition, edit),
            # Not live while a fresh read returns the Subnet that the update started from.
            not_live=lambda: client.subnet_definition(family, subnet_id) == definition,
        )
        where = f"in Shared Network '{current}'" if current is not None else "outside all Shared Networks"
        moved_back = (
            f"A step of the change did not apply, so NetBox undid the steps before it. Subnet {subnet_id} ({cidr}) is "
            f"{where} again."
        )
        application, diagnostics = _run_steps(client, family, moves, update, rolled_back=moved_back)
        return _persisted(client, family, application, diagnostics)


def _leave(server: Server, client: KeaClient, family: Family, subnet: VerifiedSubnet, name: str) -> _Step:
    """Return step 1 of a move: remove *subnet* from the Shared Network *name*."""
    subnet_id, network = subnet.subnet_id, subnet.network
    return _Step(
        text=f"remove Subnet {subnet_id} from Shared Network '{name}'",
        send=lambda: client.network_subnet_del(family, name, subnet_id),
        not_live=lambda: _member_of(server, family, subnet_id, network, SharedNetworkMembership(name)),
        undo=_Undo(
            text=f"add Subnet {subnet_id} to Shared Network '{name}' to undo step 1",
            send=lambda: client.network_subnet_add(family, name, subnet_id),
            not_live=lambda: _not_joined(server, family, subnet_id, name),
            holds=lambda: _member_of(server, family, subnet_id, network, None),
        ),
    )


def _join(server: Server, client: KeaClient, family: Family, subnet: VerifiedSubnet, name: str, number: int) -> _Step:
    """Return step *number* of a move: add *subnet* to the Shared Network *name*."""
    subnet_id, network = subnet.subnet_id, subnet.network
    joined = SharedNetworkMembership(name)
    return _Step(
        text=f"add Subnet {subnet_id} to Shared Network '{name}'",
        send=lambda: client.network_subnet_add(family, name, subnet_id),
        not_live=lambda: _not_joined(server, family, subnet_id, name),
        undo=_Undo(
            text=f"remove Subnet {subnet_id} from Shared Network '{name}' to undo step {number}",
            send=lambda: client.network_subnet_del(family, name, subnet_id),
            not_live=lambda: _member_of(server, family, subnet_id, network, joined),
            holds=lambda: _member_of(server, family, subnet_id, network, joined),
        ),
    )


@dataclass(frozen=True)
class _Command:
    """One command of a multi-step change, and the fresh read that shows that the command is not live."""

    text: str
    send: Callable[[], None]
    not_live: Callable[[], bool]


@dataclass(frozen=True)
class _Undo(_Command):
    """A command that undoes an applied step. *holds* reads whether the target still holds what the step wrote."""

    holds: Callable[[], bool]


@dataclass(frozen=True)
class _Step(_Command):
    """A step that the operation undoes when a later step did not apply."""

    undo: _Undo


# The text of a step that applied, and the command that undoes it.
_Applied = tuple[str, _Undo]


def _run_steps(
    client: KeaClient,
    family: Family,
    steps: Sequence[_Step],
    last: _Command,
    *,
    applied: Sequence[_Applied] = (),
    rolled_back: str,
) -> tuple[Application, tuple[str, ...]]:
    """Send *steps* and then *last*, after the *applied* steps that already ran.

    After an unknown step, send nothing more. When a step did not apply, undo the applied steps, newest first.

    Raises:
        ConfigChangeRejected: If a step did not apply and nothing that the change wrote is live. *rolled_back* comes
            first in the diagnostics when the operation undid a step.

    """
    done = list(applied)
    for step in steps:
        result = _send_step(client, family, done, step, rolled_back)
        if result is not None:
            return result
        done.append((step.text, step.undo))
    return _send_step(client, family, done, last, rolled_back) or ("applied", ())


def _send_step(
    client: KeaClient, family: Family, done: Sequence[_Applied], command: _Command, rolled_back: str
) -> tuple[Application, tuple[str, ...]] | None:
    """Send the step after *done*. Return None when it applied, and the result of the change when it did not."""
    text = f"Step {len(done) + 1}, {command.text}"
    try:
        application, diagnostics = _mutate(client, family, command.send, not_live=command.not_live)
    except ConfigChangeRejected as rejection:
        return _roll_back(client, family, done, text, rejection, rolled_back)
    if application == "unknown":
        return "unknown", (*_applied_states(done), f"{text}: unknown.", *diagnostics)
    return None


def _roll_back(
    client: KeaClient,
    family: Family,
    done: Sequence[_Applied],
    failed: str,
    rejection: ConfigChangeRejected,
    rolled_back: str,
) -> tuple[Application, tuple[str, ...]]:
    """Undo the *done* steps, newest first, after the step *failed* did not apply. Stop at the first undo that fails.

    Raises:
        ConfigChangeRejected: If nothing that the change wrote is live: no step applied before, or every undo applied.

    """
    if not done:
        raise rejection
    states = [*_applied_states(done), f"{failed}: not applied.", *rejection.diagnostics]
    for number, (_text, undo) in enumerate(reversed(done), start=len(done) + 2):
        step = f"Step {number}, {undo.text}"
        problem = _undo(client, family, step, undo)
        if problem is not None:
            return "unknown", (*states, *problem)
        states.append(f"{step}: applied.")
    raise ConfigChangeRejected(rejection.reason, (rolled_back, *rejection.diagnostics)) from rejection


def _applied_states(done: Sequence[_Applied]) -> tuple[str, ...]:
    return tuple(f"Step {number}, {text}: applied." for number, (text, _undo) in enumerate(done, start=1))


def _undo(client: KeaClient, family: Family, step: str, undo: _Undo) -> tuple[str, ...] | None:
    """Send *undo* only while its target holds what the step wrote. Return None when it applied, else its diagnostics."""
    try:
        holds = undo.holds()
    except CatalogueUnavailable:
        logger.warning("The read before a rollback did not confirm the Subnet", exc_info=True)
        return (f"{step}: not sent, because NetBox could not read the Subnet again.",)
    if not holds:
        return (f"{step}: not sent, because the Subnet changed in Kea.",)
    try:
        application, diagnostics = _mutate(client, family, undo.send, not_live=undo.not_live)
    except ConfigChangeRejected as rejection:
        return (f"{step}: not applied.", *rejection.diagnostics)
    if application == "unknown":
        return (f"{step}: unknown.", *diagnostics)
    return None


def delete_subnet(server: Server, family: Family, subnet_id: int, cidr: str) -> ConfigChangeOutcome:
    """Delete the Subnet with *subnet_id*, only while that ID names the network *cidr*."""
    with _client(server, family) as client, _serialized(client, family):
        with mutation(server, family) as scope:
            subnet = _subnet_as_seen(scope, subnet_id, cidr)
        application, diagnostics = _mutate(
            client,
            family,
            lambda: client.subnet_del(family, subnet.subnet_id),
            not_live=lambda: _still_there(server, family, subnet.subnet_id, subnet.network),
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
    with _client(server, family) as client, _serialized(client, family):
        with mutation(server, family) as scope:
            subnet = _subnet_as_seen(scope, subnet_id, cidr)
        return _send_candidate(
            client,
            family,
            lambda candidate: candidate.set_subnet_options(subnet.subnet_id, subnet.network, rows),
            missing=subnet_changed(subnet_id, cidr),
        )


def set_server_options(server: Server, family: Family, rows: list[dict[str, Any]]) -> ConfigChangeOutcome:
    """Merge the options form *rows* into the server-global DHCP Options.

    Raises:
        DHCPOptionConflict: If a row names a DHCP Option that is missing or ambiguous, or the rows miss one.
        DHCPOptionNameChange: If a row renames a coded DHCP Option.

    """
    return _read_modify_write(server, family, lambda candidate: candidate.set_global_options(rows), missing=None)


def add_option_definition(server: Server, family: Family, option_def: dict[str, Any]) -> ConfigChangeOutcome:
    """Add the Option Definition *option_def*. Kea's config-test refuses a duplicate."""
    return _read_modify_write(
        server, family, lambda candidate: candidate.add_option_definition(option_def), missing=None
    )


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
    server: Server, family: Family, edit: Callable[[CandidateConfiguration], None], *, missing: str | None
) -> ConfigChangeOutcome:
    """Edit the running configuration and send it back with config-set, all under the lock."""
    with _client(server, family) as client, _serialized(client, family):
        return _send_candidate(client, family, edit, missing=missing)


def _send_candidate(
    client: KeaClient, family: Family, edit: Callable[[CandidateConfiguration], None], *, missing: str | None
) -> ConfigChangeOutcome:
    """Read the running configuration, edit it, test it and send it with config-set. The caller holds the lock.

    *missing* explains a target that *edit* does not find. None: *edit* names no target.
    """
    candidate = _read_before(lambda: client.config_candidate(family))
    _edit(candidate, edit, missing)
    _test_before(client, family, candidate)
    # config-set commits and then runs the hook initialization, so no failure result proves it is not live.
    application, diagnostics = _mutate(client, family, lambda: client.config_set(candidate), not_live=None)
    return _persisted(client, family, application, diagnostics)


def _edit(
    candidate: CandidateConfiguration, edit: Callable[[CandidateConfiguration], None], missing: str | None
) -> None:
    """Run *edit* on the candidate. An error in the submitted rows propagates unchanged."""
    try:
        edit(candidate)
    except CandidateTargetMissing as exc:
        if missing is None:
            raise
        raise ConfigChangeRejected("not-sent", (missing,)) from exc
    except MalformedConfiguration as exc:
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

    _before_change(test, _CONFIG_TEST_UNUSABLE)


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


def _subnet_as_seen(scope: MutationScope, subnet_id: int, cidr: str, *, membership: bool = False) -> VerifiedSubnet:
    """Return the Verified Subnet with *subnet_id* and the network *cidr*: the Subnet that the operator saw.

    *membership* True also requires a scope that confirms the Shared Network membership of the Subnet.
    """
    try:
        subnet = scope.find_with_membership(subnet_id) if membership else scope.find_by_id(subnet_id)
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


def _still_there(server: Server, family: Family, subnet_id: int, network: IPNetworkValue) -> bool:
    """Read in a fresh scope whether *subnet_id* still names *network*."""
    current = _subnet_now(server, family, subnet_id)
    return current is not None and current.network == network


def _member_now(server: Server, family: Family, subnet_id: int) -> VerifiedSubnet | None:
    """Read the Subnet with *subnet_id* in a fresh scope that confirms its Shared Network membership."""
    with mutation(server, family) as scope:
        return scope.find_with_membership(subnet_id)


def _member_of(
    server: Server, family: Family, subnet_id: int, network: IPNetworkValue, membership: SharedNetworkMembership | None
) -> bool:
    """Read in a fresh scope whether *subnet_id* still names *network* and has the Shared Network *membership*."""
    current = _member_now(server, family, subnet_id)
    return current is not None and current.network == network and current.shared_network == membership


def _not_joined(server: Server, family: Family, subnet_id: int, name: str) -> bool:
    """Read in a fresh scope whether the Subnet with *subnet_id* is outside the Shared Network *name*."""
    current = _member_now(server, family, subnet_id)
    return current is None or current.shared_network != SharedNetworkMembership(name)


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
    return _before_change(read, _READ_UNUSABLE)


def _before_change(step: Callable[[], T], unusable: str) -> T:
    """Run a step before the change. A failure means that the change was not sent; *unusable* explains a bad reply."""
    try:
        return step()
    except KeaException as exc:
        logger.warning("A step before a Configuration Change failed: %s", exc)
        raise ConfigChangeRejected("not-sent", (f"Kea replied: {exc.reply_text}",)) from exc
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        logger.warning("A step before a Configuration Change failed", exc_info=True)
        raise ConfigChangeRejected("not-sent", (unusable,)) from exc
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

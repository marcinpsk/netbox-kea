# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Configuration Change outcomes of ``config_write``, through a real Server and a real ``KeaClient``.

Only ``requests.Session.post`` is stubbed. Each test asserts the commands that reached Kea, because an
outcome alone cannot show a command that was sent when it must not be.
"""

import ipaddress
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import requests
from django.db import OperationalError, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from urllib3.exceptions import ProtocolError

from netbox_kea import config_write
from netbox_kea.config_write import ConfigChangeOutcome, ConfigChangeRejected
from netbox_kea.constants import Family
from netbox_kea.server_configuration import parse_pool

from .kea_stub import (
    _catalogue_responses_for_subnets,
    _http_response,
    _network_absent,
    _network_present,
    _refused_connection,
    queued,
    stub_kea,
)
from .utils import _PLUGINS_CONFIG, _make_db_server

_OK = {"result": 0, "text": "ok"}
_PERSIST = ["config-get", "config-test", "config-write"]
_CONTROL_AGENT = json.loads((Path(__file__).with_name("kea_recordings") / "control-agent.json").read_text())


def _config_get(version: int) -> dict:
    return _catalogue_responses_for_subnets(version, [])["config-get"]


def _responses(version: int, command: str, reply, *, before, after=None, **persist) -> dict:
    """Answer the read before the change, the change, a check read after it, and the persist step."""
    responses = {
        f"network{version}-get": before if after is None else queued(before, after),
        f"network{version}-{command}": reply,
        "config-get": _config_get(version),
        "config-test": _OK,
        "config-write": _OK,
    }
    responses.update(persist)
    return responses


def _add(name: str, reply, *, after=None, version: int = 4, **persist) -> dict:
    return _responses(version, "add", reply, before=_network_absent(name), after=after, **persist)


def _delete(name: str, reply, *, after=None, version: int = 4, **persist) -> dict:
    return _responses(version, "del", reply, before=_network_present(version, name), after=after, **persist)


def _reset_during_reply() -> requests.ConnectionError:
    """The error that requests raises when Kea resets the connection after it received the request."""
    return requests.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError(104, "reset")))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class OutcomeMatrixTests(TestCase):
    """Application applied or unknown, times each persistence state."""

    def setUp(self):
        self.server = _make_db_server()

    def test_applied_and_persisted(self):
        with stub_kea(_add("net-a", _OK)) as kea:
            outcome = config_write.add_shared_network(self.server, 4, "net-a")
        self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", *_PERSIST])
        self.assertEqual(kea.bodies("network4-get")[0]["arguments"], {"name": "net-a"})
        self.assertEqual(
            kea.bodies("network4-add")[0],
            {"command": "network4-add", "service": ["dhcp4"], "arguments": {"shared-networks": [{"name": "net-a"}]}},
        )
        live = _config_get(4)["arguments"]
        self.assertEqual(kea.bodies("config-test")[0]["arguments"], {"Dhcp4": live["Dhcp4"]})
        self.assertEqual(kea.bodies("config-write")[0], {"command": "config-write", "service": ["dhcp4"]})

    def test_applied_and_persistence_failed(self):
        failure = {"result": 1, "text": "Unable to open file /etc/kea/kea-dhcp4.conf for writing"}
        with stub_kea(_add("net-a", _OK, **{"config-write": failure})) as kea:
            outcome = config_write.add_shared_network(self.server, 4, "net-a")
        self.assertEqual(
            outcome,
            ConfigChangeOutcome(
                "applied", "failed", ("config-write failed: Unable to open file /etc/kea/kea-dhcp4.conf for writing",)
            ),
        )
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", *_PERSIST])

    def test_applied_and_persistence_not_requested(self):
        self.server.persist_config = False
        with stub_kea(_add("net-a", _OK)) as kea:
            outcome = config_write.add_shared_network(self.server, 4, "net-a")
        self.assertEqual(outcome, ConfigChangeOutcome("applied", "not-requested"))
        self.assertEqual(kea.commands(), ["network4-get", "network4-add"])

    def test_unknown_and_persisted(self):
        with stub_kea(_add("net-a", requests.ReadTimeout("read timed out"))) as kea:
            outcome = config_write.add_shared_network(self.server, 4, "net-a")
        self.assertEqual(
            outcome, ConfigChangeOutcome("unknown", "persisted", ("Kea's reply to the change was lost or unreadable.",))
        )
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", *_PERSIST])

    def test_unknown_and_persistence_failed(self):
        responses = _add("net-a", requests.ReadTimeout("read timed out"), **{"config-write": requests.ReadTimeout()})
        with stub_kea(responses) as kea:
            outcome = config_write.add_shared_network(self.server, 4, "net-a")
        self.assertEqual(
            outcome,
            ConfigChangeOutcome(
                "unknown",
                "failed",
                (
                    "Kea's reply to the change was lost or unreadable.",
                    "The reply to config-write was lost or unreadable.",
                ),
            ),
        )
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", *_PERSIST])

    def test_unknown_and_persistence_not_requested(self):
        self.server.persist_config = False
        with stub_kea(_add("net-a", requests.ReadTimeout("read timed out"))) as kea:
            outcome = config_write.add_shared_network(self.server, 4, "net-a")
        self.assertEqual(
            outcome,
            ConfigChangeOutcome("unknown", "not-requested", ("Kea's reply to the change was lost or unreadable.",)),
        )
        self.assertEqual(kea.commands(), ["network4-get", "network4-add"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class RejectionReasonTests(TestCase):
    """Each rejection reason that a Shared Network add or delete can raise. Nothing is persisted after one."""

    def setUp(self):
        self.server = _make_db_server()

    def _rejection(self, change) -> ConfigChangeRejected:
        with self.assertRaises(ConfigChangeRejected) as raised:
            change()
        return raised.exception

    def test_kea_rejected_an_add_when_the_network_is_still_absent(self):
        duplicate = {"result": 1, "text": "duplicate network 'net-a' found in the configuration"}
        with stub_kea(_add("net-a", duplicate, after=_network_absent("net-a"))) as kea:
            rejection = self._rejection(lambda: config_write.add_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "kea-rejected")
        self.assertEqual(rejection.diagnostics, ("Kea replied: duplicate network 'net-a' found in the configuration",))
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", "network4-get"])

    def test_kea_rejected_a_delete_when_the_network_is_still_present(self):
        failure = {"result": 1, "text": "network is in use"}
        with stub_kea(_delete("net-a", failure, after=_network_present(4, "net-a"))) as kea:
            rejection = self._rejection(lambda: config_write.delete_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "kea-rejected")
        self.assertEqual(kea.commands(), ["network4-get", "network4-del", "network4-get"])

    def test_an_add_of_an_existing_name_is_not_sent(self):
        with stub_kea(_responses(4, "add", _OK, before=_network_present(4, "net-a"))) as kea:
            rejection = self._rejection(lambda: config_write.add_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "not-sent")
        self.assertEqual(rejection.diagnostics, ("Shared Network 'net-a' already exists.",))
        self.assertEqual(kea.commands(), ["network4-get"])

    def test_a_delete_of_a_missing_name_is_not_sent(self):
        with stub_kea(_responses(4, "del", _OK, before=_network_absent("net-a"))) as kea:
            rejection = self._rejection(lambda: config_write.delete_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "not-sent")
        self.assertEqual(rejection.diagnostics, ("Shared Network 'net-a' not found.",))
        self.assertEqual(kea.commands(), ["network4-get"])

    def test_a_refused_connection_on_the_change_is_not_sent(self):
        refused = _refused_connection()
        for operation, responses, command in (
            (config_write.add_shared_network, _add("net-a", refused), "network4-add"),
            (config_write.delete_shared_network, _delete("net-a", refused), "network4-del"),
        ):
            with self.subTest(command):
                with stub_kea(responses) as kea, self.assertRaises(ConfigChangeRejected) as raised:
                    operation(self.server, 4, "net-a")
                self.assertEqual(raised.exception.reason, "not-sent")
                self.assertEqual(raised.exception.diagnostics, ("Kea could not be reached.",))
                self.assertEqual(kea.commands(), ["network4-get", command])

    def test_a_connect_timeout_on_the_change_is_not_sent(self):
        with stub_kea(_add("net-a", requests.ConnectTimeout("connect timed out"))) as kea:
            rejection = self._rejection(lambda: config_write.add_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "not-sent")
        self.assertEqual(kea.commands(), ["network4-get", "network4-add"])

    def test_a_refused_connection_on_the_read_before_the_change_is_not_sent(self):
        with stub_kea(_add("net-a", _OK, **{"network4-get": _refused_connection()})) as kea:
            rejection = self._rejection(lambda: config_write.add_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "not-sent")
        self.assertEqual(rejection.diagnostics, ("Kea did not return a usable reply to the read before the change.",))
        self.assertEqual(kea.commands(), ["network4-get"])

    def test_a_kea_failure_on_the_read_before_the_change_is_not_sent(self):
        unsupported = {"result": 2, "text": "'network4-get' command not supported."}
        with stub_kea(_add("net-a", _OK, **{"network4-get": unsupported})) as kea:
            rejection = self._rejection(lambda: config_write.add_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "not-sent")
        self.assertEqual(rejection.diagnostics, ("Kea replied: 'network4-get' command not supported.",))
        self.assertEqual(kea.commands(), ["network4-get"])

    def test_a_read_before_the_change_that_names_another_network_is_not_sent(self):
        with stub_kea(_delete("net-a", _OK, **{"network4-get": _network_present(4, "net-b")})) as kea:
            rejection = self._rejection(lambda: config_write.delete_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "not-sent")
        self.assertEqual(kea.commands(), ["network4-get"])

    def test_an_invalid_client_configuration_sends_nothing(self):
        self.server.client_cert_path = "/etc/kea/client.pem"
        with stub_kea({}) as kea:
            rejection = self._rejection(lambda: config_write.add_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "invalid-client-configuration")
        self.assertEqual(kea.commands(), [])

    def test_a_missing_client_certificate_file_is_an_invalid_client_configuration(self):
        # No stub: requests itself refuses the missing file before it opens a connection.
        self.server.ca_url = "https://127.0.0.1:1/"
        self.server.client_cert_path = "/nonexistent/netbox-kea-client.pem"
        self.server.client_key_path = "/nonexistent/netbox-kea-client.key"
        rejection = self._rejection(lambda: config_write.add_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "invalid-client-configuration")
        self.assertEqual(
            rejection.diagnostics, ("A TLS certificate, key or CA file of the Server could not be found.",)
        )

    def test_a_missing_tls_file_on_the_change_is_an_invalid_client_configuration(self):
        # requests raises a plain OSError for a missing TLS file before it sends the request.
        missing = OSError("Could not find the TLS certificate file, invalid path: /nonexistent/client.pem")
        with stub_kea(_add("net-a", missing)) as kea:
            rejection = self._rejection(lambda: config_write.add_shared_network(self.server, 4, "net-a"))
        self.assertEqual(rejection.reason, "invalid-client-configuration")
        self.assertEqual(
            rejection.diagnostics, ("A TLS certificate, key or CA file of the Server could not be found.",)
        )
        self.assertEqual(kea.commands(), ["network4-get", "network4-add"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class ApplicationTests(TestCase):
    """How the reply to the command that can change the configuration decides the application."""

    def setUp(self):
        self.server = _make_db_server()

    def _both(self, reply, *, after=None) -> list:
        """Run an add and a delete of 'net-a' with *reply* to the change; return each command, outcome and stub."""
        runs = []
        for operation, responses, command in (
            (config_write.add_shared_network, _add("net-a", reply, after=after), "network4-add"),
            (config_write.delete_shared_network, _delete("net-a", reply, after=after), "network4-del"),
        ):
            with stub_kea(responses) as kea:
                runs.append((command, operation(self.server, 4, "net-a"), kea))
        return runs

    def test_a_read_timeout_is_unknown(self):
        for command, outcome, kea in self._both(requests.ReadTimeout("read timed out")):
            with self.subTest(command):
                self.assertEqual(outcome.application, "unknown")
                self.assertEqual(outcome.persistence, "persisted")
                self.assertEqual(kea.commands(), ["network4-get", command, *_PERSIST])

    def test_a_reset_during_the_reply_is_unknown(self):
        for command, outcome, _ in self._both(_reset_during_reply()):
            with self.subTest(command):
                self.assertEqual(outcome.application, "unknown")

    def test_an_http_error_is_unknown(self):
        for command, outcome, _ in self._both(_http_response({"error": "bad gateway"}, status=502)):
            with self.subTest(command):
                self.assertEqual(outcome.application, "unknown")

    def test_a_malformed_reply_is_unknown(self):
        for command, outcome, _ in self._both([_OK, _OK]):
            with self.subTest(command):
                self.assertEqual(outcome.application, "unknown")
                self.assertEqual(outcome.diagnostics[0], "Kea's reply to the change was lost or unreadable.")

    def test_result_5_is_unknown_without_a_check_read(self):
        fatal = {"result": 5, "text": "configuration could not be restored"}
        for command, outcome, kea in self._both(fatal):
            with self.subTest(command):
                self.assertEqual(outcome.application, "unknown")
                self.assertEqual(outcome.diagnostics[0], "Kea replied: configuration could not be restored")
                self.assertEqual(kea.commands(), ["network4-get", command, *_PERSIST])

    def test_a_control_agent_forwarding_failure_is_unknown_without_a_check_read(self):
        for family in (4, 6):
            for operation, command, responses in (
                (config_write.add_shared_network, "add", _add),
                (config_write.delete_shared_network, "del", _delete),
            ):
                recorded = _CONTROL_AGENT[f"network{family}-{command}"]
                with self.subTest(family=family, command=command):
                    with stub_kea(responses("net-a", recorded, version=family)) as kea:
                        outcome = operation(self.server, family, "net-a")
                    self.assertEqual(outcome.application, "unknown")
                    self.assertEqual(outcome.diagnostics[0], f"Kea replied: {recorded['text']}")
                    self.assertEqual(kea.commands()[2:], _PERSIST)

    def test_the_forwarding_text_from_a_direct_daemon_is_an_ordinary_failure(self):
        self.server.has_control_agent = False
        recorded = _CONTROL_AGENT["network4-add"]
        with stub_kea(_add("net-a", recorded, after=_network_absent("net-a"))) as kea:
            with self.assertRaises(ConfigChangeRejected) as raised:
                config_write.add_shared_network(self.server, 4, "net-a")
        self.assertEqual(raised.exception.reason, "kea-rejected")
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", "network4-get"])

    def test_a_failure_whose_change_is_visible_is_unknown(self):
        failure = {"result": 1, "text": "subnet allocator initialization failed"}
        cases = (
            (config_write.add_shared_network, _add("net-a", failure, after=_network_present(4, "net-a"))),
            (config_write.delete_shared_network, _delete("net-a", failure, after=_network_absent("net-a"))),
        )
        for operation, responses in cases:
            with self.subTest(operation.__name__):
                with stub_kea(responses) as kea:
                    outcome = operation(self.server, 4, "net-a")
                self.assertEqual(outcome.application, "unknown")
                self.assertEqual(
                    outcome.diagnostics[:2],
                    (
                        "Kea replied: subnet allocator initialization failed",
                        "The read after the failure shows the change.",
                    ),
                )
                self.assertEqual(kea.commands()[2:], ["network4-get", *_PERSIST])

    def test_a_failure_whose_check_read_fails_is_unknown(self):
        failure = {"result": 1, "text": "failed"}
        for command, outcome, kea in self._both(failure, after=requests.ReadTimeout("read timed out")):
            with self.subTest(command):
                self.assertEqual(outcome.application, "unknown")
                self.assertEqual(outcome.diagnostics[1], "The read after the failure did not succeed.")
                self.assertEqual(kea.commands(), ["network4-get", command, "network4-get", *_PERSIST])

    def test_a_delete_sends_the_name(self):
        with stub_kea(_delete("net-a", _OK, version=6)) as kea:
            outcome = config_write.delete_shared_network(self.server, 6, "net-a")
        self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(
            kea.bodies("network6-del"),
            [{"command": "network6-del", "service": ["dhcp6"], "arguments": {"name": "net-a"}}],
        )


_POOLS = {4: "10.0.0.10-10.0.0.20", 6: "2001:db8:1::100-2001:db8:1::1ff"}
_NEW_POOLS = {4: "10.0.0.100-10.0.0.110", 6: "2001:db8:1::200-2001:db8:1::2ff"}
_SEEN = {4: "10.0.0.0/24", 6: "2001:db8:1::/64"}
_MOVED = {4: "10.0.1.0/24", 6: "2001:db8:2::/64"}
_HOST_BITS = {4: "10.0.0.5/24", 6: "2001:db8:1::5/64"}


def _subnet(version: int, *, cidr: str | None = None, pools: tuple[str, ...] | None = None) -> dict:
    """Subnet 1 as Kea declares it, with the Pool of *version* unless *pools* says otherwise."""
    pools = (_POOLS[version],) if pools is None else pools
    return {"id": 1, "subnet": cidr or _SEEN[version], "pools": [{"pool": pool} for pool in pools]}


def _subnet_change(version: int, *states: dict | None, **overrides) -> dict:
    """Answer one Subnet Catalogue read per state in turn, each change, and the persist step.

    A state of None is a Server without Subnets. The last state repeats, and its config-get also answers the persist step.
    """
    reads = [_catalogue_responses_for_subnets(version, [state] if state else []) for state in states]
    responses = {
        f"subnet{version}-list": queued(*(read[f"subnet{version}-list"] for read in reads)),
        "config-get": queued(*(read["config-get"] for read in reads)),
        f"subnet{version}-del": _OK,
        f"subnet{version}-delta-add": _OK,
        f"subnet{version}-delta-del": _OK,
        "config-test": _OK,
        "config-write": _OK,
    }
    responses.update(overrides)
    return responses


def _pool(version: int, text: str):
    return parse_pool(text, ipaddress.ip_network(_SEEN[version]))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class SubnetAndPoolChangeTests(TestCase):
    """Subnet delete, Pool add and Pool delete run only on the Verified Subnet with the ID and CIDR the operator saw."""

    def setUp(self):
        self.server = _make_db_server()

    def _operations(self, version: Family):
        """Each operation on Subnet 1 as the operator saw it: its name, the call, and its Kea command."""
        seen = _SEEN[version]
        return (
            (
                "delete_subnet",
                lambda: config_write.delete_subnet(self.server, version, 1, seen),
                f"subnet{version}-del",
            ),
            (
                "add_pool",
                lambda: config_write.add_pool(self.server, version, 1, seen, _pool(version, _NEW_POOLS[version])),
                f"subnet{version}-delta-add",
            ),
            (
                "delete_pool",
                lambda: config_write.delete_pool(self.server, version, 1, seen, _pool(version, _POOLS[version])),
                f"subnet{version}-delta-del",
            ),
        )

    def test_each_change_is_applied_and_persisted_with_the_verified_subnet(self):
        for version in (4, 6):
            bodies = {
                f"subnet{version}-del": {"id": 1},
                f"subnet{version}-delta-add": {
                    f"subnet{version}": [{"id": 1, "subnet": _SEEN[version], "pools": [{"pool": _NEW_POOLS[version]}]}]
                },
                f"subnet{version}-delta-del": {
                    f"subnet{version}": [{"id": 1, "subnet": _SEEN[version], "pools": [{"pool": _POOLS[version]}]}]
                },
            }
            for name, operation, command in self._operations(version):
                with self.subTest(version=version, operation=name):
                    with stub_kea(_subnet_change(version, _subnet(version))) as kea:
                        outcome = operation()
                    self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
                    self.assertEqual(kea.commands(), [f"subnet{version}-list", "config-get", command, *_PERSIST])
                    self.assertEqual([body["arguments"] for body in kea.bodies(command)], [bodies[command]])

    def test_the_delta_commands_carry_the_subnet_text_that_kea_declares(self):
        for version in (4, 6):
            declared = _subnet(version, cidr=_HOST_BITS[version])
            for name, pool, command in (
                ("add_pool", _NEW_POOLS[version], f"subnet{version}-delta-add"),
                ("delete_pool", _POOLS[version], f"subnet{version}-delta-del"),
            ):
                with self.subTest(version=version, operation=name):
                    operation = getattr(config_write, name)
                    with stub_kea(_subnet_change(version, declared)) as kea:
                        outcome = operation(self.server, version, 1, _SEEN[version], _pool(version, pool))
                    self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
                    self.assertEqual(
                        kea.bodies(command)[0]["arguments"],
                        {f"subnet{version}": [{"id": 1, "subnet": _HOST_BITS[version], "pools": [{"pool": pool}]}]},
                    )
                    self.assertNotIn(f"subnet{version}-get", kea.commands())

    def test_an_id_that_names_another_cidr_or_nothing_is_not_sent(self):
        for version in (4, 6):
            for state in (_subnet(version, cidr=_MOVED[version]), None):
                for name, operation, _command in self._operations(version):
                    with self.subTest(version=version, operation=name, state=state):
                        with stub_kea(_subnet_change(version, state)) as kea:
                            with self.assertRaises(ConfigChangeRejected) as raised:
                                operation()
                        self.assertEqual(raised.exception.reason, "not-sent")
                        self.assertEqual(
                            raised.exception.diagnostics,
                            (f"Subnet 1 ({_SEEN[version]}) changed in Kea. Reload the page and try again.",),
                        )
                        self.assertEqual(kea.commands(), [f"subnet{version}-list", "config-get"])

    def test_the_cidr_that_kea_declares_with_host_bits_names_the_same_subnet(self):
        with stub_kea(_subnet_change(4, _subnet(4))) as kea:
            outcome = config_write.delete_subnet(self.server, 4, 1, _HOST_BITS[4])
        self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
        self.assertIn("subnet4-del", kea.commands())

    def test_an_incomplete_identity_observation_is_not_sent_and_does_not_read_as_absent(self):
        for version in (4, 6):
            failed = {"result": 1, "text": "internal error"}
            for name, operation, _command in self._operations(version):
                with self.subTest(version=version, operation=name):
                    responses = _subnet_change(version, _subnet(version), **{f"subnet{version}-list": failed})
                    with stub_kea(responses) as kea, self.assertRaises(ConfigChangeRejected) as raised:
                        operation()
                    self.assertEqual(raised.exception.reason, "not-sent")
                    self.assertEqual(
                        raised.exception.diagnostics,
                        (
                            "NetBox could not confirm Kea's Subnet list, so it did not send the change. Try again later.",
                        ),
                    )
                    self.assertEqual(kea.commands(), [f"subnet{version}-list", "config-get"])

    def test_a_failure_whose_target_shows_no_change_is_a_kea_rejection(self):
        failure = {"result": 1, "text": "command failed"}
        for version in (4, 6):
            for name, operation, command in self._operations(version):
                with self.subTest(version=version, operation=name):
                    with stub_kea(_subnet_change(version, _subnet(version), **{command: failure})) as kea:
                        with self.assertRaises(ConfigChangeRejected) as raised:
                            operation()
                    self.assertEqual(raised.exception.reason, "kea-rejected")
                    self.assertEqual(raised.exception.diagnostics, ("Kea replied: command failed",))
                    self.assertEqual(
                        kea.commands(),
                        [f"subnet{version}-list", "config-get", command, f"subnet{version}-list", "config-get"],
                    )

    def test_a_failure_whose_target_shows_the_change_is_unknown(self):
        failure = {"result": 1, "text": "allocator initialization failed"}
        for version in (4, 6):
            with_both = _subnet(version, pools=(_POOLS[version], _NEW_POOLS[version]))
            changed = {
                f"subnet{version}-del": None,
                f"subnet{version}-delta-add": with_both,
                f"subnet{version}-delta-del": _subnet(version, pools=()),
            }
            for name, operation, command in self._operations(version):
                with self.subTest(version=version, operation=name):
                    responses = _subnet_change(version, _subnet(version), changed[command], **{command: failure})
                    with stub_kea(responses) as kea:
                        outcome = operation()
                    self.assertEqual(outcome.application, "unknown")
                    self.assertEqual(outcome.persistence, "persisted")
                    self.assertEqual(
                        outcome.diagnostics,
                        (
                            "Kea replied: allocator initialization failed",
                            "The read after the failure shows the change.",
                        ),
                    )
                    self.assertEqual(kea.commands()[-5:], [f"subnet{version}-list", "config-get", *_PERSIST])

    def test_a_failure_whose_target_read_fails_is_unknown(self):
        failure = {"result": 1, "text": "command failed"}
        listed = _catalogue_responses_for_subnets(4, [_subnet(4)])["subnet4-list"]
        for name, operation, command in self._operations(4):
            with self.subTest(operation=name):
                unreadable = queued(listed, requests.ReadTimeout())
                responses = _subnet_change(4, _subnet(4), **{command: failure, "subnet4-list": unreadable})
                with stub_kea(responses):
                    outcome = operation()
                self.assertEqual(outcome.application, "unknown")
                self.assertEqual(outcome.diagnostics[1], "The read after the failure did not succeed.")

    def test_a_pool_failure_whose_target_lacks_configuration_facts_is_unknown(self):
        failure = {"result": 1, "text": "command failed"}
        catalogue = _catalogue_responses_for_subnets(4, [_subnet(4)])["config-get"]
        for name, operation, command in self._operations(4)[1:]:
            with self.subTest(operation=name):
                # Only the check read gets no configuration, so it cannot show the Pools.
                config_get = queued(catalogue, requests.ReadTimeout(), catalogue)
                responses = _subnet_change(4, _subnet(4), **{command: failure, "config-get": config_get})
                with stub_kea(responses):
                    outcome = operation()
                self.assertEqual(outcome.application, "unknown")
                self.assertEqual(outcome.persistence, "persisted")
                self.assertEqual(outcome.diagnostics[1], "The read after the failure did not succeed.")

    def test_a_pool_that_the_subnet_does_not_hold_or_already_holds_is_not_sent(self):
        for version in (4, 6):
            seen, held, absent = _SEEN[version], _POOLS[version], _NEW_POOLS[version]
            for name, pool, diagnostic in (
                ("delete_pool", absent, f"Subnet 1 ({seen}) has no Pool {absent}. Reload the page and try again."),
                ("add_pool", held, f"Subnet 1 ({seen}) already has Pool {held}. Reload the page and try again."),
            ):
                with self.subTest(version=version, operation=name):
                    with stub_kea(_subnet_change(version, _subnet(version))) as kea:
                        with self.assertRaises(ConfigChangeRejected) as raised:
                            getattr(config_write, name)(self.server, version, 1, seen, _pool(version, pool))
                    self.assertEqual(raised.exception.reason, "not-sent")
                    self.assertEqual(raised.exception.diagnostics, (diagnostic,))
                    self.assertEqual(kea.commands(), [f"subnet{version}-list", "config-get"])

    def test_without_configuration_facts_kea_decides_on_the_pool(self):
        catalogue = _catalogue_responses_for_subnets(4, [_subnet(4)])["config-get"]
        for name, pool, command in (
            ("delete_pool", _NEW_POOLS[4], "subnet4-delta-del"),
            ("add_pool", _POOLS[4], "subnet4-delta-add"),
        ):
            with self.subTest(operation=name):
                # Only the scope read gets no configuration. The persist step reads it.
                responses = _subnet_change(4, _subnet(4), **{"config-get": queued(requests.ReadTimeout(), catalogue)})
                with stub_kea(responses) as kea:
                    outcome = getattr(config_write, name)(self.server, 4, 1, _SEEN[4], _pool(4, pool))
                self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
                self.assertIn(command, kea.commands())

    def test_a_delete_of_a_pool_that_kea_declares_as_a_prefix_sends_its_range(self):
        """Kea 3.2.0 matches a Pool by its addresses, so the range form deletes a Pool declared as a prefix."""
        for version, prefix, sent in (
            (4, "10.0.0.0/28", "10.0.0.0-10.0.0.15"),
            (6, "2001:db8:1::/120", "2001:db8:1::-2001:db8:1::ff"),
        ):
            with self.subTest(version=version):
                declared = _subnet(version, pools=(prefix,))
                with stub_kea(_subnet_change(version, declared)) as kea:
                    outcome = config_write.delete_pool(self.server, version, 1, _SEEN[version], _pool(version, prefix))
                self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
                self.assertEqual(
                    kea.bodies(f"subnet{version}-delta-del")[0]["arguments"][f"subnet{version}"][0]["pools"],
                    [{"pool": sent}],
                )

    def test_a_pool_failure_whose_subnet_id_now_names_another_network_is_unknown(self):
        """The check read cannot find the Subnet that the page showed, so it cannot tell whether the Pool is live."""
        failure = {"result": 1, "text": "command failed"}
        moved = _subnet(4, cidr=_MOVED[4], pools=())
        for name, operation, command in self._operations(4)[1:]:
            with self.subTest(operation=name):
                with stub_kea(_subnet_change(4, _subnet(4), moved, **{command: failure})):
                    outcome = operation()
                self.assertEqual(outcome.application, "unknown")
                self.assertEqual(outcome.persistence, "persisted")
                self.assertEqual(outcome.diagnostics[1], "The read after the failure did not succeed.")

    def test_a_failed_pool_add_whose_subnet_is_gone_is_a_kea_rejection(self):
        failure = {"result": 1, "text": "command failed"}
        with stub_kea(_subnet_change(4, _subnet(4), None, **{"subnet4-delta-add": failure})):
            with self.assertRaises(ConfigChangeRejected) as raised:
                config_write.add_pool(self.server, 4, 1, _SEEN[4], _pool(4, _NEW_POOLS[4]))
        self.assertEqual(raised.exception.reason, "kea-rejected")

    def test_result_5_a_forwarding_failure_and_a_malformed_reply_are_unknown_and_still_persisted(self):
        for label, reply in (
            ("result 5", {"result": 5, "text": "configuration could not be restored"}),
            ("forwarding failure", _CONTROL_AGENT["network4-add"]),
            ("malformed reply", [_OK, _OK]),
        ):
            for name, operation, command in self._operations(4):
                with self.subTest(reply=label, operation=name):
                    with stub_kea(_subnet_change(4, _subnet(4), **{command: reply})) as kea:
                        outcome = operation()
                    self.assertEqual(outcome.application, "unknown")
                    self.assertEqual(outcome.persistence, "persisted")
                    # No check read follows the failure.
                    self.assertEqual(kea.commands(), ["subnet4-list", "config-get", command, *_PERSIST])

    def test_a_lost_reply_is_unknown_and_still_persisted(self):
        for name, operation, command in self._operations(4):
            with self.subTest(operation=name):
                with stub_kea(_subnet_change(4, _subnet(4), **{command: requests.ReadTimeout()})) as kea:
                    outcome = operation()
                self.assertEqual(
                    outcome,
                    ConfigChangeOutcome("unknown", "persisted", ("Kea's reply to the change was lost or unreadable.",)),
                )
                self.assertEqual(kea.commands(), ["subnet4-list", "config-get", command, *_PERSIST])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class PersistStepTests(TestCase):
    """Each phase of the persist step, after an applied add."""

    def setUp(self):
        self.server = _make_db_server()

    def _add(self, **persist) -> tuple[ConfigChangeOutcome, list[str]]:
        with stub_kea(_add("net-a", _OK, **persist)) as kea:
            outcome = config_write.add_shared_network(self.server, 4, "net-a")
        self.assertEqual(outcome.application, "applied")
        return outcome, kea.commands()[2:]

    def test_a_failed_config_get_writes_nothing(self):
        outcome, persist = self._add(**{"config-get": requests.ReadTimeout()})
        self.assertEqual(outcome.persistence, "failed")
        self.assertEqual(outcome.diagnostics, ("Kea did not return its running configuration, so it was not saved.",))
        self.assertEqual(persist, ["config-get"])

    def test_a_config_test_rejection_of_the_live_configuration_writes_nothing(self):
        outcome, persist = self._add(**{"config-test": {"result": 1, "text": "subnet overlaps"}})
        self.assertEqual(outcome.persistence, "failed")
        self.assertEqual(outcome.diagnostics, ("config-test rejected the running configuration: subnet overlaps",))
        self.assertEqual(persist, ["config-get", "config-test"])

    def test_a_lost_config_test_reply_writes_nothing(self):
        outcome, persist = self._add(**{"config-test": requests.ReadTimeout()})
        self.assertEqual(outcome.persistence, "failed")
        self.assertEqual(persist, ["config-get", "config-test"])

    def test_an_unsupported_config_test_still_writes(self):
        outcome, persist = self._add(**{"config-test": {"result": 2, "text": "'config-test' command not supported."}})
        self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(persist, _PERSIST)

    def test_a_lost_config_write_reply_is_a_failed_persistence(self):
        outcome, persist = self._add(**{"config-write": requests.ReadTimeout()})
        self.assertEqual(outcome.persistence, "failed")
        self.assertEqual(outcome.diagnostics, ("The reply to config-write was lost or unreadable.",))
        self.assertEqual(persist, _PERSIST)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class LockTests(TransactionTestCase):
    """Operations on one Kea daemon and family run one at a time, from the first read to the end of persist.

    The first operation stops inside its config-write, the last command it sends under the lock, until the
    test releases it. Each thread gets its own database connection.
    """

    def setUp(self) -> None:
        self.holder = _make_db_server(name="holder")
        self.same_url = _make_db_server(name="same-url")
        self.other_url = _make_db_server(name="other-url", ca_url="https://other.example.com")
        self.holding = threading.Event()
        self.release = threading.Event()
        self.results: dict[str, object] = {}

    def tearDown(self):
        self.release.set()

    def _responses(self) -> dict:
        def network_get(body):
            return _network_absent(body["arguments"]["name"])

        def config_get(body):
            return _config_get(6 if body.get("service") == ["dhcp6"] else 4)

        def config_write_reply(body):
            if threading.current_thread().name == "holder":
                self.holding.set()
                if not self.release.wait(timeout=30):
                    raise AssertionError("The test never released the holding operation.")
            return _OK

        return {
            "network4-get": network_get,
            "network6-get": network_get,
            "network4-add": _OK,
            "network6-add": _OK,
            "config-get": config_get,
            "config-test": _OK,
            "config-write": config_write_reply,
        }

    def _start(self, name: str, server, family: int, network: str) -> threading.Thread:
        def run():
            try:
                self.results[name] = config_write.add_shared_network(server, family, network)
            except ConfigChangeRejected as rejection:
                self.results[name] = rejection
            finally:
                connection.close()

        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread

    def _hold(self) -> threading.Thread:
        thread = self._start("holder", self.holder, 4, "held")
        self.assertTrue(self.holding.wait(timeout=30), "The holding operation never reached config-write.")
        return thread

    @staticmethod
    def _names(kea) -> list[str]:
        return [body["arguments"]["name"] for body in kea.requests if body.get("command") == "network4-get"]

    def _wait_until_a_lock_waits(self) -> None:
        deadline = time.monotonic() + 30
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                )
                if cursor.fetchone()[0]:
                    return
                time.sleep(0.01)
        self.fail("No operation waited for the advisory lock.")

    def test_a_subnet_change_reads_its_scope_only_under_the_lock(self):
        subnet = _subnet(4)
        catalogue = _catalogue_responses_for_subnets(4, [subnet])
        waiter_result = {}

        def run_delete():
            try:
                waiter_result["delete"] = config_write.delete_subnet(self.same_url, 4, 1, _SEEN[4])
            finally:
                connection.close()

        responses = {
            **self._responses(),
            "subnet4-list": catalogue["subnet4-list"],
            "config-get": catalogue["config-get"],
            "subnet4-del": _OK,
        }
        with stub_kea(responses) as kea:
            holder = self._hold()
            waiter = threading.Thread(target=run_delete, name="waiter", daemon=True)
            waiter.start()
            self._wait_until_a_lock_waits()
            self.assertNotIn("subnet4-list", kea.commands())
            self.release.set()
            holder.join(timeout=30)
            waiter.join(timeout=30)
        self.assertEqual(waiter_result["delete"], ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(
            kea.commands(),
            ["network4-get", "network4-add", *_PERSIST, "subnet4-list", "config-get", "subnet4-del", *_PERSIST],
        )

    def test_a_second_operation_on_the_same_url_waits_for_the_first(self):
        with stub_kea(self._responses()) as kea:
            holder = self._hold()
            waiter = self._start("waiter", self.same_url, 4, "waited")
            self._wait_until_a_lock_waits()
            self.assertEqual(self._names(kea), ["held"])
            self.release.set()
            holder.join(timeout=30)
            waiter.join(timeout=30)
        self.assertEqual(self.results["holder"], ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(self.results["waiter"], ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", *_PERSIST] * 2)
        self.assertEqual(self._names(kea), ["held", "waited"])

    def test_a_second_operation_is_not_sent_when_the_wait_expires(self):
        with stub_kea(self._responses()) as kea, patch.object(config_write, "LOCK_WAIT_SECONDS", 0.5):
            holder = self._hold()
            started = time.monotonic()
            with self.assertRaises(ConfigChangeRejected) as raised:
                config_write.add_shared_network(self.same_url, 4, "rejected")
            waited = time.monotonic() - started
            self.release.set()
            holder.join(timeout=30)
        self.assertEqual(raised.exception.reason, "not-sent")
        self.assertEqual(
            raised.exception.diagnostics, ("Another change to this Kea server is still running. Try again later.",)
        )
        self.assertGreaterEqual(waited, 0.5)
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", *_PERSIST])
        self.assertEqual(self._names(kea), ["held"])
        self.assertEqual(self.results["holder"], ConfigChangeOutcome("applied", "persisted"))

    def test_the_other_family_and_a_different_url_do_not_wait(self):
        with stub_kea(self._responses()) as kea, patch.object(config_write, "LOCK_WAIT_SECONDS", 0.5):
            holder = self._hold()
            other_family = config_write.add_shared_network(self.holder, 6, "other-family")
            other_url = config_write.add_shared_network(self.other_url, 4, "other-url")
            self.release.set()
            holder.join(timeout=30)
        self.assertEqual(other_family, ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(other_url, ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(self.results["holder"], ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(kea.bodies("network6-get")[0]["arguments"], {"name": "other-family"})
        self.assertEqual(self._names(kea), ["held", "other-url"])

    def test_the_lock_wait_does_not_leak_into_the_rest_of_the_transaction(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            (before,) = cursor.fetchone()
        seen: list[str] = []

        def config_write_reply(body):
            with connection.cursor() as cursor:
                cursor.execute("SHOW lock_timeout")
                seen.append(cursor.fetchone()[0])
            return _OK

        with stub_kea({**self._responses(), "config-write": config_write_reply}):
            config_write.add_shared_network(self.holder, 4, "net-a")
        self.assertEqual(seen, [before])

    def test_a_database_error_other_than_the_lock_wait_is_not_a_rejection(self):
        with stub_kea(self._responses()) as kea, patch.object(config_write, "LOCK_WAIT_SECONDS", 30):
            holder = self._hold()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = 200")
                with self.assertRaises(OperationalError) as raised:
                    config_write.add_shared_network(self.same_url, 4, "cancelled")
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("RESET statement_timeout")
            self.release.set()
            holder.join(timeout=30)
        self.assertEqual(raised.exception.__cause__.sqlstate, "57014")
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", *_PERSIST])
        self.assertEqual(self._names(kea), ["held"])
        self.assertEqual(self.results["holder"], ConfigChangeOutcome("applied", "persisted"))

    def test_a_failed_commit_after_the_change_keeps_the_outcome(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            (pid,) = cursor.fetchone()

        def terminate_then_ok(body):
            killer = connections.create_connection("default")
            try:
                with killer.cursor() as cursor:
                    cursor.execute("SELECT pg_terminate_backend(%s, 5000)", [pid])
                    self.assertTrue(cursor.fetchone()[0])
            finally:
                killer.close()
            return _OK

        with stub_kea({**self._responses(), "network4-add": terminate_then_ok}) as kea:
            with self.assertLogs(config_write.logger, "WARNING") as logs:
                outcome = config_write.add_shared_network(self.holder, 4, "net-a")
        self.assertEqual(outcome, ConfigChangeOutcome("applied", "persisted"))
        self.assertEqual(kea.commands(), ["network4-get", "network4-add", *_PERSIST])
        self.assertIsNotNone(logs.records[-1].exc_info)

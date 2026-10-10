# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Lease actions address the shown allocation and refuse stale facts: real pages, forms, views and KeaClient.

Only the Kea HTTP boundary is stubbed, by a lease store that answers with the replies recorded from a real Kea 3.2.
"""

from __future__ import annotations

import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse
from django.utils.html import escape

from netbox_kea import signals
from netbox_kea.leases import DHCPv4LeaseRequest, DHCPv6PrefixLease

from .kea_stub import LeaseDaemon, _catalogue_responses_for_subnets, lease_record, stub_kea
from .utils import _PLUGINS_CONFIG, _ViewTestBase

_SUBNETS = {4: [{"id": 10, "subnet": "192.0.2.0/24"}], 6: [{"id": 10, "subnet": "2001:db8:1::/64"}]}
_PREFIX = "2001:db8:100:600::"
_CONTEXT = {"site": {"rack": "r1", "slots": [1, 2]}}


def _address4(**changes) -> dict:
    facts = {"subnet_id": 10, "hostname": "host40.example.org", "hw_address": "aa:bb:cc:00:00:40", **changes}
    return lease_record("192.0.2.40", **facts)


def _prefix(**changes) -> dict:
    facts = {"type": "IA_PD", "prefix_len": 56, "subnet_id": 10, "iaid": 30, "hostname": "pd.example.org", **changes}
    return lease_record(_PREFIX, drop=("hw-address", "pool-id"), **facts)


class _Inputs(HTMLParser):
    """Collect the values of the named inputs of a page, as a browser would submit them."""

    def __init__(self, name: str | None = None) -> None:
        super().__init__()
        self.name = name
        self.values: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag != "input" or "name" not in attributes or attributes["name"] == "csrfmiddlewaretoken":
            return
        if self.name is not None and attributes["name"] != self.name:
            return
        self.values.setdefault(attributes["name"], []).append(attributes.get("value") or "")


def _inputs(response, name: str | None = None) -> dict[str, list[str]]:
    parser = _Inputs(name)
    parser.feed(response.content.decode())
    return parser.values


def _messages(response) -> list[str]:
    return [str(message) for message in get_messages(response.wsgi_request)]


class _Received:
    """A real receiver connected to one plugin signal for the duration of a test."""

    def __init__(self, test, signal) -> None:
        self.calls: list[dict] = []
        signal.connect(self._receive)
        test.addCleanup(signal.disconnect, self._receive)

    def _receive(self, sender, **kwargs):
        self.calls.append(kwargs)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class LeaseEditTest(_ViewTestBase):
    """Edit derives the written fields from the shown values and checks a fresh read before it writes."""

    def _url(self, family: int, target: str) -> str:
        return reverse(f"plugins:netbox_kea:server_lease{family}_edit", args=[self.server.pk, target])

    def _shown(self, daemon: LeaseDaemon, family: int, target: str) -> dict[str, list[str]]:
        with stub_kea(daemon.responses()):
            response = self.client.get(self._url(family, target))
        self.assertEqual(response.status_code, 200)
        return _inputs(response)

    def _save(self, daemon: LeaseDaemon, family: int, target: str, form: dict, **changes):
        with stub_kea(daemon.responses()) as kea:
            response = self.client.post(self._url(family, target), {**form, **changes})
        return kea, response

    def test_an_unchanged_prefilled_form_sends_no_update(self):
        daemon = LeaseDaemon(4, _address4())
        form = self._shown(daemon, 4, "192.0.2.40")

        kea, response = self._save(daemon, 4, "192.0.2.40", form)

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("lease4-update", kea.commands())
        self.assertEqual(_messages(response), ["Lease 192.0.2.40 was not changed: the form holds the shown values."])

    def test_a_changed_binding_or_subnet_sends_no_update(self):
        cases = {
            "binding": {"hw-address": "aa:bb:cc:00:00:99"},
            "client identifier": {"client-id": "01:aa:bb:cc:00:00:99"},
            "Subnet": {"subnet-id": 11},
        }
        for name, change in cases.items():
            with self.subTest(changed=name):
                self._fresh_client()
                daemon = LeaseDaemon(4, _address4())
                form = self._shown(daemon, 4, "192.0.2.40")
                daemon.put(_address4(**{key.replace("-", "_"): value for key, value in change.items()}))

                kea, response = self._save(daemon, 4, "192.0.2.40", form, hostname="renamed.example.org")

                self.assertEqual(kea.commands(), ["lease4-get"])
                (message,) = _messages(response)
                self.assertIn("Lease 192.0.2.40 was not changed", message)
                self.assertIn("Reload", message)

    def test_a_changed_dhcpv6_iaid_sends_no_update(self):
        daemon = LeaseDaemon(6, _prefix())
        form = self._shown(daemon, 6, f"{_PREFIX}/56")
        daemon.put(_prefix(iaid=31))

        kea, response = self._save(daemon, 6, f"{_PREFIX}/56", form, hostname="renamed.example.org")

        self.assertEqual(kea.commands(), ["lease6-get"])
        self.assertIn("binding", _messages(response)[0])

    def test_a_changed_written_field_sends_no_update(self):
        daemon = LeaseDaemon(4, _address4())
        form = self._shown(daemon, 4, "192.0.2.40")
        daemon.put(_address4(hostname="changed.example.org"))

        kea, response = self._save(daemon, 4, "192.0.2.40", form, hostname="renamed.example.org")

        self.assertEqual(kea.commands(), ["lease4-get"])
        self.assertIn("hostname", _messages(response)[0])
        self.assertEqual(daemon.lease("192.0.2.40")["hostname"], "changed.example.org")

    def test_the_conflict_message_names_two_changed_fields_as_a_list(self):
        daemon = LeaseDaemon(4, _address4())
        form = self._shown(daemon, 4, "192.0.2.40")
        daemon.put(_address4(hostname="changed.example.org", valid_lft=7200))

        _kea, response = self._save(daemon, 4, "192.0.2.40", form, hostname="renamed.example.org", valid_lft="1800")

        (message,) = _messages(response)
        self.assertEqual(
            message,
            "Lease 192.0.2.40 was not changed: its hostname and valid lifetime changed in Kea"
            " after the form was shown. Reload the form and try again.",
        )

    def test_a_renewal_alone_permits_the_edit_and_keeps_the_fresh_renewal_and_extensions(self):
        daemon = LeaseDaemon(4, _address4(user_context=_CONTEXT))
        form = self._shown(daemon, 4, "192.0.2.40")
        renewed_at = int(time.time()) - 100
        changed_context = {"site": {"rack": "r2", "slots": [3]}, "owner": {"team": "net"}}
        daemon.put(_address4(cltt=renewed_at, user_context=changed_context))

        kea, response = self._save(daemon, 4, "192.0.2.40", form, hostname="renamed.example.org")

        self.assertEqual(kea.commands(), ["lease4-get", "lease4-update"])
        sent = kea.bodies("lease4-update")[0]["arguments"]
        self.assertEqual(sent["user-context"], changed_context)
        self.assertEqual((sent["cltt"], sent["expire"]), (renewed_at, renewed_at + 3600))
        held = daemon.lease("192.0.2.40")
        self.assertEqual((held["hostname"], held["cltt"], held["valid-lft"]), ("renamed.example.org", renewed_at, 3600))
        self.assertEqual(_messages(response), ["Lease 192.0.2.40 updated."])

    def test_a_blank_hostname_clears_it_and_the_help_text_says_so(self):
        daemon = LeaseDaemon(4, _address4(fqdn_fwd=True, fqdn_rev=True))
        with stub_kea(daemon.responses()):
            page = self.client.get(self._url(4, "192.0.2.40"))
        self.assertContains(page, "Leave blank to clear the hostname.")
        self.assertNotContains(page, "keep current")
        form = _inputs(page)

        kea, _response = self._save(daemon, 4, "192.0.2.40", form, hostname="", hw_address="", valid_lft="")

        sent = kea.bodies("lease4-update")[0]["arguments"]
        self.assertEqual((sent["hostname"], sent["fqdn-fwd"], sent["fqdn-rev"]), ("", False, False))
        # A blank client identifier and lifetime keep the current values.
        self.assertEqual((sent["hw-address"], sent["valid-lft"]), ("aa:bb:cc:00:00:40", 3600))

    def test_a_target_that_is_gone_is_not_recreated(self):
        daemon = LeaseDaemon(4, _address4())
        form = self._shown(daemon, 4, "192.0.2.40")
        daemon.leases.clear()

        kea, response = self._save(daemon, 4, "192.0.2.40", form, hostname="renamed.example.org")

        self.assertEqual(kea.commands(), ["lease4-get"])
        self.assertEqual(daemon.leases, {})
        self.assertEqual(
            _messages(response),
            ["Lease 192.0.2.40 was not found in Kea; nothing was changed. The edit did not recreate it."],
        )

    def test_a_target_deleted_after_the_check_is_reported_and_not_recreated(self):
        daemon = LeaseDaemon(4, _address4())
        form = self._shown(daemon, 4, "192.0.2.40")
        daemon.before("lease4-update", lambda held: held.leases.clear())

        kea, response = self._save(daemon, 4, "192.0.2.40", form, hostname="renamed.example.org")

        self.assertEqual(kea.commands(), ["lease4-get", "lease4-update"])
        self.assertNotIn("force-create", kea.bodies("lease4-update")[0]["arguments"])
        self.assertEqual(daemon.leases, {})
        (message,) = _messages(response)
        self.assertIn("Kea did not change lease 192.0.2.40", message)

    def test_a_delegated_prefix_is_edited_as_a_prefix(self):
        daemon = LeaseDaemon(6, _prefix(user_context=_CONTEXT))
        with stub_kea(daemon.responses()) as kea:
            page = self.client.get(self._url(6, f"{_PREFIX}/56"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(kea.bodies("lease6-get")[0]["arguments"], {"ip-address": _PREFIX, "type": "IA_PD"})
        self.assertContains(page, f"{_PREFIX}/56")
        self.assertContains(page, "Delegated prefix")

        kea, response = self._save(daemon, 6, f"{_PREFIX}/56", _inputs(page), hostname="renamed.example.org")

        self.assertEqual(kea.commands(), ["lease6-get", "lease6-update"])
        sent = kea.bodies("lease6-update")[0]["arguments"]
        self.assertEqual((sent["type"], sent["prefix-len"], sent["user-context"]), ("IA_PD", 56, _CONTEXT))
        self.assertEqual(daemon.lease(_PREFIX, "IA_PD")["hostname"], "renamed.example.org")
        self.assertEqual(_messages(response), [f"Lease {_PREFIX}/56 updated."])

    def test_a_fresh_prefix_of_another_length_or_kind_sends_no_update(self):
        cases = {
            "prefix length": lambda held: held.put(_prefix(prefix_len=60)),
            "kind": lambda held: (held.leases.clear(), held.put(lease_record(_PREFIX, subnet_id=10, iaid=30))),
        }
        for name, change in cases.items():
            with self.subTest(changed=name):
                self._fresh_client()
                daemon = LeaseDaemon(6, _prefix())
                form = self._shown(daemon, 6, f"{_PREFIX}/56")
                change(daemon)

                kea, response = self._save(daemon, 6, f"{_PREFIX}/56", form, hostname="renamed.example.org")

                self.assertNotIn("lease6-update", kea.commands())
                (message,) = _messages(response)
                self.assertIn(f"{_PREFIX}/56", message)

    def test_an_address_route_does_not_edit_a_delegated_prefix_at_that_address(self):
        daemon = LeaseDaemon(6, _prefix())
        with stub_kea(daemon.responses()) as kea:
            response = self.client.get(self._url(6, _PREFIX))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.bodies("lease6-get")[0]["arguments"], {"ip-address": _PREFIX})
        self.assertEqual(_messages(response), [f"Lease {_PREFIX} not found."])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class LeaseDeleteTest(_ViewTestBase):
    """Delete carries the facts of the list row and checks a fresh read before it deletes."""

    def _list(self, daemon: LeaseDaemon, address: str) -> list[str]:
        family = daemon.family
        responses = {
            **_catalogue_responses_for_subnets(family, _SUBNETS[family]),
            **daemon.responses(),
            "reservation-get": {"result": 3},
        }
        with stub_kea(responses):
            page = self.client.get(
                reverse(f"plugins:netbox_kea:server_leases{family}", args=[self.server.pk]),
                {"by": "ip", "q": address},
                HTTP_HX_REQUEST="true",
            )
        return _inputs(page, "pk")["pk"]

    def _delete(self, daemon: LeaseDaemon, selected: list[str], *, confirm: bool = True):
        data = {"pk": selected, **({"_confirm": "1"} if confirm else {})}
        with stub_kea(daemon.responses()) as kea:
            response = self.client.post(
                reverse(f"plugins:netbox_kea:server_leases{daemon.family}_delete", args=[self.server.pk]), data
            )
        return kea, response

    def test_the_confirmation_shows_the_facts_of_the_selected_rows(self):
        daemon = LeaseDaemon(6, _prefix())
        selected = self._list(daemon, _PREFIX)

        _kea, response = self._delete(daemon, selected, confirm=False)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        for fact in (f"{_PREFIX}/56", "Delegated prefix", "IAID 30", "10"):
            self.assertIn(fact, content)
        self.assertEqual(_inputs(response, "pk")["pk"], selected)

    def test_a_delegated_prefix_is_deleted_as_a_prefix_and_reported_with_its_facts(self):
        received = _Received(self, signals.leases_deleted)
        daemon = LeaseDaemon(6, _prefix())
        selected = self._list(daemon, _PREFIX)

        kea, response = self._delete(daemon, selected)

        self.assertEqual(kea.commands(), ["lease6-get", "lease6-del"])
        self.assertEqual(kea.bodies("lease6-del")[0]["arguments"], {"ip-address": _PREFIX, "type": "IA_PD"})
        self.assertEqual(daemon.leases, {})
        self.assertEqual(_messages(response), ["Deleted 1 DHCPv6 lease(s)."])
        (call,) = received.calls
        self.assertEqual(set(call), {"signal", "server", "leases", "dhcp_version", "request"})
        (deleted,) = call["leases"]
        self.assertIsInstance(deleted, DHCPv6PrefixLease)
        self.assertEqual((str(deleted.prefix), deleted.iaid), (f"{_PREFIX}/56", 30))

    def test_a_changed_binding_or_subnet_is_not_deleted_and_emits_nothing(self):
        received = _Received(self, signals.leases_deleted)
        for name, change in {"binding": {"iaid": 31}, "Subnet": {"subnet_id": 11}}.items():
            with self.subTest(changed=name):
                self._fresh_client()
                daemon = LeaseDaemon(6, _prefix())
                selected = self._list(daemon, _PREFIX)
                daemon.put(_prefix(**change))

                kea, response = self._delete(daemon, selected)

                self.assertEqual(kea.commands(), ["lease6-get"])
                self.assertEqual(len(daemon.leases), 1)
                messages = _messages(response)
                self.assertIn(f"Lease {_PREFIX}/56 was not deleted", messages[0])
        self.assertEqual(received.calls, [])

    def test_one_refused_lease_shows_one_message(self):
        for name, change in {
            "conflict": lambda held: held.put(_prefix(iaid=31)),
            "absent": lambda held: held.leases.clear(),
        }.items():
            with self.subTest(refused=name):
                self._fresh_client()
                daemon = LeaseDaemon(6, _prefix())
                selected = self._list(daemon, _PREFIX)
                change(daemon)

                _kea, response = self._delete(daemon, selected)

                self.assertEqual(len(_messages(response)), 1, _messages(response))

    def test_a_renewal_alone_still_deletes(self):
        daemon = LeaseDaemon(4, _address4())
        selected = self._list(daemon, "192.0.2.40")
        daemon.put(_address4(cltt=int(time.time()) - 100, hostname="renamed-by-client"))

        kea, response = self._delete(daemon, selected)

        self.assertEqual(kea.commands(), ["lease4-get", "lease4-del"])
        self.assertEqual(daemon.leases, {})
        self.assertEqual(_messages(response), ["Deleted 1 DHCPv4 lease(s)."])

    def test_an_address_lease_of_another_kind_is_not_deleted(self):
        received = _Received(self, signals.leases_deleted)
        daemon = LeaseDaemon(6, _prefix())
        selected = self._list(daemon, _PREFIX)
        daemon.leases.clear()
        daemon.put(lease_record(_PREFIX, subnet_id=10, iaid=30))

        kea, response = self._delete(daemon, selected)

        self.assertNotIn("lease6-del", kea.commands())
        self.assertEqual(list(daemon.leases), [("IA_NA", _PREFIX)])
        self.assertEqual(_messages(response), [f"Lease {_PREFIX}/56 was not found in Kea; nothing was deleted."])
        self.assertEqual(received.calls, [])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class LeaseAddTest(_ViewTestBase):
    """Creation sends a typed request and reports what Kea confirmed and what a fresh read observed."""

    def _url(self, family: int = 4) -> str:
        return reverse(f"plugins:netbox_kea:server_lease{family}_add", args=[self.server.pk])

    def test_a_creation_signal_carries_the_request_and_the_observed_lease(self):
        received = _Received(self, signals.lease_added)
        daemon = LeaseDaemon(4)
        data = {"ip_address": "192.0.2.50", "hw_address": "AA:BB:CC:00:00:50", "hostname": "new.example.org"}
        with stub_kea(daemon.responses()) as kea:
            response = self.client.post(self._url(), data)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands(), ["lease4-add", "lease4-get"])
        (call,) = received.calls
        self.assertEqual(set(call), {"signal", "server", "creation", "lease", "dhcp_version", "request"})
        self.assertIsInstance(call["creation"], DHCPv4LeaseRequest)
        self.assertEqual(
            (str(call["creation"].address), call["creation"].hw_address), ("192.0.2.50", "aa:bb:cc:00:00:50")
        )
        self.assertEqual((str(call["lease"].address), call["lease"].subnet_id), ("192.0.2.50", 10))

    def test_a_mixed_case_hostname_that_kea_stores_in_lowercase_is_an_observation(self):
        received = _Received(self, signals.lease_added)
        daemon = LeaseDaemon(4)
        data = {"ip_address": "192.0.2.50", "hw_address": "aa:bb:cc:00:00:50", "hostname": "New.Example.ORG"}
        with stub_kea(daemon.responses()):
            response = self.client.post(self._url(), data)

        (call,) = received.calls
        self.assertEqual(call["lease"].hostname, "new.example.org")
        self.assertFalse(any("does not match" in message for message in _messages(response)))

    def test_a_failed_readback_reports_the_creation_without_an_observed_lease(self):
        received = _Received(self, signals.lease_added)
        daemon = LeaseDaemon(4)
        daemon.before("lease4-get", lambda held: held.leases.clear())
        with stub_kea(daemon.responses()):
            self.client.post(self._url(), {"ip_address": "192.0.2.50", "hw_address": "aa:bb:cc:00:00:50"})

        (call,) = received.calls
        self.assertEqual(str(call["creation"].address), "192.0.2.50")
        self.assertIsNone(call["lease"])

    def test_a_readback_that_contradicts_the_request_is_not_an_observation(self):
        from ipam.models import IPAddress

        received = _Received(self, signals.lease_added)
        changes = {
            "hardware address": {"hw-address": "aa:bb:cc:00:00:99"},
            "Subnet": {"subnet-id": 11},
            "hostname": {"hostname": "other.example.org"},
        }
        for name, change in changes.items():
            with self.subTest(changed=name):
                self._fresh_client()
                received.calls.clear()
                daemon = LeaseDaemon(4)
                daemon.before(
                    "lease4-get", lambda held, change=change: held.leases[("V4", "192.0.2.50")].update(change)
                )
                data = {
                    "ip_address": "192.0.2.50",
                    "hw_address": "aa:bb:cc:00:00:50",
                    "subnet_id": "10",
                    "hostname": "new.example.org",
                    "sync_to_netbox": "on",
                }
                responses = {**_catalogue_responses_for_subnets(4, _SUBNETS[4]), **daemon.responses()}
                with stub_kea(responses):
                    response = self.client.post(self._url(), data)

                (call,) = received.calls
                self.assertIsNone(call["lease"])
                self.assertFalse(IPAddress.objects.filter(address__net_host="192.0.2.50").exists())
                self.assertTrue(
                    any("does not match" in message for message in _messages(response)), _messages(response)
                )

    def test_a_dhcpv6_readback_with_another_iaid_is_not_an_observation(self):
        received = _Received(self, signals.lease_added)
        daemon = LeaseDaemon(6)
        daemon.before("lease6-get", lambda held: held.leases[("IA_NA", "2001:db8:1::50")].update({"iaid": 6}))
        data = {"ip_address": "2001:db8:1::50", "duid": "00:01:02:03", "iaid": "5"}
        with stub_kea(daemon.responses()):
            self.client.post(self._url(6), data)

        (call,) = received.calls
        self.assertIsNone(call["lease"])

    def test_a_refused_creation_emits_nothing(self):
        received = _Received(self, signals.lease_added)
        daemon = LeaseDaemon(4, lease_record("192.0.2.50", subnet_id=10))
        with stub_kea(daemon.responses()):
            response = self.client.post(self._url(), {"ip_address": "192.0.2.50", "hw_address": "aa:bb:cc:00:00:50"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(received.calls, [])

    def test_a_dhcpv4_creation_needs_a_hardware_address(self):
        with stub_kea(LeaseDaemon(4).responses()) as kea:
            response = self.client.post(self._url(), {"ip_address": "192.0.2.50"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])
        self.assertIn("hw_address", response.context["form"].errors)

    def test_sync_claims_only_a_current_readback_even_with_a_subnet(self):
        from ipam.models import IPAddress

        daemon = LeaseDaemon(4)
        expired = {"cltt": int(time.time()) - 7200, "valid-lft": 3600}
        daemon.before("lease4-get", lambda held: held.leases[("V4", "192.0.2.50")].update(expired))
        data = {
            "ip_address": "192.0.2.50",
            "hw_address": "aa:bb:cc:00:00:50",
            "subnet_id": "10",
            "sync_to_netbox": "on",
        }
        responses = {**_catalogue_responses_for_subnets(4, _SUBNETS[4]), **daemon.responses()}
        with stub_kea(responses) as kea:
            response = self.client.post(self._url(), data)

        self.assertEqual(kea.commands().count("lease4-get"), 1)
        self.assertFalse(IPAddress.objects.filter(address__net_host="192.0.2.50").exists())
        self.assertIn(
            "Lease created, but it was not synced to NetBox: Kea did not report it as a current lease.",
            _messages(response),
        )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class LeaseReserveTest(_ViewTestBase):
    """Reserve and the reservation badges compare a delegated prefix with the Reservation prefixes."""

    def _rows(self, records: list[dict], reservation_get) -> dict[str, dict]:
        responses = {
            **_catalogue_responses_for_subnets(6, _SUBNETS[6]),
            "lease6-get-all": {"result": 0, "arguments": {"leases": records}},
            "reservation-get": reservation_get,
        }
        with stub_kea(responses):
            response = self.client.get(
                reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk]),
                {"by": "subnet_id", "q": "10"},
                HTTP_HX_REQUEST="true",
            )
        self.last_response = response
        return {row.record["kind"]: row.record for row in response.context["table"].rows}

    def test_a_delegated_prefix_offers_a_prefix_reservation_an_edit_and_a_sync(self):
        rows = self._rows([lease_record("2001:db8:1::10", subnet_id=10), _prefix()], {"result": 3})

        query = parse_qs(urlsplit(rows["delegated-prefix"]["create_reservation_url"]).query)
        self.assertEqual(query["prefixes"], [f"{_PREFIX}/56"])
        self.assertNotIn("ip_addresses", query)
        self.assertEqual(
            rows["delegated-prefix"]["edit_url"],
            reverse("plugins:netbox_kea:server_lease6_edit", args=[self.server.pk, f"{_PREFIX}/56"])
            + "?"
            + urlencode({"return_url": self.last_response.wsgi_request.get_full_path()}),
        )
        self.assertEqual(
            rows["delegated-prefix"].get("sync_url"),
            reverse("plugins:netbox_kea:server_lease6_sync", args=[self.server.pk]),
        )
        self.assertEqual(
            parse_qs(urlsplit(rows["address"]["create_reservation_url"]).query)["ip_addresses"], ["2001:db8:1::10"]
        )

    def test_a_reservation_of_the_prefix_shows_reserved(self):
        host = {
            "subnet-id": 10,
            "duid": "00:01:00:01:2c:4f:00:01:aa:bb:cc:00:00:09",
            "hostname": "pdres",
            "ip-addresses": [],
            "prefixes": [f"{_PREFIX}/56"],
        }

        def reservation_get(body):
            # Kea 3.2.0 finds a host by the base address of its reserved prefix.
            if body["arguments"].get("ip-address") == _PREFIX:
                return {"result": 0, "text": "Host found.", "arguments": host}
            return {"result": 3, "text": "Host not found."}

        rows = self._rows([_prefix()], reservation_get)

        self.assertTrue(rows["delegated-prefix"]["is_reserved"])
        self.assertFalse(rows["delegated-prefix"]["pending_ip_change"])

    def test_a_reservation_of_another_prefix_for_the_client_is_pending(self):
        lease = _prefix()
        host = {
            "subnet-id": 10,
            "duid": lease["duid"],
            "hostname": "",
            "ip-addresses": [],
            "prefixes": ["2001:db8:100:700::/56"],
        }

        def reservation_get(body):
            if body["arguments"].get("identifier") == lease["duid"]:
                return {"result": 0, "text": "Host found.", "arguments": host}
            return {"result": 3, "text": "Host not found."}

        rows = self._rows([lease], reservation_get)

        row = rows["delegated-prefix"]
        self.assertFalse(row["is_reserved"])
        self.assertTrue(row["pending_ip_change"])
        self.assertEqual(row["pending_reservation_ip"], "2001:db8:100:700::/56")

    def _identity_host(self, lease: dict, *, addresses: list[str], prefixes: list[str]):
        host = {"subnet-id": 10, "duid": lease["duid"], "hostname": "", "ip-addresses": addresses, "prefixes": prefixes}

        def reservation_get(body):
            if body["arguments"].get("identifier") == lease["duid"]:
                return {"result": 0, "text": "Host found.", "arguments": host}
            return {"result": 3, "text": "Host not found."}

        return reservation_get

    def test_a_reservation_that_holds_only_the_other_kind_is_shown_but_not_reserved(self):
        cases = {
            "delegated-prefix": (_prefix(), {"addresses": ["2001:db8:1::99"], "prefixes": []}),
            "address": (
                lease_record("2001:db8:1::10", subnet_id=10),
                {"addresses": [], "prefixes": ["2001:db8:100:700::/56"]},
            ),
        }
        for kind, (lease, held) in cases.items():
            with self.subTest(kind=kind):
                rows = self._rows([lease], self._identity_host(lease, **held))

                row = rows[kind]
                self.assertFalse(row["is_reserved"])
                self.assertFalse(row["pending_ip_change"])
                self.assertTrue(row["reservation_url"])
                self.assertIsNone(row["create_reservation_url"])
                cell = self.last_response.content.decode()
                self.assertIn(f'href="{escape(row["reservation_url"])}"', cell)
                self.assertNotIn("Reserved</a>", cell)
                self.assertNotIn('text-bg-success">Reserved</span>', cell)
                self.assertNotIn("+ Reserve", cell)

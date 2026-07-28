# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for netbox_kea plugin.

Also contains pure-Python unit tests for helper functions defined in views.py
(e.g. ``_extract_identifier``), which do not require a database but live here
because they are tightly coupled to view logic.

These tests verify correct HTTP responses and redirect behaviour for every view.
They drive a **real** ``KeaClient``; only the HTTP boundary is stubbed via
``kea_stub.stub_kea``, so the request payloads the views actually send to Kea are
exercised (and can be asserted on) instead of asserting against a ``MagicMock``.

Test organisation strategy
--------------------------
Each view class gets its own ``TestCase`` subclass so failures are isolated and
clearly named.  Every test that triggers a redirect asserts that the redirect URL
contains an *integer* pk (never the string "None"), which is the pattern that
revealed the original ``POST /plugins/kea/servers/None`` 404 bug.

View tests use ``django.test.TestCase`` because they write to the test database
(user + server fixtures).  Server objects are created via ``Server.objects.create()``
which does **not** call ``Model.clean()`` and therefore does not trigger live Kea
connectivity checks.

NOTE — reservation writes never persist
---------------------------------------
``KeaClient.reservation_add`` / ``reservation_update`` / ``reservation_del`` each
issue a *single* Kea command and never ``config-write`` (host_cmds writes to a host
backend, not the config file).  The ``except PartialPersistError`` branch that
follows each of those calls in the view is therefore unreachable through the real
client — it only ever fired when a ``MagicMock`` injected the exception into a
method that cannot raise it.  Those mock-only tests are omitted here; the
persisting-write guarantee is covered by the pool/subnet PartialPersist cases in
``test_reservation_views.py`` (real ``config-write`` result 1).
"""

import unittest as _unittest  # alias to avoid pytest collection confusion
from unittest.mock import patch
from urllib.parse import quote_plus, urlencode

import requests as req
from django.contrib import messages as django_messages
from django.test import override_settings
from django.urls import reverse
from ipam.models import IPAddress as NbIP

from netbox_kea import constants
from netbox_kea.kea import KeaClient
from netbox_kea.signals import reservation_deleted
from netbox_kea.views import _get_reservation_identifier as _extract_identifier

from .kea_stub import _leases_per_subnet, _res_get, _res_page, _subnet_get, stub_kea
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase

# ─────────────────────────────────────────────────────────────────────────────
# Shared stub responses (real KeaClient + HTTP-boundary stub)
# ─────────────────────────────────────────────────────────────────────────────
#
# Command chains issued by the reservation views:
#   list GET:  ``reservation-get-page`` (drained via ``iter_reservations``) then, if
#              any reservations are found, ``lease{v}-get-all`` per unique subnet
#              (lease-status enrichment; NetBox IPAM badges hit the DB, not Kea).
#   add POST:  ``subnet{v}-get`` (pool-overlap probe, non-fatal) + ``reservation-add``.
#   edit GET:  ``reservation-get`` (prefill) + ``lease{v}-get`` (hostname diff).
#   edit POST: ``reservation-get`` (reload existing) + ``reservation-update``.
#   delete POST: ``reservation-del``.
#
# A POST followed with ``follow=True`` lands on the reservations list, which then
# issues ``reservation-get-page`` again — so those stubs also register it.


#: ``reservation-get-page`` with no hosts (source exhausted → empty reservation list).
_RES_EMPTY_PAGE = {"result": 3}
#: ``reservation-get`` / ``lease{v}-get`` with result 3 = no such record.
_RES_NOT_FOUND = {"result": 3}
_LEASE_NOT_FOUND = {"result": 3}

#: Commands the reservation list issues, for POSTs that redirect back to it.
_RES_LIST_STUB = {"reservation-get-page": _RES_EMPTY_PAGE, "lease4-get-all": _LEASE_NOT_FOUND}

#: Existing-reservation payloads used to seed the edit POST reload.
_EDIT4_EXISTING = {"hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "10.0.0.55", "subnet-id": 1}
_EDIT6_EXISTING = {
    "ip-addresses": ["2001:db8::1"],
    "duid": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
    "subnet-id": 1,
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationListWithoutAddress(_ViewTestBase):
    """Reservations that reserve no address must not break the list (issue #110).

    Kea omits ``ip-address`` for identifier-only DHCPv4 hosts (hostname / option-data /
    client-classes only) and omits ``ip-addresses`` for prefix-delegation-only DHCPv6
    hosts.  The actions column used to reverse an address-keyed route with an empty
    string, which ``<str:ip_address>`` (``[^/]+``) cannot match — a single such row
    raised ``NoReverseMatch`` and 500'd the whole tab.

    The test user is a superuser, so ``can_change`` is True: the reversal lived inside
    ``{% if record.can_change %}`` and a read-only user never reached it.
    """

    #: Identifier-only DHCPv4 host: legal in Kea, reserves a hostname but no address.
    ADDRESSLESS_V4 = {"subnet-id": 3742, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "printer-1"}
    #: Prefix-delegation-only DHCPv6 host: reserves a prefix, no addresses.
    PD_ONLY_V6 = {"subnet-id": 12, "duid": "00:01:00:01:12:34:56:78:aa:bb", "prefixes": ["2001:db8:1::/64"]}

    def _url4(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    def _url6(self):
        return reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])

    def test_v4_reservation_without_ip_address_renders(self):
        with stub_kea({"reservation-get-page": _res_page([self.ADDRESSLESS_V4]), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url4())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "printer-1")

    def test_v6_reservation_with_only_prefixes_renders(self):
        with stub_kea({"reservation-get-page": _res_page([self.PD_ONLY_V6]), "lease6-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url6())
        self.assertEqual(response.status_code, 200)

    def test_address_less_row_is_actionable_by_identifier(self):
        """No address to key on, so the actions address the row by its identifier."""
        with stub_kea({"reservation-get-page": _res_page([self.ADDRESSLESS_V4]), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url4())
        self.assertContains(response, "/reservations4/3742/edit-by-identifier/")
        self.assertContains(response, "identifier_type=hw-address")
        # ...and never through the address-keyed route, which cannot express this row.
        self.assertNotContains(response, "/reservations4/3742/edit/")

    def test_addressed_row_keeps_its_edit_and_delete_links(self):
        """Regression guard: moving URL construction into the view must not drop the links."""
        host = {"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}
        with stub_kea({"reservation-get-page": _res_page([host]), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url4())
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 1, "10.0.0.5"]),
        )
        self.assertContains(
            response,
            reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, 1, "10.0.0.5"]),
        )

    def test_mixed_rows_sort_by_ip_column(self):
        """django-tables2 orders on ``_ip_sort_key``, which address-less rows do not have.

        Driven through the real view + real table so ordering and row rendering actually
        run; sorting the dicts by hand would not exercise the missing accessor.
        """
        addressed = {"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:01"}
        hosts = [self.ADDRESSLESS_V4, addressed]
        with stub_kea({"reservation-get-page": _res_page(hosts), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url4(), {"sort": "ip_address"})
        self.assertEqual(response.status_code, 200)
        with stub_kea({"reservation-get-page": _res_page(hosts), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url4(), {"sort": "-ip_address"})
        self.assertEqual(response.status_code, 200)

    def test_row_with_neither_address_nor_identifier_renders(self):
        """A host with no address and no identifier still must not invent a URL."""
        host = {"subnet-id": 7, "hostname": "ghost"}
        with stub_kea({"reservation-get-page": _res_page([host]), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url4())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ghost")

    def test_string_subnet_id_still_gets_action_links(self):
        """``<int:subnet_id>`` reverses a decimal *string* fine — don't reject one.

        Pre-validating the value type in Python instead of asking the route would
        silently drop working buttons for a payload the old template handled.
        """
        host = {"subnet-id": "1", "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}
        with stub_kea({"reservation-get-page": _res_page([host]), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url4())
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 1, "10.0.0.5"]),
        )

    def test_values_the_route_cannot_reverse_do_not_break_the_page(self):
        """Anything the converters reject costs that row its buttons, not the tab.

        A negative subnet id fails ``<int:subnet_id>`` (``[0-9]+``) and an address
        containing a slash fails ``<str:ip_address>`` (``[^/]+``) — neither may raise
        NoReverseMatch out of the table.
        """
        hosts = [
            {"subnet-id": -5, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:01"},
            {"subnet-id": 1, "ip-address": "2001:db8::/64", "hw-address": "aa:bb:cc:dd:ee:02"},
        ]
        with stub_kea({"reservation-get-page": _res_page(hosts), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url4())
        self.assertEqual(response.status_code, 200)
        # No per-row reservation action URL for either host (the page's own
        # object-edit links are unrelated, hence matching on the row route prefix).
        self.assertNotContains(response, f"/servers/{self.server.pk}/reservations4/1/")
        self.assertNotContains(response, f"/servers/{self.server.pk}/reservations4/-5/")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationByIdentifierRoutes(_ViewTestBase):
    """Edit and delete a reservation that has no address to key on.

    The identifier travels in the query string, so these routes reverse from integers
    alone and no reservation value can make them fail to reverse.
    """

    V4_HOST = {"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "printer-1"}
    V6_HOST = {"subnet-id": 12, "duid": "00:01:00:01:12:34", "prefixes": ["2001:db8:1::/64"]}
    QUERY_V4 = {"identifier_type": "hw-address", "identifier": "aa:bb:cc:dd:ee:ff"}
    QUERY_V6 = {"identifier_type": "duid", "identifier": "00:01:00:01:12:34"}

    def _edit_url(self, version=4, subnet_id=1):
        return reverse(
            f"plugins:netbox_kea:server_reservation{version}_edit_by_identifier", args=[self.server.pk, subnet_id]
        )

    def _delete_url(self, version=4, subnet_id=1):
        return reverse(
            f"plugins:netbox_kea:server_reservation{version}_delete_by_identifier", args=[self.server.pk, subnet_id]
        )

    # ── lookup ────────────────────────────────────────────────────────────

    def test_edit_get_looks_the_reservation_up_by_identifier(self):
        with stub_kea({"reservation-get": _res_get(self.V4_HOST), "lease4-get": _LEASE_NOT_FOUND}) as kea:
            response = self.client.get(self._edit_url(), self.QUERY_V4)
        self.assertEqual(response.status_code, 200)
        body = kea.bodies("reservation-get")[0]
        self.assertEqual(body["arguments"]["identifier-type"], "hw-address")
        self.assertEqual(body["arguments"]["identifier"], "aa:bb:cc:dd:ee:ff")
        self.assertNotIn("ip-address", body["arguments"])

    def test_edit_get_leaves_the_address_editable(self):
        """The IP is not part of the key here, so it can be added or removed."""
        with stub_kea({"reservation-get": _res_get(self.V4_HOST), "lease4-get": _LEASE_NOT_FOUND}):
            response = self.client.get(self._edit_url(), self.QUERY_V4)
        form = response.context["form"]
        self.assertFalse(form.fields["ip_address"].disabled)
        self.assertTrue(form.fields["identifier"].disabled)
        self.assertTrue(form.fields["identifier_type"].disabled)
        self.assertTrue(form.fields["subnet_id"].disabled)

    def test_missing_reservation_is_404(self):
        with stub_kea({"reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._edit_url(), self.QUERY_V4)
        self.assertEqual(response.status_code, 404)

    # ── update ────────────────────────────────────────────────────────────

    def test_edit_post_without_an_address_sends_no_ip_address_key(self):
        post = {"hostname": "printer-2", "ip_address": "", **_FORMSET_MGMT}
        with stub_kea(
            {"reservation-get": _res_get(self.V4_HOST), "reservation-update": {"result": 0}, **_RES_LIST_STUB}
        ) as kea:
            response = self.client.post(self._edit_url(), {**post}, QUERY_STRING=urlencode(self.QUERY_V4))
        self.assertIn(response.status_code, (200, 302))
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertNotIn("ip-address", sent)
        self.assertEqual(sent["hostname"], "printer-2")
        self.assertEqual(sent["hw-address"], "aa:bb:cc:dd:ee:ff")

    def test_edit_post_can_give_the_reservation_an_address(self):
        """Kea's reservation-update replaces the host keyed by identifier, so this works."""
        post = {"hostname": "printer-1", "ip_address": "10.0.0.55", **_FORMSET_MGMT}
        with stub_kea(
            {"reservation-get": _res_get(self.V4_HOST), "reservation-update": {"result": 0}, **_RES_LIST_STUB}
        ) as kea:
            self.client.post(self._edit_url(), {**post}, QUERY_STRING=urlencode(self.QUERY_V4))
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(sent["ip-address"], "10.0.0.55")

    def test_edit_post_keeps_option_data_it_did_not_touch(self):
        existing = {**self.V4_HOST, "option-data": [{"name": "domain-name-servers", "data": "10.0.0.1"}]}
        post = {"hostname": "printer-1", "ip_address": "", **_FORMSET_MGMT}
        with stub_kea(
            {"reservation-get": _res_get(existing), "reservation-update": {"result": 0}, **_RES_LIST_STUB}
        ) as kea:
            self.client.post(self._edit_url(), {**post}, QUERY_STRING=urlencode(self.QUERY_V4))
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(sent["option-data"], [{"name": "domain-name-servers", "data": "10.0.0.1"}])

    def test_edit_post_still_clears_option_data_the_user_deleted(self):
        """A submitted formset stays authoritative — "untouched" must not mean "unclearable"."""
        existing = {**self.V4_HOST, "option-data": [{"name": "domain-name-servers", "data": "10.0.0.1"}]}
        post = {
            "hostname": "printer-1",
            "ip_address": "",
            "options-TOTAL_FORMS": "1",
            "options-INITIAL_FORMS": "1",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            "options-0-name": "domain-name-servers",
            "options-0-data": "10.0.0.1",
            "options-0-DELETE": "on",
        }
        with stub_kea(
            {"reservation-get": _res_get(existing), "reservation-update": {"result": 0}, **_RES_LIST_STUB}
        ) as kea:
            self.client.post(self._edit_url(), post, QUERY_STRING=urlencode(self.QUERY_V4))
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertNotIn("option-data", sent)

    def test_v6_edit_post_replaces_prefixes(self):
        post = {"hostname": "", "ip_addresses": "", "prefixes": "2001:db8:9::/64", **_FORMSET_MGMT}
        with stub_kea(
            {
                "reservation-get": _res_get(self.V6_HOST),
                "reservation-update": {"result": 0},
                "reservation-get-page": _RES_EMPTY_PAGE,
            }
        ) as kea:
            self.client.post(self._edit_url(6, 12), {**post}, QUERY_STRING=urlencode(self.QUERY_V6))
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(sent["prefixes"], ["2001:db8:9::/64"])
        self.assertNotIn("ip-addresses", sent)

    def test_v6_edit_post_with_blank_prefixes_removes_them(self):
        post = {"hostname": "", "ip_addresses": "2001:db8::5", "prefixes": "", **_FORMSET_MGMT}
        with stub_kea(
            {
                "reservation-get": _res_get(self.V6_HOST),
                "reservation-update": {"result": 0},
                "reservation-get-page": _RES_EMPTY_PAGE,
            }
        ) as kea:
            self.client.post(self._edit_url(6, 12), {**post}, QUERY_STRING=urlencode(self.QUERY_V6))
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertNotIn("prefixes", sent)
        self.assertEqual(sent["ip-addresses"], ["2001:db8::5"])

    def test_v6_edit_post_with_an_invalid_prefix_sends_nothing(self):
        post = {"hostname": "", "ip_addresses": "", "prefixes": "2001:db8::1/64", **_FORMSET_MGMT}
        with stub_kea({"reservation-get": _res_get(self.V6_HOST), "reservation-update": {"result": 0}}) as kea:
            response = self.client.post(self._edit_url(6, 12), {**post}, QUERY_STRING=urlencode(self.QUERY_V6))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reservation-update", kea.commands())

    # ── delete ────────────────────────────────────────────────────────────

    def test_delete_post_keys_on_the_identifier(self):
        with stub_kea({"reservation-del": {"result": 0}, **_RES_LIST_STUB}) as kea:
            response = self.client.post(self._delete_url(), {"confirm": "true"}, QUERY_STRING=urlencode(self.QUERY_V4))
        self.assertIn(response.status_code, (200, 302))
        args = kea.bodies("reservation-del")[0]["arguments"]
        self.assertEqual(args["identifier-type"], "hw-address")
        self.assertEqual(args["identifier"], "aa:bb:cc:dd:ee:ff")
        self.assertNotIn("ip-address", args)

    def test_delete_signal_carries_the_identifier_and_a_null_address(self):
        received = []

        def _receiver(sender, **kwargs):
            received.append(kwargs)

        reservation_deleted.connect(_receiver)
        try:
            with stub_kea({"reservation-del": {"result": 0}, **_RES_LIST_STUB}):
                self.client.post(self._delete_url(), {"confirm": "true"}, QUERY_STRING=urlencode(self.QUERY_V4))
        finally:
            reservation_deleted.disconnect(_receiver)
        self.assertEqual(len(received), 1)
        self.assertIsNone(received[0]["ip_address"])
        self.assertEqual(received[0]["identifier_type"], "hw-address")
        self.assertEqual(received[0]["identifier"], "aa:bb:cc:dd:ee:ff")

    def test_address_keyed_delete_signal_keeps_the_same_kwargs(self):
        """Receivers must see one payload shape, whichever route was used."""
        received = []

        def _receiver(sender, **kwargs):
            received.append(kwargs)

        reservation_deleted.connect(_receiver)
        try:
            url = reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, 1, "10.0.0.5"])
            with stub_kea({"reservation-del": {"result": 0}, **_RES_LIST_STUB}):
                self.client.post(url, {"confirm": "true"})
        finally:
            reservation_deleted.disconnect(_receiver)
        self.assertEqual(received[0]["ip_address"], "10.0.0.5")
        self.assertIsNone(received[0]["identifier_type"])
        self.assertIsNone(received[0]["identifier"])

    # ── long identifiers ──────────────────────────────────────────────────

    def test_max_octet_duid_survives_the_whole_by_identifier_path(self):
        """A 128-octet DUID is 383 characters — the routes must carry it, not reject it.

        The list builds the row's action URL, so this walks the same path the operator
        does: render the tab, take the ``edit-by-identifier`` link it produced, and
        follow it.
        """
        duid = ":".join(["ab"] * constants.DUID_MAX_OCTETS)
        host = {"subnet-id": 12, "duid": duid, "prefixes": ["2001:db8:1::/64"]}
        with stub_kea({"reservation-get-page": _res_page([host]), "lease6-get-all": _LEASE_NOT_FOUND}):
            listing = self.client.get(reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk]))
        self.assertEqual(listing.status_code, 200)
        edit_url = listing.context["table"].data.data[0]["edit_url"]
        self.assertIn(quote_plus(duid), edit_url)

        with stub_kea({"reservation-get": _res_get(host), "lease6-get": _LEASE_NOT_FOUND}) as kea:
            response = self.client.get(edit_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.bodies("reservation-get")[0]["arguments"]["identifier"], duid)

    def test_duid_past_its_octet_limit_is_still_rejected_by_the_route(self):
        query = urlencode({"identifier_type": "duid", "identifier": ":".join(["ab"] * 200)})
        response = self.client.get(f"{self._edit_url(6, 12)}?{query}")
        self.assertEqual(response.status_code, 400)

    # ── malformed lookups ─────────────────────────────────────────────────

    def test_malformed_lookups_are_rejected(self):
        cases = {
            "missing both": "",
            "missing value": "identifier_type=hw-address",
            "empty value": "identifier_type=hw-address&identifier=",
            "unknown type": "identifier_type=nonsense&identifier=aa:bb",
            "wrong version type": "identifier_type=duid&identifier=00:01",
            "repeated": "identifier_type=hw-address&identifier=aa:bb&identifier=cc:dd",
            "too long": f"identifier_type=hw-address&identifier={'a' * 300}",
        }
        for label, query in cases.items():
            with self.subTest(case=label):
                response = self.client.get(f"{self._edit_url()}?{query}")
                self.assertEqual(response.status_code, 400)
                response = self.client.post(f"{self._delete_url()}?{query}", {"confirm": "true"})
                self.assertEqual(response.status_code, 400)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationListShowsWhatIsReserved(_ViewTestBase):
    """A reservation row must show what it identifies and what it reserves.

    The v4 table only had a Hardware Address column and the v6 table only a DUID
    column, so a host identified by client-id, circuit-id, flex-id or remote-id
    rendered as a blank cell — and a DHCPv6 host that only delegates prefixes showed
    nothing at all about what it reserves.
    """

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:server_reservations{version}", args=[self.server.pk])

    def test_v4_non_mac_identifier_is_shown_with_its_type(self):
        host = {"subnet-id": 1, "client-id": "01:aa:bb:cc:dd:ee:ff", "hostname": "kiosk"}
        with stub_kea({"reservation-get-page": _res_page([host]), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url(4))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "01:aa:bb:cc:dd:ee:ff")
        self.assertContains(response, "client-id")

    def test_v6_non_duid_identifier_is_shown_with_its_type(self):
        host = {"subnet-id": 1, "flex-id": "hostname-router-1", "ip-addresses": ["2001:db8::5"]}
        with stub_kea({"reservation-get-page": _res_page([host]), "lease6-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url(6))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hostname-router-1")
        self.assertContains(response, "flex-id")

    def test_v6_delegated_prefixes_are_shown(self):
        host = {
            "subnet-id": 12,
            "duid": "00:01:00:01:12:34",
            "prefixes": ["2001:db8:1::/64", "2001:db8:2::/64"],
        }
        with stub_kea({"reservation-get-page": _res_page([host]), "lease6-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url(6))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2001:db8:1::/64")
        self.assertContains(response, "2001:db8:2::/64")

    def test_address_less_row_says_so(self):
        host = {"subnet-id": 1, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "printer-1"}
        with stub_kea({"reservation-get-page": _res_page([host]), "lease4-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url(4))
        self.assertContains(response, "No address")

    def test_v6_prefix_only_row_says_it_has_no_address(self):
        host = {"subnet-id": 12, "duid": "00:01:00:01:12:34", "prefixes": ["2001:db8:1::/64"]}
        with stub_kea({"reservation-get-page": _res_page([host]), "lease6-get-all": _LEASE_NOT_FOUND}):
            response = self.client.get(self._url(6))
        self.assertContains(response, "No address")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservations4V6OnlyRedirect(_ViewTestBase):
    """A v6-only server's /reservations4/ redirects to the merged tab's v6 route."""

    def test_get_reservations4_on_v6_only_server_redirects_to_v6(self):
        v6_only = _make_db_server(name="v6-only-resv", dhcp4=False, dhcp6=True)
        response = self.client.get(reverse("plugins:netbox_kea:server_reservations4", args=[v6_only.pk]))
        self.assertRedirects(
            response,
            reverse("plugins:netbox_kea:server_reservations6", args=[v6_only.pk]),
            fetch_redirect_response=False,
        )


class TestExtractIdentifier(_unittest.TestCase):
    """Unit tests for the ``_extract_identifier()`` helper in ``views.py``.

    The function walks a Kea reservation dict looking for identifier keys in
    priority order (v4: hw-address > client-id > circuit-id > flex-id;
    v6: duid > hw-address > client-id > flex-id).
    """

    def test_v4_prefers_hw_address(self):
        r = {"hw-address": "aa:bb:cc:dd:ee:ff", "client-id": "01:aa:bb", "subnet-id": 1}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "aa:bb:cc:dd:ee:ff")

    def test_v4_client_id_when_no_hw_address(self):
        r = {"client-id": "01:aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "client-id")
        self.assertEqual(ival, "01:aa:bb:cc:dd:ee:ff")

    def test_v4_circuit_id(self):
        r = {"circuit-id": "0a:1b:2c"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "circuit-id")
        self.assertEqual(ival, "0a:1b:2c")

    def test_v4_flex_id_as_last_resort(self):
        r = {"flex-id": "aabbccdd"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "flex-id")
        self.assertEqual(ival, "aabbccdd")

    def test_v4_hw_address_beats_flex_id(self):
        r = {"flex-id": "aabbccdd", "hw-address": "aa:bb:cc"}
        itype, _ = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")

    def test_v6_prefers_duid_over_hw_address(self):
        r = {"duid": "00:01:02:03:04:05", "hw-address": "aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 6)
        self.assertEqual(itype, "duid")
        self.assertEqual(ival, "00:01:02:03:04:05")

    def test_v6_hw_address_fallback_when_no_duid(self):
        r = {"hw-address": "aa:bb:cc:dd:ee:ff"}
        itype, ival = _extract_identifier(r, 6)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "aa:bb:cc:dd:ee:ff")

    def test_fallback_returns_hw_address_empty_string(self):
        """When no known identifier key is present return ``("hw-address", "")``.

        This keeps the form pre-population logic from crashing.
        """
        r = {"subnet-id": 1, "ip-address": "10.0.0.1"}
        itype, ival = _extract_identifier(r, 4)
        self.assertEqual(itype, "hw-address")
        self.assertEqual(ival, "")


# ---------------------------------------------------------------------------
# Reservation list exception paths
# ---------------------------------------------------------------------------

_FORMSET_MGMT = {
    "options-TOTAL_FORMS": "0",
    "options-INITIAL_FORMS": "0",
    "options-MIN_NUM_FORMS": "0",
    "options-MAX_NUM_FORMS": "1000",
}

_VALID_RESERVATION4_POST = {
    "subnet_id": "1",
    "ip_address": "10.0.0.55",
    "identifier_type": "hw-address",
    "identifier": "aa:bb:cc:dd:ee:ff",
    "hostname": "test-host",
    **_FORMSET_MGMT,
}

# Edit-shaped payload — omits disabled fields (subnet_id, ip_address, identifier_type,
# identifier) as a real browser would; the view reads identifiers from reservation_get.
_VALID_RESERVATION4_EDIT_POST = {
    "hostname": "test-host",
    **_FORMSET_MGMT,
}

_VALID_RESERVATION6_POST = {
    "subnet_id": "1",
    "ip_addresses": "2001:db8::1",
    "identifier_type": "duid",
    "identifier": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
    "hostname": "test-host6",
    **_FORMSET_MGMT,
}

# Edit form payload — subnet_id, ip_addresses, identifier_type, identifier are all
# disabled on the edit form so browsers never submit them.  The view reads ip-addresses
# and identifier data from reservation_get instead.
_VALID_RESERVATION6_EDIT_POST = {
    "hostname": "test-host6",
    **_FORMSET_MGMT,
}


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4ListExceptions(_ViewTestBase):
    """Reservation list view — exception path coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    def test_hook_not_available_shows_warning(self):
        """reservation-get-page result=2 sets hook_available=False without crashing."""
        with stub_kea({"reservation-get-page": {"result": 2, "text": "unknown command 'reservation-get-page'"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context.get("hook_available", True))

    def test_network_error_during_fetch_keeps_hook_available(self):
        """A transport error during reservation-get-page keeps hook_available=True.

        Transport errors do not indicate the hook is missing — only result==2 does.
        """
        with stub_kea({"reservation-get-page": req.RequestException("connection refused")}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("hook_available", False))

    def test_kea_exception_non_result2_keeps_hook_available(self):
        """A reservation-get-page result!=2 keeps hook_available=True.

        Only result==2 (unknown command = hook not loaded) should hide the hook UI.
        """
        with stub_kea({"reservation-get-page": {"result": 1, "text": "general error"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("hook_available", False))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6ListExceptions(_ViewTestBase):
    """Reservation6 list view — exception path coverage."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations6", args=[self.server.pk])

    def test_hook_not_available_shows_warning(self):
        """reservation-get-page result=2 sets hook_available=False without crashing."""
        with stub_kea({"reservation-get-page": {"result": 2, "text": "unknown command 'reservation-get-page'"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context.get("hook_available", True))

    def test_network_error_during_fetch_keeps_hook_available(self):
        """A transport error during reservation-get-page keeps hook_available=True.

        Transport errors do not indicate the hook is missing — only result==2 does.
        """
        with stub_kea({"reservation-get-page": req.RequestException("timeout")}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("hook_available", False))

    def test_kea_exception_non_result2_keeps_hook_available(self):
        """A reservation-get-page result!=2 keeps hook_available=True.

        Only result==2 (unknown command = hook not loaded) should hide the hook UI.
        """
        with stub_kea({"reservation-get-page": {"result": 1, "text": "general error"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context.get("hook_available", False))


# ---------------------------------------------------------------------------
# Reservation4Add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4AddExceptions(_ViewTestBase):
    """ServerReservation4AddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

    def test_kea_exception_rerenders_form(self):
        """A reservation-add error must re-render the form with an error message."""
        with stub_kea({"subnet4-get": _subnet_get(4), "reservation-add": {"result": 1, "text": "already exists"}}):
            response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_kea_exception_result1_rerenders_form(self):
        """A reservation-add result=1 must re-render the form with an error message."""
        with stub_kea({"subnet4-get": _subnet_get(4), "reservation-add": {"result": 1, "text": "server error"}}):
            response = self.client.post(self._url(), _VALID_RESERVATION4_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_success_with_sync_to_netbox(self):
        """Successful add with sync_to_netbox=True runs the real sync → NetBox IP created."""
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with stub_kea(
            {"subnet4-get": _subnet_get(4), "reservation-add": {"result": 0}, "reservation-get-page": _RES_EMPTY_PAGE}
        ) as kea:
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("reservation-add"), 1)
        self.assertTrue(NbIP.objects.filter(address__startswith="10.0.0.55/").exists())

    def test_success_sync_failure_shows_warning(self):
        """Successful add where sync raises must show a warning (no 500).

        The sync boundary (a NetBox-side function tested in test_sync.py) is patched
        to raise so the view's error handling is exercised; the KeaClient is real.
        """
        post_data = {**_VALID_RESERVATION4_POST, "sync_to_netbox": "on"}
        with (
            patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=ValueError("sync fail")),
            stub_kea(
                {
                    "subnet4-get": _subnet_get(4),
                    "reservation-add": {"result": 0},
                    "reservation-get-page": _RES_EMPTY_PAGE,
                }
            ),
        ):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Reservation6Add exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6AddExceptions(_ViewTestBase):
    """ServerReservation6AddView POST exception paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])

    def test_kea_exception_rerenders_form(self):
        """A reservation-add error must re-render the form without leaking raw Kea text."""
        sentinel = "kea-detail-should-not-leak"
        with stub_kea({"subnet6-get": _subnet_get(6), "reservation-add": {"result": 1, "text": sentinel}}):
            response = self.client.post(self._url(), _VALID_RESERVATION6_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(django_messages.get_messages(response.wsgi_request))
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))
        self.assertFalse(any(sentinel in str(m) for m in msgs))

    def test_generic_exception_propagates(self):
        """An unexpected transport-layer error must propagate (not be silently caught)."""
        with stub_kea({"subnet6-get": _subnet_get(6), "reservation-add": RuntimeError("bang")}):
            with self.assertRaises(RuntimeError):
                self.client.post(self._url(), _VALID_RESERVATION6_POST)


# ---------------------------------------------------------------------------
# Reservation4Edit exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4EditExceptions(_ViewTestBase):
    """ServerReservation4EditView GET and POST exception paths."""

    def _url(self, subnet_id=1, ip="10.0.0.55"):
        return reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, subnet_id, ip])

    def test_get_redirects_on_kea_exception(self):
        """GET that raises a Kea error during reservation fetch must redirect."""
        with stub_kea({"reservation-get": {"result": 1, "text": "server error"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_redirects_on_transport_error(self):
        """GET that raises a transport error during reservation fetch must redirect."""
        with stub_kea({"reservation-get": req.ConnectionError("down")}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_404_when_reservation_not_found(self):
        """GET must return 404 when reservation_get returns None."""
        with stub_kea({"reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_post_kea_exception_rerenders_form(self):
        """A reservation-update error must re-render the form with an error message."""
        with stub_kea({"reservation-get": _res_get(_EDIT4_EXISTING), "reservation-update": {"result": 1, "text": "x"}}):
            response = self.client.post(self._url(), _VALID_RESERVATION4_EDIT_POST)
        self.assertEqual(response.status_code, 200)
        msgs = list(response.context["messages"])
        self.assertTrue(
            any("Kea reported an error" in str(m) for m in msgs), "Expected KeaException hint in flash message"
        )

    def test_post_generic_exception_propagates(self):
        """An unexpected transport-layer error on reservation-update must propagate."""
        with stub_kea({"reservation-get": _res_get(_EDIT4_EXISTING), "reservation-update": RuntimeError("crash")}):
            with self.assertRaises(RuntimeError):
                self.client.post(self._url(), _VALID_RESERVATION4_EDIT_POST)

    def test_post_success_with_sync(self):
        """Successful update with sync_to_netbox runs the real sync → NetBox IP created."""
        post_data = {**_VALID_RESERVATION4_EDIT_POST, "sync_to_netbox": "on"}
        with stub_kea(
            {
                "reservation-get": _res_get(_EDIT4_EXISTING),
                "reservation-update": {"result": 0},
                "reservation-get-page": _RES_EMPTY_PAGE,
            }
        ):
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(NbIP.objects.filter(address__startswith="10.0.0.55/").exists())

    def test_post_success_sync_failure_shows_warning(self):
        """Successful update where sync raises must show a warning."""
        post_data = {**_VALID_RESERVATION4_EDIT_POST, "sync_to_netbox": "on"}
        with (
            patch("netbox_kea.views.reservations.sync_reservation_to_netbox", side_effect=ValueError("oops")),
            stub_kea(
                {
                    "reservation-get": _res_get(_EDIT4_EXISTING),
                    "reservation-update": {"result": 0},
                    "reservation-get-page": _RES_EMPTY_PAGE,
                }
            ),
        ):
            response = self.client.post(self._url(), post_data, follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any("sync failed" in m.message.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Reservation6Edit exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6EditExceptions(_ViewTestBase):
    """ServerReservation6EditView GET and POST exception paths."""

    def _url(self, subnet_id=1, ip="2001:db8::1"):
        return reverse("plugins:netbox_kea:server_reservation6_edit", args=[self.server.pk, subnet_id, ip])

    def test_get_redirects_on_kea_exception(self):
        """GET that raises a Kea error during reservation fetch must redirect."""
        with stub_kea({"reservation-get": {"result": 1, "text": "error"}}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_redirects_on_transport_error(self):
        """GET that raises a transport error during reservation fetch must redirect."""
        with stub_kea({"reservation-get": req.ConnectionError("down")}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_get_404_when_reservation_not_found(self):
        """GET must return 404 when reservation_get returns None."""
        with stub_kea({"reservation-get": _RES_NOT_FOUND}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_post_kea_exception_rerenders_form(self):
        """A reservation-update error must re-render the form."""
        with stub_kea(
            {"reservation-get": _res_get(_EDIT6_EXISTING), "reservation-update": {"result": 1, "text": "error"}}
        ):
            response = self.client.post(self._url(), _VALID_RESERVATION6_EDIT_POST)
        self.assertEqual(response.status_code, 200)

    def test_post_generic_exception_propagates(self):
        """An unexpected transport-layer error on reservation-update must propagate."""
        with stub_kea({"reservation-get": _res_get(_EDIT6_EXISTING), "reservation-update": RuntimeError("crash")}):
            with self.assertRaises(RuntimeError):
                self.client.post(self._url(), _VALID_RESERVATION6_EDIT_POST)


# ---------------------------------------------------------------------------
# Reservation4Delete exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4DeleteExceptions(_ViewTestBase):
    """ServerReservation4DeleteView POST exception paths."""

    def _url(self, subnet_id=1, ip="10.0.0.55"):
        return reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, subnet_id, ip])

    def test_kea_exception_shows_error(self):
        """A reservation-del error must show an error message and redirect."""
        with stub_kea({"reservation-del": {"result": 1, "text": "not found"}, "reservation-get-page": _RES_EMPTY_PAGE}):
            response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_generic_exception_propagates(self):
        """An unexpected transport-layer error must propagate (not show a generic error message)."""
        with stub_kea({"reservation-del": RuntimeError("crash")}):
            with self.assertRaises(RuntimeError):
                self.client.post(self._url())


# ---------------------------------------------------------------------------
# Reservation6Delete exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6DeleteExceptions(_ViewTestBase):
    """ServerReservation6DeleteView POST exception paths."""

    def _url(self, subnet_id=1, ip="2001:db8::1"):
        return reverse("plugins:netbox_kea:server_reservation6_delete", args=[self.server.pk, subnet_id, ip])

    def test_kea_exception_shows_error(self):
        """A reservation-del error must show an error message."""
        with stub_kea({"reservation-del": {"result": 1, "text": "error"}, "reservation-get-page": _RES_EMPTY_PAGE}):
            response = self.client.post(self._url(), follow=True)
        msgs = list(response.context["messages"])
        self.assertTrue(any(m.level == django_messages.ERROR for m in msgs))

    def test_generic_exception_propagates(self):
        """An unexpected transport-layer error must propagate (not show a generic error message)."""
        with stub_kea({"reservation-del": RuntimeError("crash")}):
            with self.assertRaises(RuntimeError):
                self.client.post(self._url())


# ---------------------------------------------------------------------------
# _get_reservation_options_formset — partial submission path
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetReservationOptionsFormsetPartial(_ViewTestBase):
    """_build_reservation_options_formset: partial options-* keys but no management form."""

    def test_partial_options_keys_returns_invalid_formset(self):
        """When options-* keys exist without management form, returns (formset, False)."""
        from netbox_kea.views import _build_reservation_options_formset

        post_data = {"options-0-name": "domain-name-servers"}  # no TOTAL_FORMS key
        fs, is_valid = _build_reservation_options_formset(post_data)
        self.assertFalse(is_valid)
        self.assertTrue(fs.is_bound)
        self.assertTrue(fs.non_form_errors())


# ---------------------------------------------------------------------------
# Reservation list enrichment — thread pool exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationListEnrichmentExceptions(_ViewTestBase):
    """_enrich_reservations_with_lease_status: thread pool exception paths (via the list view)."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

    def test_no_reservations_skips_enrichment(self):
        """An empty reservation list → enrichment returns early (no lease query issued)."""
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("lease4-get-all", kea.commands())

    def test_thread_pool_generic_exception_returns_early(self):
        """A generic error in the lease-enrichment worker causes enrichment to return early."""
        host = {"subnet-id": 1, "ip-address": "10.0.0.5", "hw-address": "aa:bb:cc:dd:ee:ff"}
        # lease4-get-all raises an unexpected error in the worker thread; the enrichment
        # outer except swallows it so the list still renders.
        with stub_kea({"reservation-get-page": _res_page([host]), "lease4-get-all": RuntimeError("unexpected")}) as kea:
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-all", kea.commands())


# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6AddOptionDataAndSync(_ViewTestBase):
    """Reservation6 add with option-data and sync-to-NetBox."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])

    def test_post_with_option_data_included(self):
        """option-data is included in the reservation-add payload when the formset has entries."""
        post_data = {
            **_VALID_RESERVATION6_POST,
            "options-TOTAL_FORMS": "1",
            "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            "options-0-name": "dns-servers",
            "options-0-data": "2001:4860:4860::8888",
            "options-0-always_send": "",
            "options-0-DELETE": "",
        }
        with stub_kea({"subnet6-get": _subnet_get(6), "reservation-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(kea.commands().count("reservation-add"), 1)
        reservation_dict = kea.bodies("reservation-add")[0]["arguments"]["reservation"]
        option_data = reservation_dict.get("option-data", [])
        dns_entry = next((o for o in option_data if o.get("name") == "dns-servers"), None)
        self.assertIsNotNone(dns_entry, "dns-servers option not found in reservation option-data")
        self.assertEqual(dns_entry["data"], "2001:4860:4860::8888")

    def test_post_sync_success(self):
        """sync_to_netbox=on runs the real sync → NetBox IP created + info message queued."""
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        with stub_kea(
            {"subnet6-get": _subnet_get(6), "reservation-add": {"result": 0}, "reservation-get-page": _RES_EMPTY_PAGE}
        ):
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(NbIP.objects.filter(address__startswith="2001:db8::1/").exists())
        msgs = list(response.context["messages"])
        self.assertTrue(
            any(
                m.level == django_messages.INFO
                and ("synced" in m.message.lower() or "created" in m.message.lower() or "updated" in m.message.lower())
                for m in msgs
            ),
            f"Expected sync success message, got: {[m.message for m in msgs]}",
        )

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    def test_post_sync_exception_shows_warning(self, mock_sync):
        """sync raises exception → warning message queued (reservation still created)."""
        mock_sync.side_effect = ValueError("sync failed")
        post_data = {**_VALID_RESERVATION6_POST, "sync_to_netbox": "on"}
        with stub_kea(
            {"subnet6-get": _subnet_get(6), "reservation-add": {"result": 0}, "reservation-get-page": _RES_EMPTY_PAGE}
        ):
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()
        msgs = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.WARNING for m in msgs),
            f"Expected a WARNING message on sync failure, got: {[(m.level, m.message) for m in msgs]}",
        )


# ---------------------------------------------------------------------------
# Reservation sync requires IPAM write permission (force=True overwrite guard)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationSyncRequiresIpamPermission(_ViewTestBase):
    """The reservation-form sync uses force=True (overrides the foreign-IP guard).

    It must require IPAM write permission, not just server-edit access — otherwise
    a user with change_server but no IPAM rights could overwrite curated IPAddress
    records.
    """

    _RES = {
        "ip-address": "192.0.2.55",
        "hw-address": "11:22:33:44:55:66",
        "hostname": "res-host",
        "subnet-id": 1,
    }

    def _request(self, user):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user
        setattr(request, "session", "session")
        setattr(request, "_messages", FallbackStorage(request))
        return request

    def test_sync_skipped_without_ipam_permission(self):
        from django.contrib.auth import get_user_model
        from ipam.models import IPAddress

        from netbox_kea.views.reservations import _run_reservation_success_side_effects

        IPAddress.objects.create(address="192.0.2.55/32", status="active", description="Router loopback")
        limited = get_user_model().objects.create_user(username="res_no_ipam", password="x")
        _run_reservation_success_side_effects(
            self._request(limited), self.server, dict(self._RES), 4, "created", sync_to_netbox=True
        )
        # Foreign IP left exactly as the operator set it — the force-sync was gated out.
        ip = IPAddress.objects.get(address="192.0.2.55/32")
        self.assertEqual(ip.status, "active")
        self.assertEqual(ip.description, "Router loopback")

    def test_sync_runs_with_ipam_permission(self):
        from ipam.models import IPAddress

        from netbox_kea.views.reservations import _run_reservation_success_side_effects

        IPAddress.objects.create(address="192.0.2.55/32", status="active", description="Router loopback")
        # self.user is a superuser → has IPAM write permission.
        _run_reservation_success_side_effects(
            self._request(self.user), self.server, dict(self._RES), 4, "created", sync_to_netbox=True
        )
        ip = IPAddress.objects.get(address__startswith="192.0.2.55/")
        # With IPAM permission the force-sync claims the IP (reserved status).
        self.assertEqual(ip.status, "reserved")


# ---------------------------------------------------------------------------
# Reservation6 Edit — option-data and sync paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6EditOptionDataAndSync(_ViewTestBase):
    """Reservation6 edit with option-data and sync-to-NetBox."""

    _EXISTING = {
        "subnet-id": 1,
        "ip-addresses": ["2001:db8::1", "2001:db8::2"],
        "duid": "00:01:00:01:12:34:56:78:aa:bb:cc:dd:ee:ff",
        "hostname": "v6host",
        "option-data": [],
    }

    def _url(self):
        return reverse(
            "plugins:netbox_kea:server_reservation6_edit",
            args=[self.server.pk, 1, "2001:db8::1"],
        )

    def test_post_with_option_data(self):
        """option-data is included in the reservation-update payload when the formset has entries.

        Uses an edit-shaped payload (no subnet_id/ip_addresses) and a two-address existing
        reservation to verify the multi-IP preserve path runs and both IPs are preserved.
        """
        post_data = {
            **_VALID_RESERVATION6_EDIT_POST,
            "options-TOTAL_FORMS": "1",
            "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            "options-0-name": "ntp-servers",
            "options-0-data": "2001:db8::1:1",
            "options-0-always_send": "",
            "options-0-DELETE": "",
        }
        with stub_kea({"reservation-get": _res_get(self._EXISTING), "reservation-update": {"result": 0}}) as kea:
            response = self.client.post(self._url(), post_data)
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(kea.commands().count("reservation-update"), 1)
        reservation_dict = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        # Both original IPs must be preserved (not collapsed to one).
        self.assertEqual(
            reservation_dict.get("ip-addresses"),
            ["2001:db8::1", "2001:db8::2"],
            "Edit must preserve all existing ip-addresses from reservation_get",
        )
        option_data = reservation_dict.get("option-data", [])
        ntp_entry = next((o for o in option_data if o.get("name") == "ntp-servers"), None)
        self.assertIsNotNone(ntp_entry, "ntp-servers option not found in reservation option-data")
        self.assertEqual(ntp_entry["data"], "2001:db8::1:1")

    def test_post_without_an_options_formset_keeps_existing_option_data(self):
        """The address-keyed route merges too: a submission that omits the options section keeps them."""
        existing = {**self._EXISTING, "option-data": [{"name": "ntp-servers", "data": "2001:db8::99"}]}
        with stub_kea({"reservation-get": _res_get(existing), "reservation-update": {"result": 0}}) as kea:
            self.client.post(self._url(), dict(_VALID_RESERVATION6_EDIT_POST))
        reservation_dict = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(reservation_dict["option-data"], [{"name": "ntp-servers", "data": "2001:db8::99"}])

    def test_post_sync_success(self):
        """sync_to_netbox=on runs the real sync → info message queued."""
        post_data = {**_VALID_RESERVATION6_EDIT_POST, "sync_to_netbox": "on"}
        with stub_kea(
            {
                "reservation-get": _res_get(self._EXISTING),
                "reservation-update": {"result": 0},
                "reservation-get-page": _RES_EMPTY_PAGE,
            }
        ):
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(NbIP.objects.filter(address__startswith="2001:db8::1/").exists())
        msgs = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.INFO for m in msgs),
            f"Expected INFO message on sync success, got: {[(m.level, m.message) for m in msgs]}",
        )

    @patch("netbox_kea.views.reservations.sync_reservation_to_netbox")
    def test_post_sync_exception(self, mock_sync):
        """sync exception → warning message queued (reservation still updated)."""
        mock_sync.side_effect = ValueError("sync fail")
        post_data = {**_VALID_RESERVATION6_EDIT_POST, "sync_to_netbox": "on"}
        with stub_kea(
            {
                "reservation-get": _res_get(self._EXISTING),
                "reservation-update": {"result": 0},
                "reservation-get-page": _RES_EMPTY_PAGE,
            }
        ):
            response = self.client.post(self._url(), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()
        msgs = list(response.context["messages"])
        self.assertTrue(
            any(m.level == django_messages.WARNING for m in msgs),
            f"Expected WARNING message on sync failure, got: {[(m.level, m.message) for m in msgs]}",
        )


# ---------------------------------------------------------------------------
# _enrich_reservations_with_lease_status — direct unit tests (real KeaClient)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichReservationsLeaseStatusCoverage(_ViewTestBase):
    """_enrich_reservations_with_lease_status: direct unit tests for edge cases.

    These drive a real ``KeaClient`` (no DB needed) with the HTTP boundary stubbed;
    ``clone()`` builds a fresh session in each worker thread, which the class-level
    ``requests.Session.post`` patch also covers.
    """

    def test_result3_returns_empty_list(self):
        """lease-get-all result=3 → subnet confirmed empty → has_active_lease set False."""
        from netbox_kea.views import _enrich_reservations_with_lease_status

        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        with stub_kea({"lease4-get-all": {"result": 3}}):
            client = KeaClient(url="https://kea.example.com")
            _enrich_reservations_with_lease_status(client, reservations, 4)
        # result=3 means empty — confirmed no active lease, so has_active_lease is set False
        self.assertFalse(reservations[0].get("has_active_lease", True))

    def test_kea_exception_non_result2_returns_empty(self):
        """A lease-get-all result != 2/3 → subnet treated as indeterminate → has_active_lease unset."""
        from netbox_kea.views import _enrich_reservations_with_lease_status

        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        with stub_kea({"lease4-get-all": {"result": 1, "text": "error"}}):
            client = KeaClient(url="https://kea.example.com")
            _enrich_reservations_with_lease_status(client, reservations, 4)
        # result != 2 → subnet indeterminate → has_active_lease must remain unset
        self.assertNotIn("has_active_lease", reservations[0])

    def test_no_subnet_id_skips_fetch(self):
        """Reservations without subnet-id → unique_subnet_ids empty → enrichment returns early."""
        from netbox_kea.views import _enrich_reservations_with_lease_status

        reservations = [{"ip-address": "10.0.0.1"}]  # no subnet-id
        with stub_kea({}) as kea:
            client = KeaClient(url="https://kea.example.com")
            _enrich_reservations_with_lease_status(client, reservations, 4)
        # No valid subnet-ids → no Kea traffic at all.
        self.assertEqual(kea.commands(), [])

    def test_as_completed_exception_returns_early(self):
        """Exception from as_completed → outer except swallows it and enrichment returns early."""
        from netbox_kea.views import _enrich_reservations_with_lease_status

        reservations = [{"ip-address": "10.0.0.1", "subnet-id": 42}]
        with (
            patch(
                "netbox_kea.views.reservations.concurrent.futures.as_completed",
                side_effect=RuntimeError("as_completed failed"),
            ) as mock_as_completed,
            stub_kea({"lease4-get-all": {"result": 3}}),
        ):
            client = KeaClient(url="https://kea.example.com")
            _enrich_reservations_with_lease_status(client, reservations, 4)
        # as_completed must have been reached (executor submitted tasks)
        mock_as_completed.assert_called_once()
        # outer except returns early — has_active_lease stays unset
        self.assertNotIn("has_active_lease", reservations[0])


# ---------------------------------------------------------------------------
# Lease status for reservations that reserve no address
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestAddressLessReservationLeaseStatus(_ViewTestBase):
    """A reservation with no address still has a lease state — match on the identifier.

    ``has_active_lease`` was derived purely from the reserved addresses, so an
    address-less host could only ever report "No Lease", even while its client held an
    active lease.  Kea's leases carry the client identifier (``hw-address`` /
    ``client-id`` on v4, ``duid`` on v6), so that is what these rows match on — inside
    their own subnet only, since the same client leasing elsewhere is a different row.
    """

    ADDRESSLESS_V4 = {"subnet-id": 42, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "printer-1"}

    def _enrich(self, reservations, leases_by_subnet, version=4):
        from netbox_kea.views import _enrich_reservations_with_lease_status

        with stub_kea({f"lease{version}-get-all": _leases_per_subnet(leases_by_subnet)}):
            client = KeaClient(url="https://kea.example.com")
            _enrich_reservations_with_lease_status(client, reservations, version)
        return reservations

    def test_list_shows_an_active_lease_for_an_address_less_row(self):
        """End to end: the tab renders "Active Lease" for a host that reserves no address.

        The lease's hardware address is written dash-delimited and upper-case — the same
        identifier, spelled the other way Kea and operators write it.
        """
        lease = {"ip-address": "10.0.0.77", "hw-address": "AA-BB-CC-DD-EE-FF", "subnet-id": 42, "state": 0}
        with stub_kea(
            {
                "reservation-get-page": _res_page([self.ADDRESSLESS_V4]),
                "lease4-get-all": _leases_per_subnet({42: [lease]}),
            }
        ):
            response = self.client.get(reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active Lease")
        self.assertNotContains(response, "No Lease")

    def test_a_lease_in_another_subnet_does_not_light_up_the_row(self):
        """Same client, different subnet: that lease belongs to the other reservation."""
        elsewhere = {"subnet-id": 7, "hw-address": "11:22:33:44:55:66", "hostname": "elsewhere"}
        lease = {"ip-address": "10.0.7.9", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 7, "state": 0}
        reservations = [dict(self.ADDRESSLESS_V4), elsewhere]
        self._enrich(reservations, {7: [lease]})
        self.assertFalse(reservations[0]["has_active_lease"])

    def test_no_matching_lease_is_no_lease(self):
        lease = {"ip-address": "10.0.0.77", "hw-address": "11:22:33:44:55:66", "subnet-id": 42, "state": 0}
        reservations = [dict(self.ADDRESSLESS_V4)]
        self._enrich(reservations, {42: [lease]})
        self.assertFalse(reservations[0]["has_active_lease"])

    def test_v4_client_id_row_matches_the_lease_client_id(self):
        host = {"subnet-id": 42, "client-id": "01:aa:bb:cc:dd:ee:ff"}
        lease = {"ip-address": "10.0.0.77", "client-id": "01:AA:BB:CC:DD:EE:FF", "subnet-id": 42, "state": 0}
        reservations = [dict(host)]
        self._enrich(reservations, {42: [lease]})
        self.assertTrue(reservations[0]["has_active_lease"])

    def test_v6_address_less_row_matches_the_lease_duid(self):
        host = {"subnet-id": 12, "duid": "00:01:00:01:12:34:56:78", "prefixes": ["2001:db8:1::/64"]}
        lease = {"ip-address": "2001:db8::5", "duid": "00:01:00:01:12:34:56:78", "subnet-id": 12, "state": 0}
        reservations = [dict(host)]
        self._enrich(reservations, {12: [lease]}, version=6)
        self.assertTrue(reservations[0]["has_active_lease"])

    def test_opaque_identifier_row_has_nothing_to_match_on(self):
        """Kea leases carry no flex-id, so such a row cannot be matched by identifier."""
        lease = {"ip-address": "10.0.0.77", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 42, "state": 0}
        reservations = [{"subnet-id": 42, "flex-id": "vendor-42"}]
        self._enrich(reservations, {42: [lease]})
        self.assertFalse(reservations[0]["has_active_lease"])

    def test_addressed_row_still_matches_on_its_address(self):
        """Regression guard: a row that does reserve an address keeps the IP match."""
        host = {"subnet-id": 42, "hw-address": "11:22:33:44:55:66", "ip-address": "10.0.0.5"}
        lease = {"ip-address": "10.0.0.5", "hw-address": "99:99:99:99:99:99", "subnet-id": 42, "state": 0}
        reservations = [dict(host)]
        self._enrich(reservations, {42: [lease]})
        self.assertTrue(reservations[0]["has_active_lease"])

    def test_addressed_row_is_not_lit_up_by_its_clients_other_lease(self):
        """The reserved address is what an addressed row reports on, not the client."""
        host = {"subnet-id": 42, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "10.0.0.5"}
        lease = {"ip-address": "10.0.0.77", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 42, "state": 0}
        reservations = [dict(host)]
        self._enrich(reservations, {42: [lease]})
        self.assertFalse(reservations[0]["has_active_lease"])

    def test_indeterminate_subnet_leaves_the_flag_unset(self):
        """An address-less row must not report "No Lease" when the subnet could not be read."""
        reservations = [dict(self.ADDRESSLESS_V4)]
        with stub_kea({"lease4-get-all": {"result": 1, "text": "error"}}):
            from netbox_kea.views import _enrich_reservations_with_lease_status

            client = KeaClient(url="https://kea.example.com")
            _enrich_reservations_with_lease_status(client, reservations, 4)
        self.assertNotIn("has_active_lease", reservations[0])


# ---------------------------------------------------------------------------
# _warn_pool_reservation_overlap — edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWarnPoolReservationOverlapCoverage(_ViewTestBase):
    """Direct unit tests for _warn_pool_reservation_overlap helper."""

    def test_cidr_pool_creates_ipnetwork(self):
        """Pool string without dash (CIDR notation) → IPNetwork path is taken."""
        from netbox_kea.views import _warn_pool_reservation_overlap

        request = self._make_request()
        with stub_kea({"reservation-get-page": _RES_EMPTY_PAGE}):
            client = self.server.get_client(version=4)
            _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0/24")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)

    def test_host_with_different_subnet_id_skipped(self):
        """Host whose subnet-id doesn't match the requested subnet_id is skipped."""
        from netbox_kea.views import _warn_pool_reservation_overlap

        request = self._make_request()
        with stub_kea({"reservation-get-page": _res_page([{"subnet-id": 999, "ip-address": "10.0.0.5"}])}):
            client = self.server.get_client(version=4)
            _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0-10.0.0.100")
        # host skipped → no warning
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)

    def test_malformed_ip_skipped(self):
        """Malformed IP address string is silently skipped (inner except path)."""
        from netbox_kea.views import _warn_pool_reservation_overlap

        request = self._make_request()
        with stub_kea({"reservation-get-page": _res_page([{"subnet-id": 1, "ip-address": "NOT_AN_IP"}])}):
            client = self.server.get_client(version=4)
            _warn_pool_reservation_overlap(request, client, 4, subnet_id=1, pool_str="10.0.0.0-10.0.0.100")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)


# ---------------------------------------------------------------------------
# _warn_reservation_pool_overlap — edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWarnReservationPoolOverlapCoverage(_ViewTestBase):
    """Direct unit tests for _warn_reservation_pool_overlap helper."""

    def test_empty_pool_string_skipped(self):
        """Pool entry with empty pool string is silently skipped."""
        from netbox_kea.views import _warn_reservation_pool_overlap

        request = self._make_request()
        with stub_kea({"subnet4-get": _subnet_get(4, pools=[""])}):
            client = self.server.get_client(version=4)
            _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)

    def test_cidr_pool_creates_ipnetwork(self):
        """Pool string without dash (CIDR notation) → IPNetwork path is taken; IP inside → warning."""
        from netbox_kea.views import _warn_reservation_pool_overlap

        request = self._make_request()
        with stub_kea({"subnet4-get": _subnet_get(4, pools=["10.0.0.0/24"])}):
            client = self.server.get_client(version=4)
            _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].level, django_messages.WARNING)

    def test_client_command_exception_swallowed(self):
        """A transport error from client.command is swallowed and no warning is issued."""
        from netbox_kea.views import _warn_reservation_pool_overlap

        request = self._make_request()
        with stub_kea({"subnet4-get": req.RequestException("network failure")}):
            client = self.server.get_client(version=4)
            _warn_reservation_pool_overlap(request, client, 4, subnet_id=1, ip_str="10.0.0.5")
        msgs = list(django_messages.get_messages(request))
        self.assertEqual(len(msgs), 0)


# ---------------------------------------------------------------------------
# Reservation mutation bare except — programming errors must propagate
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationMutationBareExcept(_ViewTestBase):
    """Reservation mutation handlers must not swallow programming errors via bare except Exception."""

    def test_attribute_error_from_add_propagates(self):
        """An AttributeError raised at the Kea boundary during reservation-add must propagate."""
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        with stub_kea({"subnet4-get": _subnet_get(4), "reservation-add": AttributeError("programming bug")}):
            with self.assertRaises(AttributeError):
                self.client.post(url, {**_VALID_RESERVATION4_POST})

    def test_attribute_error_from_v6_add_propagates(self):
        """An AttributeError raised at the Kea boundary during reservation-add (v6) must propagate."""
        url = reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])
        with stub_kea({"subnet6-get": _subnet_get(6), "reservation-add": AttributeError("programming bug")}):
            with self.assertRaises(AttributeError):
                self.client.post(url, {**_VALID_RESERVATION6_POST})


# ---------------------------------------------------------------------------
# Issue #64 Part 1: live NetBox IP-check advisory wiring on the Add form
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationAddFormIPCheckWiring(_ViewTestBase):
    """The reservation Add form renders the NetBox IP-check target div + blur script.

    These render the real view → template (no Kea call needed for the Add GET),
    standing in for a live-render check since the advisory is plain-fetch JS.
    """

    def test_v4_add_form_includes_ip_check_div_and_script(self):
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        body = self.client.get(url).content.decode()
        check_url = reverse("plugins:netbox_kea:reservation_check_ip", args=[self.server.pk])
        self.assertIn('id="netbox-ip-check"', body)
        self.assertIn(check_url, body)
        self.assertIn('addEventListener("blur"', body)
        # v4 watches the single-IP field.
        self.assertIn('"id_ip_address"', body)

    def test_v6_add_form_targets_multi_ip_field(self):
        url = reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])
        body = self.client.get(url).content.decode()
        self.assertIn('id="netbox-ip-check"', body)
        # v6 watches the comma-separated ip_addresses field.
        self.assertIn('"id_ip_addresses"', body)

    def test_v6_add_form_script_checks_every_address(self):
        """The blur script must check *all* comma-separated v6 addresses, not just the first.

        Regression guard: the original handler used split(",")[0], so a conflict in
        the 2nd+ DHCPv6 address was silently missed.
        """
        url = reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk])
        body = self.client.get(url).content.decode()
        # Splits the field and fans a lookup out per address — never a single-index pick.
        self.assertIn('.split(",")', body)
        self.assertIn("Promise.all", body)
        self.assertNotIn('split(",")[0]', body)

    def test_edit_form_omits_blur_script(self):
        """Edit mode disables the IP field, so the advisory script must not attach."""
        existing = {"hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "10.0.0.55", "subnet-id": 1}
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 1, "10.0.0.55"])
        with stub_kea({"reservation-get": _res_get(existing), "lease4-get": _LEASE_NOT_FOUND}):
            body = self.client.get(url).content.decode()
        check_url = reverse("plugins:netbox_kea:reservation_check_ip", args=[self.server.pk])
        self.assertNotIn('addEventListener("blur"', body)
        self.assertNotIn(check_url, body)

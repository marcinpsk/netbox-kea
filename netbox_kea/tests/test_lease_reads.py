# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Lease reads share typed facts, scope and completeness: real requests, views, REST and the real KeaClient.

Only the Kea HTTP boundary is stubbed. Records come from the lease replies recorded from a real Kea 3.2.
"""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse
from ipam.models import IPAddress
from rest_framework.test import APIClient

from netbox_kea.ipam_reconciliation import LeasePhase, reconcile

from .kea_stub import (
    _catalogue_responses_for_subnets,
    _leases_per_subnet,
    _res_page,
    _subnet_stats,
    lease_page,
    lease_pages,
    lease_record,
    lease_reply,
    queued,
    stub_kea,
)
from .test_ipam_reconciliation import _kea, _lease, _links, _reconcile, _row, _server
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase, plugins_config

_SUBNETS4 = [{"id": 10, "subnet": "192.0.2.0/24"}]
_SUBNETS6 = [{"id": 10, "subnet": "2001:db8:1::/64"}]
#: The fields of the documented public Lease projection.
_PUBLIC_FIELDS = {
    "family",
    "kind",
    "address",
    "prefix_length",
    "subnet_id",
    "state",
    "current",
    "binding",
    "hostname",
    "valid_lifetime",
    "last_transaction",
    "expiration",
}


def _recorded_leases(family: int) -> list[dict]:
    """Return the ``lease{v}-get-all`` records recorded from a real Kea, each one current from now."""
    recorded = json.loads((Path(__file__).with_name("kea_recordings") / f"dhcp{family}.json").read_text())
    records = recorded["leases"][f"lease{family}-get-all"]["arguments"]["leases"]
    return [{**record, "cltt": int(time.time())} for record in records]


def _malformed(address: str, **changes) -> dict:
    """Return a recorded lease record that the reader must exclude."""
    return {**lease_record("192.0.2.30"), "ip-address": address, **changes}


def _prefix_only_at(address: str, record: dict):
    """A ``lease6-get`` responder that holds one delegated prefix at *address*, as Kea 3.2 answers."""

    def respond(body: dict) -> dict:
        arguments = body["arguments"]
        if arguments == {"ip-address": address, "type": "IA_PD"}:
            return {"result": 0, "text": "IPv6 lease found.", "arguments": record}
        return {"result": 3, "text": "Lease not found."}

    return respond


#: A DHCPv4 client ID that a lease can carry (Kea allows 255 octets) but a Reservation cannot (128).
_LONG_CLIENT_ID = ":".join(["01"] * 129)


def _csv_rows(response) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(response.content.decode())))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class LeaseBrowsingTest(_ViewTestBase):
    """Browsing shows valid siblings beside the safe reason for each excluded record."""

    def _url(self, family: int = 4) -> str:
        return reverse(f"plugins:netbox_kea:server_leases{family}", args=[self.server.pk])

    def test_a_subnet_search_keeps_valid_siblings_and_shows_safe_reasons(self):
        records = [
            lease_record("192.0.2.10"),
            _malformed("not-an-address"),
            lease_record("192.0.2.12", state="private-state-value"),
            lease_record("192.0.2.13"),
        ]
        responses = {
            **_catalogue_responses_for_subnets(4, _SUBNETS4),
            "lease4-get-all": lease_reply(*records),
            "reservation-get": {"result": 3},
        }
        with stub_kea(responses):
            response = self.client.get(self._url(), {"by": "subnet_id", "q": "10"}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row.record["ip_address"] for row in response.context["table"].rows], ["192.0.2.10", "192.0.2.13"]
        )
        self.assertEqual(
            response.context["lease_diagnostics"],
            [
                "leases[1] (ip-address): The lease address is not valid.",
                "leases[2] (state): A lease field has the wrong type.",
            ],
        )
        self.assertContains(response, "2 lease records that could not be read")
        self.assertNotContains(response, "private-state-value")
        self.assertNotContains(response, "not-an-address")

    def test_a_dhcpv6_address_search_finds_a_delegated_prefix(self):
        prefix = lease_record("2001:db8:100:100::", type="IA_PD", prefix_len=56)
        responses = {
            **_catalogue_responses_for_subnets(6, _SUBNETS6),
            "lease6-get": _prefix_only_at("2001:db8:100:100::", prefix),
            "reservation-get": {"result": 3},
        }
        with stub_kea(responses):
            response = self.client.get(self._url(6), {"by": "ip", "q": "2001:db8:100:100::"}, HTTP_HX_REQUEST="true")

        rows = [row.record for row in response.context["table"].rows]
        self.assertEqual(
            [(row["ip_address"], row["kind"], row["prefix_length"]) for row in rows],
            [("2001:db8:100:100::", "delegated-prefix", 56)],
        )

    def test_only_an_address_lease_row_offers_sync_and_edit(self):
        records = [
            lease_record("2001:db8:1::10", subnet_id=10),
            lease_record("2001:db8:100:100::", type="IA_PD", prefix_len=56, subnet_id=10),
        ]
        responses = {
            **_catalogue_responses_for_subnets(6, _SUBNETS6),
            "lease6-get-all": lease_reply(*records),
            "reservation-get": {"result": 3},
        }
        with stub_kea(responses):
            response = self.client.get(self._url(6), {"by": "subnet_id", "q": "10"}, HTTP_HX_REQUEST="true")

        rows = {row.record["kind"]: row.record for row in response.context["table"].rows}
        self.assertEqual(
            rows["address"]["edit_url"],
            reverse("plugins:netbox_kea:server_lease6_edit", args=[self.server.pk, "2001:db8:1::10"]),
        )
        self.assertTrue(rows["address"].get("sync_url"))
        # Edit and Sync look up an address Lease, which Kea reports absent for a delegated prefix.
        self.assertIsNone(rows["delegated-prefix"].get("edit_url"))
        self.assertIsNone(rows["delegated-prefix"].get("sync_url"))

    def test_an_unconfigured_subnet_cidr_is_refused_not_reported_empty(self):
        responses = {**_catalogue_responses_for_subnets(4, _SUBNETS4)}
        with stub_kea(responses) as kea:
            response = self.client.get(self._url(), {"by": "subnet", "q": "198.51.100.0/24"}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("lease4-get-all", kea.commands())
        self.assertContains(response, "This Subnet CIDR is not configured on the Kea server")
        self.assertNotContains(response, "No leases found.")

    def test_every_lease_state_shows_its_own_label(self):
        responses = {
            **_catalogue_responses_for_subnets(6, _SUBNETS6),
            "lease6-get-all": lease_reply(*_recorded_leases(6)),
            "reservation-get": {"result": 3},
        }
        with stub_kea(responses):
            response = self.client.get(self._url(6), {"by": "subnet_id", "q": "10"}, HTTP_HX_REQUEST="true")

        labels = {row.record["ip_address"]: row.record["state_label"] for row in response.context["table"].rows}
        self.assertEqual(labels["2001:db8:1::11"], "Registered")
        self.assertEqual(labels["2001:db8:1::14"], "Released")
        self.assertEqual(labels["2001:db8:1::13"], "Expired")

    def test_a_malformed_exact_lease_cannot_be_edited_and_is_not_reported_as_absent(self):
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "192.0.2.10"])
        with stub_kea({"lease4-get": {"result": 0, "arguments": lease_record("192.0.2.10", drop=("state",))}}):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(messages, ["Kea returned a lease that could not be read; it cannot be edited."])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class LeaseExportTest(_ViewTestBase):
    """A complete export refuses an incomplete observation; a limited one says so."""

    def _url(self, family: int = 4) -> str:
        return reverse(f"plugins:netbox_kea:server_leases{family}", args=[self.server.pk])

    def test_export_all_refuses_an_observation_with_a_malformed_record(self):
        records = [lease_record("192.0.2.10"), lease_record("192.0.2.11", valid_lft=True)]
        with stub_kea({"lease4-get-page": lease_pages(records)}):
            response = self.client.get(self._url(), {"export_all": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            [
                (
                    "Export refused: Kea returned 1 lease record(s) that could not be read "
                    "(A lease field has the wrong type.). A complete export cannot leave them out."
                )
            ],
        )

    def test_export_all_writes_kea_facts_not_display_labels(self):
        now = int(time.time())
        records = [
            lease_record("192.0.2.10", cltt=now - 7200, valid_lft=3600),
            lease_record("192.0.2.11", valid_lft=0xFFFFFFFF, cltt=now),
        ]
        with stub_kea({"lease4-get-page": lease_pages(records)}):
            response = self.client.get(self._url(), {"export_all": "1"})

        self.assertEqual(response.status_code, 200)
        header = response.content.decode().splitlines()[0]
        self.assertEqual(
            header,
            "family,kind,address,prefix_length,subnet_id,state,current,hostname,valid_lifetime,"
            "last_transaction,infinite,expires_at,hw_address,client_id",
        )
        expired, infinite = _csv_rows(response)
        self.assertEqual(
            {key: expired[key] for key in ("state", "current", "valid_lifetime", "infinite")},
            {"state": "assigned", "current": "false", "valid_lifetime": "3600", "infinite": "false"},
        )
        self.assertEqual(expired["expires_at"], datetime.fromtimestamp(now - 3600, tz=timezone.utc).isoformat())
        self.assertEqual(
            {key: infinite[key] for key in ("current", "valid_lifetime", "infinite", "expires_at")},
            {"current": "true", "valid_lifetime": "4294967295", "infinite": "true", "expires_at": ""},
        )

    def test_export_all_has_kind_aware_documented_columns(self):
        with stub_kea({"lease6-get-page": lease_pages(_recorded_leases(6))}):
            response = self.client.get(self._url(6), {"export_all": "1"})

        self.assertEqual(response.status_code, 200)
        rows = {row["address"]: row for row in _csv_rows(response)}
        self.assertEqual(len(rows), 10)
        self.assertEqual(
            {key: rows["2001:db8:100:100::"][key] for key in ("family", "kind", "prefix_length")},
            {"family": "6", "kind": "delegated-prefix", "prefix_length": "56"},
        )
        self.assertEqual(rows["2001:db8:1::10"]["kind"], "address")
        self.assertEqual(rows["2001:db8:1::15"]["infinite"], "true")
        self.assertEqual(list(rows["2001:db8:1::10"])[-4:], ["duid", "iaid", "hw_address", "preferred_lifetime"])

    def test_a_malformed_record_after_the_cap_refuses_the_export_for_the_cap(self):
        records = [lease_record("192.0.2.10"), lease_record("192.0.2.11")]
        probe = lease_page({**lease_record("192.0.2.12"), "ip-address": "not-an-address"})
        with (
            patch("netbox_kea.views.leases._LEASE_EXPORT_MAX_LEASES", new=2),
            stub_kea({"lease4-get-page": queued(lease_page(*records), probe)}),
        ):
            response = self.client.get(self._url(), {"export_all": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Export is limited to 2 leases. Narrow the lease set before exporting."],
        )

    def test_a_search_export_is_complete_or_explicitly_limited(self):
        records = [lease_record("192.0.2.10"), lease_record("192.0.2.11", state=True)]
        responses = {**_catalogue_responses_for_subnets(4, _SUBNETS4), "lease4-get-all": lease_reply(*records)}
        query = {"by": "subnet_id", "q": "10"}
        with stub_kea(responses):
            complete = self.client.get(self._url(), {**query, "export": ""})
            limited = self.client.get(self._url(), {**query, "export": "table"})

        self.assertEqual(complete.status_code, 302)
        self.assertIn("Export refused", str(next(iter(get_messages(complete.wsgi_request)))))
        self.assertEqual(limited.status_code, 200)
        self.assertIn("leases_limited_coverage.csv", limited["Content-Disposition"])
        self.assertEqual([row["IP Address"] for row in _csv_rows(limited)], ["192.0.2.10"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class CombinedLeaseViewTest(_ViewTestBase):
    def test_a_malformed_record_keeps_the_server_and_its_valid_siblings(self):
        records = [lease_record("192.0.2.10"), _malformed("not-an-address")]
        url = reverse("plugins:netbox_kea:combined_leases4")
        with stub_kea({"lease4-get-all": lease_reply(*records)}):
            response = self.client.get(url, {"q": "10", "by": "subnet_id", "server": self.server.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["errors"], [])
        self.assertEqual([row["ip_address"] for row in response.context["table"].data], ["192.0.2.10"])
        self.assertEqual(
            response.context["incomplete_servers"],
            [(self.server.name, "1 record(s) could not be read: The lease address is not valid.")],
        )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class LeaseRestTest(_ViewTestBase):
    """REST publishes normalized typed facts with explicit scope and coverage."""

    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def _get(self, family: int, params: dict, responses: dict):
        url = reverse(f"plugins-api:netbox_kea-api:server-leases{family}", args=[self.server.pk])
        with stub_kea(responses):
            return self.api.get(url, params)

    def test_results_are_the_public_projection_with_safe_diagnostics(self):
        records = [lease_record("192.0.2.10"), lease_record("192.0.2.11", state="private-state-value")]
        response = self._get(4, {"subnet_id": "10"}, {"lease4-get-all": lease_reply(*records)})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        (result,) = data["results"]
        self.assertEqual(set(result), _PUBLIC_FIELDS)
        self.assertEqual(
            {key: result[key] for key in ("family", "kind", "address", "prefix_length", "state", "current")},
            {
                "family": 4,
                "kind": "address",
                "address": "192.0.2.10",
                "prefix_length": None,
                "state": "assigned",
                "current": True,
            },
        )
        self.assertEqual(result["binding"], {"hw_address": "aa:bb:cc:00:00:10", "client_id": "01:aa:bb:cc:00:00:10"})
        self.assertEqual(
            data["diagnostics"],
            [
                {
                    "code": "invalid-type",
                    "field": "state",
                    "message": "A lease field has the wrong type.",
                    "source_position": "leases[1]",
                    "kinds": ["address"],
                }
            ],
        )
        self.assertEqual((data["complete"], data["coverage"], data["next_cursor"]), (False, "exhaustive", None))
        self.assertEqual(data["query"], {"family": 4, "selector": "subnet_id", "value": 10, "state": None})
        body = json.dumps(data)
        for private in ("user-context", "user_context", "rack", "state_label", "private-state-value"):
            self.assertNotIn(private, body)

    def test_a_dhcpv6_address_search_reads_both_kinds(self):
        prefix = lease_record("2001:db8:100:100::", type="IA_PD", prefix_len=56)
        url = reverse("plugins-api:netbox_kea-api:server-leases6", args=[self.server.pk])
        with stub_kea({"lease6-get": _prefix_only_at("2001:db8:100:100::", prefix)}) as kea:
            response = self.api.get(url, {"ip_address": "2001:db8:100:100::"})

        data = response.json()
        self.assertEqual([(r["kind"], r["prefix_length"]) for r in data["results"]], [("delegated-prefix", 56)])
        self.assertTrue(data["complete"])
        self.assertEqual(
            [body["arguments"] for body in kea.bodies("lease6-get")],
            [{"ip-address": "2001:db8:100:100::"}, {"ip-address": "2001:db8:100:100::", "type": "IA_PD"}],
        )

    def test_a_dhcpv6_address_search_is_complete_only_when_each_kind_is_confirmed(self):
        cases = (
            ({"result": 3, "text": "Lease not found."}, True),
            (
                lambda body: (
                    {"result": 3}
                    if "type" in body["arguments"]
                    else {"result": 0, "arguments": lease_record("2001:db8:1::10", drop=("state",))}
                ),
                False,
            ),
        )
        for reply, complete in cases:
            with self.subTest(complete=complete):
                response = self._get(6, {"ip_address": "2001:db8:1::10"}, {"lease6-get": reply})
                data = response.json()
                self.assertEqual((data["count"], data["complete"]), (0, complete))

    def test_delegated_prefixes_and_infinite_lifetimes_are_explicit(self):
        response = self._get(6, {"subnet_id": "10"}, {"lease6-get-all": lease_reply(*_recorded_leases(6))})

        results = {result["address"]: result for result in response.json()["results"]}
        prefix = results["2001:db8:100:200::"]
        self.assertEqual((prefix["kind"], prefix["prefix_length"]), ("delegated-prefix", 56))
        self.assertEqual(prefix["expiration"], {"infinite": True, "expires_at": None})
        self.assertEqual(results["2001:db8:1::11"]["state"], "registered")
        self.assertTrue(response.json()["complete"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class ReservationLeaseRelationshipTest(_ViewTestBase):
    """Only a Current Lease is a live relationship; "No Lease" needs a complete observation."""

    def _rows(self, family: int, reservation: dict, responses: dict):
        subnets = _SUBNETS4 if family == 4 else _SUBNETS6
        registry = {
            **_catalogue_responses_for_subnets(family, subnets),
            "reservation-get-page": _res_page([reservation]),
            **responses,
        }
        url = reverse(f"plugins:netbox_kea:server_reservations{family}", args=[self.server.pk])
        with stub_kea(registry):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response, response.context["table"].data.data

    def test_a_registered_dhcpv6_address_is_an_active_lease(self):
        registered = lease_record("2001:db8:1::11", state=4)
        reservation = {"subnet-id": 10, "duid": "00:01:02:03:04:05", "ip-addresses": ["2001:db8:1::11"]}
        # Kea's assigned-state query does not return a registered lease; only the whole Subnet does.
        response, rows = self._rows(
            6,
            reservation,
            {"lease6-get-by-state": {"result": 3}, "lease6-get-all": _leases_per_subnet({10: [registered]})},
        )

        self.assertIs(rows[0]["has_active_lease"], True)
        self.assertContains(response, "Active Lease")

    def test_an_expired_assigned_lease_is_not_an_active_lease(self):
        expired = lease_record("192.0.2.10", cltt=int(time.time()) - 7200, valid_lft=3600)
        reservation = {"subnet-id": 10, "hw-address": "aa:bb:cc:00:00:10", "ip-address": "192.0.2.10"}
        response, rows = self._rows(4, reservation, {"lease4-get-by-state": _leases_per_subnet({10: [expired]})})

        self.assertIs(rows[0]["has_active_lease"], False)
        self.assertContains(response, "No Lease")

    def test_an_infinite_lease_is_an_active_lease(self):
        infinite = lease_record("192.0.2.10", valid_lft=0xFFFFFFFF, cltt=1)
        reservation = {"subnet-id": 10, "hw-address": "aa:bb:cc:00:00:10", "ip-address": "192.0.2.10"}
        _response, rows = self._rows(4, reservation, {"lease4-get-by-state": _leases_per_subnet({10: [infinite]})})

        self.assertIs(rows[0]["has_active_lease"], True)

    @override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=2))
    def test_a_subnet_read_over_the_cap_is_unknown_not_no_lease(self):
        inactive = [
            lease_record("2001:db8:1::31", state=1, duid="00:00:00", drop=("hw-address",)),
            lease_record("2001:db8:1::32", state=3),
            lease_record("2001:db8:1::33", state=2),
        ]
        reservation = {"subnet-id": 10, "duid": "00:01:02:03:04:05", "ip-addresses": ["2001:db8:1::20"]}
        # Kea counts only assigned addresses and prefixes, so the measured Subnet looks small.
        response, rows = self._rows(
            6,
            reservation,
            {
                "stat-lease6-get": _subnet_stats(6, 10, assigned=1),
                "lease6-get-all": _leases_per_subnet({10: inactive}),
            },
        )

        self.assertIsNone(rows[0]["has_active_lease"])
        self.assertNotContains(response, "No Lease")

    def test_a_lease_identifier_that_no_reservation_can_hold_makes_the_relationship_unknown(self):
        lease = lease_record("192.0.2.60", hw_address="", client_id=_LONG_CLIENT_ID)
        reservation = {"subnet-id": 10, "hw-address": "aa:bb:cc:00:00:99", "ip-address": "192.0.2.50"}
        response, rows = self._rows(4, reservation, {"lease4-get-by-state": _leases_per_subnet({10: [lease]})})

        self.assertIsNone(rows[0]["has_active_lease"])
        self.assertNotContains(response, "No Lease")
        self.assertContains(response, "Lease Unknown")

    def test_a_partial_observation_shows_an_unknown_relationship_not_no_lease(self):
        malformed = lease_record("192.0.2.20", hostname=["private"])
        reservation = {"subnet-id": 10, "hw-address": "aa:bb:cc:00:00:10", "ip-address": "192.0.2.10"}
        response, rows = self._rows(4, reservation, {"lease4-get-by-state": _leases_per_subnet({10: [malformed]})})

        self.assertIsNone(rows[0]["has_active_lease"])
        self.assertNotContains(response, "No Lease")
        self.assertContains(response, "Lease Unknown")
        self.assertContains(response, "a missing Lease cannot be confirmed")


@override_settings(PLUGINS_CONFIG=plugins_config())
class LeaseReconciliationTest(_ViewTestBase):
    """The lease phase reads one typed Snapshot; an incomplete one removes no stale link."""

    def test_a_malformed_record_blocks_stale_cleanup_and_keeps_its_valid_sibling(self):
        server = _server("owner")
        _reconcile(server, [_lease("10.0.0.9")], sources=("lease",))

        report = _reconcile(server, [_lease("10.0.0.5"), _lease("10.0.0.6", state="bogus")], sources=("lease",))

        self.assertEqual((report.errors, report.incomplete), (1, {"lease"}))
        self.assertEqual(set(_links(_row("10.0.0.9"))), {"owner"})
        self.assertEqual(set(_links(_row("10.0.0.5"))), {"owner"})
        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.6").exists())

    def test_a_subnet_id_beyond_kea_s_range_is_excluded_also_without_a_catalogue(self):
        server = _server("owner")
        records = [_lease("10.0.0.8", **{"subnet-id": 4_294_967_295}), _lease()]
        with _kea(records):
            report = reconcile(server, 4, [LeasePhase(max_leases=None, subnet_prefix_lengths=None)])

        self.assertEqual((report.errors, report.incomplete), (1, {"lease"}))
        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.8").exists())
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.5").exists())

    def test_a_delegated_prefix_still_reports_its_base_address_to_the_lease_source(self):
        server = _make_db_server(name="pd-owner", ca_url="https://pd.example.com", dhcp4=False)
        prefix = lease_record("2001:db8:1:100::", type="IA_PD", prefix_len=56, subnet_id=10)
        responses = {
            **_catalogue_responses_for_subnets(6, _SUBNETS6),
            "lease6-get-page": lease_pages([prefix]),
        }
        with stub_kea(responses):
            report = reconcile(server, 6, [LeasePhase(max_leases=None, subnet_prefix_lengths={10: 64})])

        self.assertEqual((report.created, report.errors, report.incomplete), (1, 0, set()))
        self.assertEqual(set(_links(_row("2001:db8:1:100::"))), {"pd-owner"})

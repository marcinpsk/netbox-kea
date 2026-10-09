# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Lease reads share typed facts, scope and completeness: real requests, views, REST and the real KeaClient.

Only the Kea HTTP boundary is stubbed. Records come from the lease replies recorded from a real Kea 3.2.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import requests
from django.contrib import messages as django_messages
from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse
from extras.models import JournalEntry
from ipam.models import IPAddress
from rest_framework.test import APIClient

from netbox_kea import signals
from netbox_kea.ipam_reconciliation import reconcile

from .kea_stub import (
    LeaseDaemon,
    _catalogue_responses_for_subnets,
    _http_response,
    _leases_per_subnet,
    _raw_http_response,
    _res_page,
    _subnet_stats,
    lease_page,
    lease_pages,
    lease_record,
    lease_reply,
    queued,
    shown_token,
    stub_kea,
)
from .test_ipam_reconciliation import _kea, _lease, _links, _reconcile, _row, _server
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase, lease_phase, plugins_config

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
        self.assertContains(response, "<li>leases[1] (ip-address): The lease address is not valid.</li>", html=True)
        self.assertContains(response, "<li>leases[2] (state): A lease field has the wrong type.</li>", html=True)
        self.assertEqual(response.context["lease_notice"].level, django_messages.WARNING)
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

    def test_address_and_delegated_prefix_rows_offer_edit_and_sync(self):
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
            reverse("plugins:netbox_kea:server_lease6_edit", args=[self.server.pk, "2001:db8:1::10"])
            + f"?{urlencode({'return_url': f'{self._url(6)}?by=subnet_id&q=10'})}",
        )
        self.assertEqual(
            rows["delegated-prefix"]["edit_url"],
            reverse("plugins:netbox_kea:server_lease6_edit", args=[self.server.pk, "2001:db8:100:100::/56"])
            + f"?{urlencode({'return_url': f'{self._url(6)}?by=subnet_id&q=10'})}",
        )
        self.assertTrue(rows["address"].get("sync_url"))
        # Sync claims a delegated prefix as a Prefix.
        self.assertEqual(rows["delegated-prefix"].get("sync_url"), rows["address"]["sync_url"])

    def test_the_sync_offer_uses_the_evaluation_time_of_the_observation(self):
        class _TwoHoursLater(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=2)

        responses = {
            **_catalogue_responses_for_subnets(4, _SUBNETS4),
            "lease4-get-all": lease_reply(lease_record("192.0.2.10", subnet_id=10, valid_lft=3600)),
            "reservation-get": {"result": 3},
        }
        # mock-ok: the clock is the boundary; only the view module reads it later than the observation.
        with stub_kea(responses), patch("netbox_kea.views.leases.datetime", _TwoHoursLater):
            response = self.client.get(self._url(4), {"by": "subnet_id", "q": "10"}, HTTP_HX_REQUEST="true")

        (row,) = (row.record for row in response.context["table"].rows)
        self.assertEqual(row["state_label"], "Active")
        self.assertEqual(row["expiry_class"], "")
        self.assertTrue(row.get("sync_url"))

    def test_only_an_address_lease_row_links_its_netbox_ip_address(self):
        # NetBox holds an IP Address at the network address of the prefix, which is not the delegated prefix.
        from ipam.models import IPAddress

        address = IPAddress.objects.create(address="2001:db8:1::10/64")
        IPAddress.objects.create(address="2001:db8:100:100::/128")
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
        self.assertEqual(rows["address"].get("netbox_ip_url"), address.get_absolute_url())
        self.assertIsNone(rows["delegated-prefix"].get("netbox_ip_url"))
        self.assertContains(response, " Synced</a>", count=1)

    def test_a_delegated_prefix_row_shows_its_length_and_searches_prefixes(self):
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

        rows = {row.record["kind"]: row for row in response.context["table"].rows}
        self.assertEqual(rows["delegated-prefix"].get_cell("ip_address"), "2001:db8:100:100::/56")
        self.assertEqual(rows["address"].get_cell("ip_address"), "2001:db8:1::10")
        ip_search = f'href="{reverse("ipam:ipaddress_list")}?address='
        self.assertContains(response, f"{ip_search}2001:db8:1::10", count=1)
        self.assertNotContains(response, f"{ip_search}2001:db8:100:100::")
        self.assertContains(response, f'href="{reverse("ipam:prefix_list")}?prefix=2001:db8:100:100::/56"', count=1)
        self.assertContains(response, 'title="Search prefixes"', count=1)

    def test_a_delegated_prefix_row_offers_a_prefix_reservation(self):
        # A prefix inside the Subnet CIDR still prefills the prefix field, not an address.
        records = [
            lease_record("2001:db8:1::10", subnet_id=10),
            lease_record("2001:db8:1:0:1::", type="IA_PD", prefix_len=80, subnet_id=10),
        ]
        responses = {
            **_catalogue_responses_for_subnets(6, _SUBNETS6),
            "lease6-get-all": lease_reply(*records),
            "reservation-get": {"result": 3},
        }
        with stub_kea(responses):
            response = self.client.get(self._url(6), {"by": "subnet_id", "q": "10"}, HTTP_HX_REQUEST="true")

        rows = {row.record["kind"]: row.record for row in response.context["table"].rows}
        self.assertIn("ip_addresses=2001%3Adb8%3A1%3A%3A10", rows["address"]["create_reservation_url"])
        link = rows["delegated-prefix"]["create_reservation_url"]
        self.assertIn("prefixes=2001%3Adb8%3A1%3A0%3A1%3A%3A%2F80", link)
        self.assertNotIn("ip_addresses", link)

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
class LeaseDeleteTest(_ViewTestBase):
    """Bulk delete removes the selected allocation: real page, form, view and KeaClient; only HTTP is stubbed."""

    def _delete_url(self, family: int = 6) -> str:
        return reverse(f"plugins:netbox_kea:server_leases{family}_delete", args=[self.server.pk])

    def test_a_selected_delegated_prefix_is_deleted_as_a_prefix(self):
        records = [
            lease_record("2001:db8:1::10", subnet_id=10),
            lease_record("2001:db8:100:100::", type="IA_PD", prefix_len=56, subnet_id=10),
        ]
        daemon = LeaseDaemon(6, *records)
        responses = {
            **_catalogue_responses_for_subnets(6, _SUBNETS6),
            "lease6-get-all": lease_reply(*records),
            "reservation-get": {"result": 3},
        }
        with stub_kea(responses):
            page = self.client.get(
                reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk]),
                {"by": "subnet_id", "q": "10"},
                HTTP_HX_REQUEST="true",
            )
        selected = [html.unescape(value) for value in re.findall(r'name="pk" value="([^"]+)"', page.content.decode())]
        self.assertEqual(
            sorted(json.loads(value)["identity"]["kind"] for value in selected), ["address", "delegated-prefix"]
        )

        with stub_kea(daemon.responses()) as kea:
            response = self.client.post(self._delete_url(), {"pk": selected, "_confirm": "1"})

        self.assertEqual(daemon.leases, {})
        self.assertIn(
            {"ip-address": "2001:db8:100:100::", "type": "IA_PD"}, [b["arguments"] for b in kea.bodies("lease6-del")]
        )
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(messages, ["Deleted 2 DHCPv6 lease(s)."])

    def test_a_lease_kea_does_not_hold_is_not_reported_deleted(self):
        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        selected = shown_token(lease_record("2001:db8:1::10", subnet_id=10))
        signals.leases_deleted.connect(handler)
        try:
            with stub_kea(LeaseDaemon(6).responses()) as kea:
                response = self.client.post(self._delete_url(), {"pk": [selected], "_confirm": "1"})
        finally:
            signals.leases_deleted.disconnect(handler)

        self.assertEqual(kea.commands(), ["lease6-get"])
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(messages, ["Lease 2001:db8:1::10 was not found in Kea; nothing was deleted."])
        self.assertEqual(received, [])
        self.assertFalse(JournalEntry.objects.filter(assigned_object_id=self.server.pk).exists())

    def test_an_empty_delete_reply_fails_that_lease_and_the_rest_continue(self):
        records = [lease_record(address, subnet_id=10) for address in ("2001:db8:1::10", "2001:db8:1::11")]
        daemon = LeaseDaemon(6, *records)
        responses = daemon.responses()
        with stub_kea({**responses, "lease6-del": queued([], responses["lease6-del"])}):
            response = self.client.post(
                self._delete_url(), {"pk": [shown_token(record) for record in records], "_confirm": "1"}
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(daemon.leases), [("IA_NA", "2001:db8:1::10")])
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(
            messages,
            [
                "Error deleting lease 2001:db8:1::10: see server logs for details.",
                "Deleted 1 DHCPv6 lease(s).",
                "Failed to delete 1 lease(s). See above for details.",
            ],
        )

    def test_a_dhcpv4_delete_refuses_a_dhcpv6_selection_before_kea(self):
        selected = shown_token(lease_record("2001:db8:100:100::", type="IA_PD", prefix_len=56, subnet_id=10))
        with stub_kea({}) as kea:
            response = self.client.post(self._delete_url(4), {"pk": [selected], "_confirm": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands(), [])


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

    def test_every_malformed_kea_reply_is_a_bad_gateway(self):
        cases = {
            "stat-lease4-get without a result set": (
                {"subnet_id": "10"},
                {"stat-lease4-get": {"result": 0, "arguments": {}}},
            ),
            "reply entry without a result": ({"ip_address": "192.0.2.10"}, {"lease4-get": {"text": "no result"}}),
            "reply is not a list": ({"ip_address": "192.0.2.10"}, {"lease4-get": _http_response({"result": 0})}),
            "reply is not JSON": ({"ip_address": "192.0.2.10"}, {"lease4-get": _raw_http_response(b"<html>")}),
            "lease page without a record list": (
                {"subnet_id": "10"},
                {
                    "stat-lease4-get": _subnet_stats(4, 10, assigned=1),
                    "lease4-get-all": {"result": 0, "arguments": {"leases": "x"}},
                },
            ),
        }
        for name, (params, responses) in cases.items():
            with (
                self.subTest(name),
                override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=100)),
            ):
                response = self._get(4, params, responses)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json(), {"detail": "An internal error occurred"})

    def test_every_kea_transport_failure_is_a_bad_gateway(self):
        cases = {
            "connection refused": requests.ConnectionError("refused"),
            "timeout": requests.Timeout("slow"),
            "HTTP error status": _http_response({"result": 1}, status=503),
        }
        for name, reply in cases.items():
            with self.subTest(name):
                response = self._get(4, {"ip_address": "192.0.2.10"}, {"lease4-get": reply})
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json(), {"detail": "Could not connect to Kea server."})

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
            report = reconcile(server, 4, [lease_phase(server, 4, None)])

        self.assertEqual((report.errors, report.incomplete), (1, {"lease"}))
        self.assertFalse(IPAddress.objects.filter(address__net_host="10.0.0.8").exists())
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.5").exists())

    def test_a_delegated_prefix_reports_no_ip_address_to_the_address_lease_source(self):
        server = _make_db_server(name="pd-owner", ca_url="https://pd.example.com", dhcp4=False)
        prefix = lease_record("2001:db8:1:100::", type="IA_PD", prefix_len=56, subnet_id=10)
        responses = {
            **_catalogue_responses_for_subnets(6, _SUBNETS6),
            "lease6-get-page": lease_pages([prefix]),
        }
        with stub_kea(responses):
            report = reconcile(server, 6, [lease_phase(server, 6, {10: 64})])

        self.assertEqual((report.created, report.errors, report.incomplete), (0, 0, set()))
        self.assertFalse(IPAddress.objects.exists())

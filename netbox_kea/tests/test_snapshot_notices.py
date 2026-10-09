# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The one Snapshot notice rule of ADR 0003, from the rule to the rendered page."""

from typing import Any, cast

import requests
from django.contrib import messages as django_messages
from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse

from netbox_kea import server_configuration, subnet_catalogue
from netbox_kea.kea import LeaseQueryPreflightUnavailable
from netbox_kea.server_configuration import Diagnostic
from netbox_kea.views.notices import HEADLINES, Notice, ServerNotices, load_snapshot, notice, show_notices

from .kea_stub import (
    _catalogue_responses,
    _res_get,
    _res_page,
    _subnet_list,
    complete_lease,
    lease_page,
    queued,
    stub_kea,
)
from .utils import _ViewTestBase, plugins_config

_IDENTITY_UNAVAILABLE = "Kea subnet identity facts are unavailable."
_INVALID_POOLS = "Kea returned a non-list Pool collection."


def _config4(*subnets: dict, **service: object) -> dict:
    return {
        "result": 0,
        "arguments": {"Dhcp4": {"subnet4": list(subnets), "shared-networks": [], **service}, "hash": "h1"},
    }


def _page_messages(response) -> list[tuple[int, str]]:
    return [(message.level, str(message)) for message in get_messages(response.wsgi_request)]


_SUBNET = {"id": 1, "subnet": "198.18.0.0/24"}
_COMPLETE = {"subnet4-list": _subnet_list(4, [_SUBNET]), "config-get": _config4(_SUBNET)}
_UNAVAILABLE = {
    "subnet4-list": requests.ConnectionError("identity unavailable"),
    "config-get": requests.ConnectionError("configuration unavailable"),
}
_INCOMPLETE = {
    "subnet4-list": _subnet_list(4, [_SUBNET]),
    "config-get": _config4({**_SUBNET, "pools": "invalid"}),
}


class TestConfigurationSnapshotNotices(_ViewTestBase):
    """The Catalogue and the Server Configuration Snapshot: unavailable, incomplete and complete."""

    def _catalogue(self, responses: dict):
        # The display read caches a usable Catalogue, so each case starts a new cache generation.
        server_configuration.invalidate(self.server, 4)
        with stub_kea(responses):
            return subnet_catalogue.display(self.server, 4)

    def _configuration(self, responses: dict):
        with stub_kea(responses):
            return server_configuration.for_verification(self.server, 4)

    def test_each_state_of_each_kind(self):
        cases = (
            ("catalogue", self._catalogue, _UNAVAILABLE, django_messages.ERROR, _IDENTITY_UNAVAILABLE),
            ("catalogue", self._catalogue, _INCOMPLETE, django_messages.WARNING, _INVALID_POOLS),
            ("configuration", self._configuration, _UNAVAILABLE, django_messages.ERROR, None),
            ("configuration", self._configuration, _INCOMPLETE, django_messages.WARNING, _INVALID_POOLS),
        )
        for kind, read, responses, level, message in cases:
            with self.subTest(kind=kind, level=level):
                result = notice(read(responses))
                self.assertIsNotNone(result)
                self.assertEqual((result.kind, result.level), (kind, level))
                if level == django_messages.ERROR:
                    self.assertEqual(result.lines[0], HEADLINES[kind])
                else:
                    self.assertEqual(result.headline, "")
                    self.assertNotIn(HEADLINES[kind], result.lines)
                self.assertIn("Kea configuration facts are unavailable." if message is None else message, result.lines)
        for read in (self._catalogue, self._configuration):
            with self.subTest(read=read.__name__, state="complete"):
                self.assertIsNone(notice(read(_COMPLETE)))

    def test_another_type_is_not_a_snapshot(self):
        with self.assertRaisesRegex(TypeError, "Diagnostic is not a Snapshot"):
            notice(cast(Any, Diagnostic("code", "message", "source")))

    def test_the_configuration_snapshot_answers_the_catalogue_predicate(self):
        self.assertTrue(self._configuration(_UNAVAILABLE).unavailable)
        self.assertFalse(self._configuration(_INCOMPLETE).unavailable)


class TestRecordSnapshotNotices(_ViewTestBase):
    """The Reservation and Lease Snapshot: a raised read goes through the loader, the rest through the rule."""

    def _reservations(self, page: object):
        responses = {**_catalogue_responses(4, 20, "198.18.0.0/24"), "reservation-get-page": page}
        with stub_kea(responses):
            client = self.server.get_client(version=4)
            catalogue = subnet_catalogue.display(self.server, 4)
            return load_snapshot(self.server, "reservation", lambda: client.reservation_page(4, catalogue, limit=2))

    def _leases(self, reply: object):
        with stub_kea({"lease4-get-page": reply}):
            client = self.server.get_client(version=4)
            return load_snapshot(
                self.server, "lease", lambda: client.lease_get_page(4, limit=1, server_id=self.server.pk)
            )

    def test_a_reservation_record_that_cannot_be_read_is_a_warning(self):
        snapshot = self._reservations(_res_page([{"subnet-id": 20, "remote-id": "not native"}]))
        result = notice(snapshot)
        self.assertEqual(
            (result.kind, result.level, len(result.diagnostics)), ("reservation", django_messages.WARNING, 1)
        )
        # The record list of the page shows the diagnostics with their source position.
        self.assertEqual(result.lines, ())

    def test_a_reservation_page_with_more_to_come_gives_no_notice(self):
        hosts = [{"subnet-id": 20, "hw-address": f"aa:bb:cc:dd:ee:0{index}"} for index in range(2)]
        snapshot = self._reservations(_res_page(hosts, next_from=2, next_source=1))
        self.assertIsNotNone(snapshot.next_cursor)
        self.assertIsNone(notice(snapshot))

    def test_a_failed_reservation_read_is_an_unavailable_notice(self):
        for page, unsupported in (
            (requests.ConnectionError("unreachable"), False),
            ({"result": 1, "text": "database error"}, False),
            (["not", "entries"], False),
            ({"result": 2, "text": "unknown command"}, True),
        ):
            with self.subTest(page=page):
                result = self._reservations(page)
                self.assertEqual(result, Notice("reservation", django_messages.ERROR, unsupported_command=unsupported))
                self.assertEqual(result.lines, (HEADLINES["reservation"],))

    def test_a_lease_record_that_cannot_be_read_is_a_warning(self):
        broken = complete_lease({"ip-address": "198.18.0.10", "valid-lft": -1})
        with stub_kea({"lease4-get-by-hostname": {"result": 0, "arguments": {"leases": [broken]}}}):
            client = self.server.get_client(version=4)
            snapshot = client.lease_search(4, "hostname", "host.example.invalid", server_id=self.server.pk)
        result = notice(snapshot)
        self.assertEqual((result.kind, result.level, len(result.diagnostics)), ("lease", django_messages.WARNING, 1))
        self.assertEqual(result.reasons, "A lease field is outside the range that Kea permits.")

    def test_a_lease_page_with_a_continuation_and_no_diagnostics_gives_no_notice(self):
        snapshot = self._leases(lease_page(complete_lease({"ip-address": "198.18.0.10"})))
        self.assertEqual((snapshot.coverage, str(snapshot.next_cursor)), ("page", "198.18.0.10"))
        self.assertIsNone(notice(snapshot))

    def test_a_complete_lease_snapshot_gives_no_notice(self):
        with stub_kea({"lease4-get-by-hostname": {"result": 3, "arguments": {"leases": []}}}):
            client = self.server.get_client(version=4)
            snapshot = client.lease_search(4, "hostname", "host.example.invalid", server_id=self.server.pk)
        self.assertTrue(snapshot.complete)
        self.assertIsNone(notice(snapshot))

    def test_a_failed_lease_read_is_an_unavailable_notice(self):
        for reply in (requests.ReadTimeout("timed out"), {"result": 1, "text": "error"}, {"result": 0}):
            with self.subTest(reply=reply):
                self.assertEqual(self._leases(reply), Notice("lease", django_messages.ERROR))

    @override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=100))
    def test_a_refused_lease_query_and_a_value_error_stay_outside_the_rule(self):
        with stub_kea({"stat-lease4-get": {"result": 2, "text": "unknown command"}}):
            client = self.server.get_client(version=4)
            with self.assertRaises(LeaseQueryPreflightUnavailable):
                load_snapshot(
                    self.server, "lease", lambda: client.lease_search(4, "subnet_id", 12, server_id=self.server.pk)
                )
            with self.assertRaises(ValueError):
                load_snapshot(self.server, "lease", lambda: client.lease_get_page(4, limit=0, server_id=1))


class TestOneServerReadsSeveralSnapshots(_ViewTestBase):
    """Each distinct line shows once, at the level of the worst Snapshot of the Server that reported it."""

    def test_a_shared_line_takes_the_worst_level(self):
        shared = "Kea configuration facts are unavailable."
        identity_only = {**_UNAVAILABLE, "subnet4-list": _subnet_list(4, [_SUBNET])}
        with stub_kea(identity_only):
            catalogue = notice(subnet_catalogue.display(self.server, 4))
            configuration = notice(server_configuration.for_verification(self.server, 4))
        # The Catalogue keeps its verified Subnets, so it reports the failed configuration read as a warning.
        self.assertEqual(catalogue.level, django_messages.WARNING)
        self.assertIn(shared, catalogue.lines)
        request = self._make_request()
        show_notices(request, catalogue, None, configuration)
        shown = [(message.level, message.message) for message in request._messages]
        self.assertEqual([text for _level, text in shown].count(shared), 1)
        self.assertIn((django_messages.ERROR, shared), shown)
        self.assertEqual(shown[-1], (django_messages.ERROR, HEADLINES["configuration"]))

    def test_two_servers_that_report_one_line_keep_it_each(self):
        with stub_kea(_INCOMPLETE):
            incomplete = notice(server_configuration.for_verification(self.server, 4))
        other = type(self.server).objects.create(name="other-kea", ca_url="https://other.example.com")
        lists = ServerNotices()
        lists.add(self.server, incomplete)
        lists.add(other, incomplete)
        self.assertEqual(lists.warnings, [(self.server.name, _INVALID_POOLS), (other.name, _INVALID_POOLS)])
        self.assertEqual(lists.errors, [])


class TestSubnetEditNotices(_ViewTestBase):
    """The Subnet edit page reads the Subnet Catalogue and the Server Configuration of one Server."""

    def test_an_unavailable_catalogue_next_to_an_available_configuration_is_an_error(self):
        subnet = {"id": 42, "subnet": "10.0.0.0/24", "pools": [], "option-data": []}
        with stub_kea(
            {
                "subnet4-list": requests.ConnectionError("identity unavailable"),
                # The Catalogue reads the configuration first and fails; the page then reads it live.
                "config-get": queued(requests.ConnectionError("configuration unavailable"), _config4(subnet)),
                "stat-lease4-get": {"result": 2, "text": "unknown command"},
            }
        ):
            response = self.client.get(reverse("plugins:netbox_kea:server_subnet4_edit", args=[self.server.pk, 42]))
        self.assertEqual(response.status_code, 200)
        self.assertIn((django_messages.ERROR, _IDENTITY_UNAVAILABLE), _page_messages(response))
        self.assertNotIn((django_messages.WARNING, _IDENTITY_UNAVAILABLE), _page_messages(response))


class TestStatusPageNotices(_ViewTestBase):
    """The status page reads the Server Configuration of both families of one Server."""

    def test_one_failure_of_both_families_shows_once(self):
        failure = requests.ConnectionError("unreachable")
        with stub_kea({"status-get": failure, "version-get": failure, "config-get": failure}):
            response = self.client.get(reverse("plugins:netbox_kea:server_status", args=[self.server.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            _page_messages(response),
            [
                (django_messages.ERROR, HEADLINES["configuration"]),
                (django_messages.ERROR, "Kea configuration facts are unavailable."),
            ],
        )


class TestLeaseSearchFormNotice(_ViewTestBase):
    """The lease search form shows the Catalogue Notice inline, also in the htmx partial."""

    def test_an_unavailable_catalogue_is_an_inline_error(self):
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea({**_UNAVAILABLE, "lease4-get-page": lease_page()}):
            page = self.client.get(url)
            partial = self.client.get(url, {"q": "", "by": ""}, HTTP_HX_REQUEST="true")
        for response in (page, partial):
            with self.subTest(htmx=response is partial):
                self.assertEqual(response.status_code, 200)
                # One alert holds the headline, then each diagnostic as a list item.
                self.assertContains(response, 'class="alert alert-danger py-2 px-3 mb-3 small"', count=1)
                self.assertContains(response, f"<strong>{HEADLINES['catalogue']}</strong>", html=True)
                self.assertContains(response, f"<li>{_IDENTITY_UNAVAILABLE}</li>", html=True)

    def test_an_incomplete_catalogue_is_an_inline_warning_without_a_headline(self):
        server_configuration.invalidate(self.server, 4)
        with stub_kea(_INCOMPLETE):
            response = self.client.get(reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk]))
        self.assertContains(response, 'class="alert alert-warning py-2 px-3 mb-3 small"', count=1)
        self.assertContains(response, f"<li>{_INVALID_POOLS}</li>", html=True)
        self.assertNotContains(response, HEADLINES["catalogue"])


class TestRecordPagesShowAFailedReadThroughTheLoader(_ViewTestBase):
    """A failed Reservation or Lease read shows the unavailable Notice in the channel of its page."""

    def test_the_reservation_page_shows_the_headline_once_in_its_record_list(self):
        responses = {**_catalogue_responses(4, 20, "198.18.0.0/24"), "reservation-get-page": ["not", "entries"]}
        with stub_kea(responses):
            response = self.client.get(reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_page_messages(response), [])
        self.assertContains(
            response, f'<div class="alert alert-danger" role="alert">{HEADLINES["reservation"]}</div>', html=True
        )
        # A failed read is not an incomplete Snapshot, so the page gives it no second headline.
        self.assertNotContains(response, "Snapshot is incomplete")
        self.assertNotContains(response, "This bounded Snapshot is complete")

    def test_the_lease_search_partial_shows_the_headline_inline(self):
        with stub_kea({**_COMPLETE, "lease4-get-by-hw-address": requests.ConnectionError("unreachable")}):
            response = self.client.get(
                reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk]),
                {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, f'<div class="alert alert-danger" role="alert">{HEADLINES["lease"]}</div>', html=True
        )
        self.assertNotContains(response, 'id="lease-delete-form"')

    @override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=100))
    def test_a_refused_lease_query_keeps_its_own_message(self):
        with stub_kea({**_COMPLETE, "stat-lease4-get": {"result": 2, "text": "unknown command"}}):
            response = self.client.get(
                reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk]),
                {"by": "subnet_id", "q": "1"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Load the stat_cmds hook or disable the guard explicitly.")
        self.assertNotContains(response, HEADLINES["lease"])


class TestCombinedPagesKeepANoticePerServer(_ViewTestBase):
    """A combined page fills its per-Server lists from the Notices and never merges two Servers."""

    def test_two_servers_that_report_one_diagnostic_show_it_under_each(self):
        other = type(self.server).objects.create(name="other-kea", ca_url="https://other.example.com", dhcp4=True)
        with stub_kea({**_INCOMPLETE, "stat-lease4-get": {"result": 2, "text": "unknown command"}}):
            response = self.client.get(reverse("plugins:netbox_kea:combined_subnets4"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["errors"], [])
        self.assertCountEqual(
            response.context["warnings"], [(self.server.name, _INVALID_POOLS), (other.name, _INVALID_POOLS)]
        )
        self.assertContains(response, _INVALID_POOLS, count=2)

    def test_a_failed_reservation_read_is_an_error_of_its_server(self):
        responses = {**_catalogue_responses(4, 20, "198.18.0.0/24"), "reservation-get-page": {"result": 1}}
        with stub_kea(responses):
            response = self.client.get(reverse("plugins:netbox_kea:combined_reservations4"), {"server": self.server.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["errors"], [(self.server.name, HEADLINES["reservation"])])
        self.assertContains(response, '<div class="alert alert-danger mt-3">')

    def test_a_failed_lease_read_is_an_error_and_a_refused_query_a_warning(self):
        url = reverse("plugins:netbox_kea:combined_leases4")
        query = {"q": "aa:bb:cc:dd:ee:ff", "by": "hw", "server": self.server.pk}
        with stub_kea({"lease4-get-by-hw-address": requests.ConnectionError("unreachable")}):
            failed = self.client.get(url, query)
        self.assertEqual(failed.context["errors"], [(self.server.name, HEADLINES["lease"])])
        self.assertEqual(failed.context["warnings"], [])
        self.assertContains(failed, '<div class="alert alert-danger">')
        with (
            override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=100)),
            stub_kea({"stat-lease4-get": {"result": 2, "text": "unknown command"}}),
        ):
            refused = self.client.get(url, {**query, "q": "1", "by": "subnet_id"})
        self.assertEqual(refused.context["errors"], [])
        self.assertEqual(len(refused.context["warnings"]), 1)
        self.assertIn("stat_cmds", refused.context["warnings"][0][1])

    def test_a_client_that_cannot_be_built_is_an_error_with_the_headline_of_its_kind(self):
        type(self.server).objects.filter(pk=self.server.pk).update(
            client_key_path="/tls/client.key", client_cert_path=""
        )
        with stub_kea({}) as kea:
            reservations = self.client.get(
                reverse("plugins:netbox_kea:combined_reservations4"), {"server": self.server.pk}
            )
            leases = self.client.get(
                reverse("plugins:netbox_kea:combined_leases4"),
                {"q": "aa:bb:cc:dd:ee:ff", "by": "hw", "server": self.server.pk},
            )
        self.assertEqual(kea.commands(), [])
        self.assertEqual(reservations.context["errors"], [(self.server.name, HEADLINES["reservation"])])
        self.assertEqual(leases.context["errors"], [(self.server.name, HEADLINES["lease"])])


class TestReservationEditNotice(_ViewTestBase):
    """The Reservation edit page reads the displayed Catalogue for its hostname suffix, so it shows its Notice."""

    def _edit(self, responses: dict):
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        current = {"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff"}
        with stub_kea({**responses, "reservation-get": _res_get(current)}):
            return self.client.get(url, {"identifier_type": "hw-address", "identifier": "aa:bb:cc:dd:ee:ff"})

    def test_an_incomplete_catalogue_is_a_warning_on_the_form(self):
        identity_only = {
            **_catalogue_responses(4, 20, "198.18.0.0/24"),
            "config-get": requests.ConnectionError("configuration unavailable"),
        }
        response = self._edit(identity_only)
        self.assertEqual(response.status_code, 200)
        self.assertIn((django_messages.WARNING, "Kea configuration facts are unavailable."), _page_messages(response))

    def test_an_unavailable_catalogue_is_an_error_before_the_failed_target_read(self):
        failure = requests.ConnectionError("unreachable")
        response = self._edit({"subnet4-list": failure, "config-get": failure, "list-commands": failure})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(_page_messages(response)[0], (django_messages.ERROR, HEADLINES["catalogue"]))
        self.assertIn(
            (django_messages.ERROR, "The Reservation could not be loaded. See server logs."), _page_messages(response)
        )


_CONFIGURATION_UNAVAILABLE = "Kea configuration facts are unavailable."
_IDENTITY_ONLY = {"subnet4-list": _subnet_list(4, [_SUBNET]), "config-get": requests.ConnectionError("unavailable")}


class TestNoticeGapsFromReview(_ViewTestBase):
    """Pages that read a Snapshot show its Notice once, in a channel that renders it."""

    def test_a_subnets_table_refresh_shows_the_notice_out_of_band_and_queues_no_message(self):
        # NetBox renders htmx/table.html without messages, so a queued one would surface on the next page.
        url = reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk])
        server_configuration.invalidate(self.server, 4)
        with stub_kea(_UNAVAILABLE):
            failed = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(_page_messages(failed), [])
        self.assertContains(failed, '<div id="subnet-notice" hx-swap-oob="true">')
        self.assertContains(failed, f"<strong>{HEADLINES['catalogue']}</strong>", html=True)
        self.assertContains(failed, f"<li>{_IDENTITY_UNAVAILABLE}</li>", html=True)
        # A later refresh with a complete Catalogue clears the slot.
        server_configuration.invalidate(self.server, 4)
        with stub_kea({**_COMPLETE, "stat-lease4-get": {"result": 2, "text": "unknown command"}}):
            recovered = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertContains(recovered, '<div id="subnet-notice" hx-swap-oob="true"></div>', html=True)

    def test_the_subnets_page_has_an_empty_notice_slot_and_shows_the_notice_as_messages(self):
        server_configuration.invalidate(self.server, 4)
        with stub_kea(_UNAVAILABLE):
            response = self.client.get(reverse("plugins:netbox_kea:server_subnets4", args=[self.server.pk]))
        self.assertContains(response, '<div id="subnet-notice"></div>', html=True)
        self.assertIn((django_messages.ERROR, HEADLINES["catalogue"]), _page_messages(response))

    def test_the_pool_forms_warn_about_an_incomplete_catalogue(self):
        urls = (
            reverse("plugins:netbox_kea:server_subnet4_pool_add", args=[self.server.pk, 1]),
            reverse(
                "plugins:netbox_kea:server_subnet4_pool_delete", args=[self.server.pk, 1, "198.18.0.10-198.18.0.20"]
            ),
        )
        for url in urls:
            with self.subTest(url=url):
                server_configuration.invalidate(self.server, 4)
                with stub_kea(_IDENTITY_ONLY):
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn((django_messages.WARNING, _CONFIGURATION_UNAVAILABLE), _page_messages(response))

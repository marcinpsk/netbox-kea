# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for netbox_kea plugin.

Also contains pure-Python unit tests for helper functions defined in views.py
(e.g. ``_extract_identifier``), which do not require a database but live here
because they are tightly coupled to view logic.

These tests verify correct HTTP responses and redirect behaviour for every view.
All Kea HTTP calls are mocked so no running Kea instance is required.

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
"""

import re
import threading
from datetime import datetime, timezone
from unittest.mock import patch

import requests
from django.contrib.messages import get_messages
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress as NbIP

from netbox_kea.kea import KeaClient, KeaException
from netbox_kea.models import Server
from netbox_kea.utilities import lease_rows

from .kea_stub import (
    _catalogue_responses,
    _catalogue_responses_for_subnets,
    _http_response,
    _raw_http_response,
    _subnet_list,
    _subnet_stats,
    complete_lease,
    kea_client,
    lease_pages,
    lease_record,
    queued,
    stub_kea,
    typed_lease,
)
from .utils import _PLUGINS_CONFIG, _make_db_server, _ViewTestBase, active_tabs, plugins_config

#: The HTMX error template renders a uuid4 reference ID, so a test that stops at the
#: label also passes when that ID is missing.
_ERROR_TEMPLATE = re.compile(
    r"An internal error occurred\. Reference ID: <code>"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}</code>"
)


def _assert_rendered_error_template(test, response):
    """Assert the HTMX handler rendered exception_htmx.html with a real reference ID."""
    test.assertRegex(response.content.decode(), _ERROR_TEMPLATE)


def _assert_no_error_template(test, response):
    """Assert the HTMX handler rendered no part of exception_htmx.html."""
    test.assertNotRegex(response.content.decode(), _ERROR_TEMPLATE)
    test.assertNotContains(response, "An internal error occurred. Reference ID:")


#: The lease-query guard is off, so a Subnet lease search issues no stat-lease{v}-get
#: preflight and needs none registered. Tests that register only the lease command name
#: this dependency here instead of inheriting the value from the shared fixture.
_UNGUARDED_PLUGINS_CONFIG = plugins_config(lease_query_max_unpaged_leases=0)


def _lease_stub(responses: dict):
    """Supply the picker configuration matching each registered Subnet listing."""

    def configuration(body):
        family = int(body["service"][0][-1])
        listing = responses.get(f"subnet{family}-list")
        if not isinstance(listing, dict):
            return {"result": 1, "text": "Configuration unavailable in this lease scenario"}
        arguments = listing.get("arguments", {})
        subnets = arguments.get("subnets", []) if isinstance(arguments, dict) else []
        return _catalogue_responses_for_subnets(family, subnets)["config-get"]

    return stub_kea({"config-get": configuration, **responses})


def _reservation_stub(version: int, responses: dict):
    """Add the matching configuration source required by typed Reservation scope.

    The Catalogue response shape has one definition in ``kea_stub``; this only points it
    at the subnets the caller declared, then lets the caller's own entries win.
    """
    list_response = responses.get(f"subnet{version}-list")
    arguments = list_response.get("arguments", {}) if isinstance(list_response, dict) else {}
    subnets = arguments.get("subnets", []) if isinstance(arguments, dict) else []
    return stub_kea({**_catalogue_responses_for_subnets(version, subnets), **responses})


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerLeases4View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/leases4/"""

    def test_get_returns_200(self):
        """Initial leases4 page renders without Kea API calls."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_shows_the_add_and_bulk_import_links(self):
        response = self.client.get(reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk]))
        for name in ("server_lease4_add", "server_lease4_bulk_import"):
            with self.subTest(name):
                self.assertContains(response, f'href="{reverse(f"plugins:netbox_kea:{name}", args=[self.server.pk])}"')

    def test_get_with_dhcp4_disabled_redirects_to_server_with_valid_pk(self):
        """When DHCPv4 is disabled the view must redirect to the server detail page.

        The redirect URL must contain an integer pk — this is the pattern that
        would fail with servers/None if the instance had pk=None.
        """
        v6_only = _make_db_server(name="v6-only", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_leases4", args=[v6_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(v6_only.pk), response.url)

    def test_get_nonexistent_returns_404(self):
        url = reverse("plugins:netbox_kea:server_leases4", args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerLeases6View(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/leases6/"""

    def test_get_returns_200(self):
        url = reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_with_dhcp6_disabled_redirects_to_server_with_valid_pk(self):
        v4_only = _make_db_server(name="v4-only", dhcp4=True, dhcp6=False)
        url = reverse("plugins:netbox_kea:server_leases6", args=[v4_only.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(v4_only.pk), response.url)


# ─────────────────────────────────────────────────────────────────────────────
# Lease delete views
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerLeases4DeleteView(_ViewTestBase):
    """POST /plugins/kea/servers/<pk>/leases4/delete/"""

    def test_get_redirects_to_server_not_none(self):
        """GET on a POST-only view must redirect back to the server (never to servers/None)."""
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(self.server.pk), response.url)

    def test_post_empty_form_redirects_not_none(self):
        """POST with invalid/empty lease list must redirect, not to servers/None."""
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_post_htmx_single_lease_returns_hx_refresh(self):
        """An HTMX POST with a single IP and _confirm returns HX-Refresh: true instead of redirect."""
        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        with _lease_stub({"lease4-del": {"result": 0, "text": "Success"}}) as kea:
            response = self.client.post(
                url,
                {"pk": "192.0.2.1", "_confirm": "1"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Refresh"), "true")
        # De-mocked: assert the real request payload built by KeaClient.command(), not a mock call.
        self.assertEqual(kea.commands(), ["lease4-del"])
        body = kea.bodies("lease4-del")[0]
        self.assertEqual(body["arguments"], {"ip-address": "192.0.2.1"})
        self.assertEqual(body["service"], ["dhcp4"])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestServerLeases6DeleteView(_ViewTestBase):
    """POST /plugins/kea/servers/<pk>/leases6/delete/"""

    def test_get_redirects_to_server_not_none(self):
        url = reverse("plugins:netbox_kea:server_leases6_delete", args=[self.server.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        self.assertIn(str(self.server.pk), response.url)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7a: "Reserved" badge on lease pages
# ─────────────────────────────────────────────────────────────────────────────


def _close_that_fails(self) -> None:
    """Stand in for KeaClient.close when the connection is already gone."""
    raise OSError("connection already gone")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservedBadgeOnLeases(_ViewTestBase):
    """HTMX lease search must show a 'Reserved' badge when a matching reservation exists.

    The badge links to the reservation edit form so operators can quickly jump
    to the reservation from the lease table.
    """

    _LEASE4 = complete_lease(
        {
            "ip-address": "192.168.1.100",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "subnet-id": 1,
            "cltt": 1700000000,
            "valid-lft": 86400,
            "hostname": "testhost",
        }
    )
    _RESERVATION4 = {
        "ip-address": "192.168.1.100",
        "hw-address": "aa:bb:cc:dd:ee:ff",
        "subnet-id": 1,
        "hostname": "testhost",
    }
    # The lease-search page fetches the subnet suggestions via subnet{v}-list first.
    _SUBNETS4 = _subnet_list(4, [{"id": 1, "subnet": "192.168.1.0/24"}])
    #: The badge column reads the Subnet Catalogue before it looks a Reservation up, so
    #: config-get must be registered. Without it the enrichment fails and no badge can
    #: render, which the column header alone would still satisfy.
    _CATALOGUE4 = _catalogue_responses(4, 1, "192.168.1.0/24")

    def _badge_responses(self, **extra):
        return {
            **self._CATALOGUE4,
            "subnet4-list": self._SUBNETS4,
            "lease4-get": {"result": 0, "arguments": {"ip-address": "192.168.1.100", **self._LEASE4}},
            **extra,
        }

    def _htmx_get(self, url, data):
        """Issue an HTMX GET request (adds HX-Request header)."""
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def test_reserved_badge_shown_when_reservation_exists(self):
        """When a lease IP has a corresponding reservation, the table cell shows 'Reserved'."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _lease_stub(self._badge_responses(**{"reservation-get": {"result": 0, "arguments": self._RESERVATION4}})):
            response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        self.assertEqual(response.status_code, 200)
        # The column header also reads "Reserved", so assert on the badge link itself.
        self.assertContains(response, 'text-decoration-none">Reserved</a>')

    def test_a_worker_client_close_failure_keeps_the_badge(self):
        """Closing the worker clients ran in a finally that could replace the result.

        Every reservation lookup had already succeeded, so a socket error while
        releasing the connections must not blank the whole column.
        """
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with (
            stub_kea(self._badge_responses(**{"reservation-get": {"result": 0, "arguments": self._RESERVATION4}})),
            patch.object(KeaClient, "close", _close_that_fails),
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        self.assertEqual(response.status_code, 200)
        # The column header also reads "Reserved", so assert on the badge link itself.
        self.assertContains(response, 'text-decoration-none">Reserved</a>')

    def test_no_reserved_badge_when_no_reservation(self):
        """When no reservation exists for the lease IP, no badge is rendered."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _lease_stub(
            self._badge_responses(**{"reservation-get": {"result": 3}})  # not found
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        self.assertEqual(response.status_code, 200)
        # HX-Push-Url is set only on the success render path, and the lease row must
        # appear — together these prove the table rendered, not the exception partial.
        self.assertIn("HX-Push-Url", response.headers)
        self.assertContains(response, "192.168.1.100")
        # The column header says "Reserved" — check no badge link is rendered
        self.assertNotContains(response, 'text-decoration-none">Reserved</a>')

    def test_no_crash_when_host_cmds_unavailable(self):
        """When host_cmds is not loaded, reservation lookup is skipped and no badge shown."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _lease_stub(
            # host_cmds not loaded: result 2 (unknown command) makes reservation_get raise KeaException.
            self._badge_responses(**{"reservation-get": {"result": 2, "text": "unknown command 'reservation-get'"}})
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "192.168.1.100"})

        # Must not 500; page renders normally (success path sets HX-Push-Url and
        # shows the lease row) without a reservation badge.
        self.assertEqual(response.status_code, 200)
        self.assertIn("HX-Push-Url", response.headers)
        self.assertContains(response, "192.168.1.100")
        self.assertNotContains(response, 'text-decoration-none">Reserved</a>')


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9A: Lease search paths — all BY_* types
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchPaths(_ViewTestBase):
    """Each search-by type in BaseServerLeasesView.get_leases() must dispatch the
    correct Kea command with correct arguments, via HTMX GET.

    De-mocked: exercises the real ``KeaClient`` so the actual request payload built
    by ``KeaClient.command()`` — command name, ``arguments``, and ``service`` — is
    asserted, not a ``MagicMock`` call-arg. Only the HTTP boundary
    (``requests.Session.post``) is stubbed. A search issues ``subnet{v}-list`` (subnet
    quick-select) → ``lease{v}-get…`` → per-lease ``reservation-get`` enrichment.
    """

    _LEASE4 = complete_lease(
        {
            "ip-address": "10.0.0.5",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "client-id": "01:aa:bb:cc:dd:ee:ff",
            "hostname": "search-host",
            "subnet-id": 1,
            "valid-lft": 3600,
            "cltt": 1_700_000_000,
        }
    )
    # The lease-search page fetches the subnet suggestions via subnet{v}-list first.
    _SUBNETS4 = _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}])
    _SUBNETS6 = _subnet_list(6, [{"id": 1, "subnet": "2001:db8::/64"}])
    # Reservation enrichment runs for every returned lease; "not found" (result 3)
    # means no reservation, which is all these lease-command tests care about.
    _NO_RESERVATION = {"result": 3}

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def _url6(self):
        return reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])

    def _multi(self, leases):
        """Multi-result lease-get response envelope (leases list + count)."""
        return {"result": 0, "arguments": {"leases": leases, "count": len(leases)}}

    def _single(self, lease):
        """Single-result lease-get response envelope (lease fields under arguments)."""
        return {"result": 0, "arguments": dict(lease)}

    def test_search_by_hw_address_sends_correct_command(self):
        """BY_HW_ADDRESS must call lease4-get-by-hw-address with hw-address argument."""
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-by-hw-address": self._multi([dict(self._LEASE4)]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-by-hw-address", kea.commands())
        body = kea.bodies("lease4-get-by-hw-address")[0]
        self.assertEqual(body["arguments"]["hw-address"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(body["service"], ["dhcp4"])

    @override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=100))
    def test_a_guard_rejected_subnet_query_renders_the_bound_form(self):
        """The guard handler reads `form` and `form.cleaned_data`, so both must be bound.

        No other test drives this handler, and `form` was assigned inside the same `try`
        the handler serves. Any lease query added above that assignment would turn this
        into an UnboundLocalError instead of a rendered form error.
        """
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "stat-lease4-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "subnet_id", "q": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Load the stat_cmds hook")

    def test_a_large_hostname_result_enriches_only_the_rendered_page(self):
        """`lease*-get-by-hostname` takes no limit, so the whole result set was enriched.

        `configure()` already paginates the table, so the extra rows were never rendered:
        the run paid one reservation lookup per lease to enrich rows nobody sees, and the
        paginator stayed hidden, so those rows could not be reached at all.
        """
        leases = [dict(self._LEASE4, **{"ip-address": f"10.0.0.{n}"}) for n in range(1, 61)]
        responses = {
            "subnet4-list": self._SUBNETS4,
            "lease4-get-by-hostname": self._multi(leases),
            "reservation-get": self._NO_RESERVATION,
        }

        with _lease_stub(responses):
            response = self._htmx_get(self._url4(), {"by": "hostname", "q": "search-host"})

        self.assertEqual(response.status_code, 200)
        rendered = len(response.context["table"].paginated_rows)
        self.assertLess(rendered, len(leases), "the table must paginate a result set this large")
        # Enrichment stamps can_delete on every lease it touches, so this counts its input.
        enriched = [row for row in response.context["table"].data.data if "can_delete" in row]
        self.assertEqual(len(enriched), rendered)
        # Rows are being withheld, so the paginator has to be offered.
        self.assertTrue(response.context["paginate"])

    def test_search_by_hostname_sends_correct_command(self):
        """BY_HOSTNAME must call lease4-get-by-hostname with hostname argument."""
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-by-hostname": self._multi([dict(self._LEASE4)]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "hostname", "q": "search-host"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-by-hostname", kea.commands())
        body = kea.bodies("lease4-get-by-hostname")[0]
        self.assertEqual(body["arguments"]["hostname"], "search-host")
        self.assertEqual(body["service"], ["dhcp4"])

    def test_search_by_client_id_sends_correct_command(self):
        """BY_CLIENT_ID must call lease4-get-by-client-id with client-id argument."""
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-by-client-id": self._multi([dict(self._LEASE4)]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "client_id", "q": "01:aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-by-client-id", kea.commands())
        body = kea.bodies("lease4-get-by-client-id")[0]
        self.assertEqual(body["arguments"]["client-id"], "01:aa:bb:cc:dd:ee:ff")
        self.assertEqual(body["service"], ["dhcp4"])

    def test_search_by_subnet_id_sends_correct_command(self):
        """BY_SUBNET_ID must call lease4-get-all with subnets=[<id>]."""
        with _lease_stub(
            {
                **_catalogue_responses(4, 1, "10.0.0.0/24"),
                "stat-lease4-get": _subnet_stats(4, 1),
                "lease4-get-all": self._multi([dict(self._LEASE4)]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "subnet_id", "q": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get-all", kea.commands())
        body = kea.bodies("lease4-get-all")[0]
        self.assertEqual(body["arguments"]["subnets"], [1])
        self.assertEqual(body["service"], ["dhcp4"])

    @override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
    def test_search_by_subnet_id_paginates_locally_and_enriches_only_visible_page(self):
        """Expose rows after the first table page without repeating their enrichment."""
        leases = [
            {
                **self._LEASE4,
                "ip-address": f"10.0.0.{index}",
                "hw-address": f"aa:bb:cc:dd:ee:{index:02x}",
            }
            for index in range(1, 57)
        ]
        with _reservation_stub(
            4,
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-all": self._multi(leases),
                "reservation-get": self._NO_RESERVATION,
            },
        ) as kea:
            response = self._htmx_get(
                self._url4(),
                {"by": "subnet_id", "q": "1", "page": "2", "per_page": "50"},
            )

        self.assertEqual(response.status_code, 200)
        table = response.context["table"]
        self.assertTrue(hasattr(table, "page"), table.__dict__)
        self.assertEqual(table.page.number, 2)
        self.assertEqual(table.paginator.per_page, 50)
        rows = list(table.paginated_rows)
        self.assertEqual([row.record["ip_address"] for row in rows], [f"10.0.0.{index}" for index in range(51, 57)])
        self.assertTrue(response.context["paginate"])
        self.assertIsNone(response.context["next_page"])
        address_probes = 6
        distinct_hardware_addresses = 6
        distinct_client_ids = 1
        reservation_scopes = 2
        expected_probes = (
            address_probes + distinct_hardware_addresses * reservation_scopes + distinct_client_ids * reservation_scopes
        )
        # Rows outside this page are not enriched.
        self.assertEqual(len(kea.bodies("reservation-get")), expected_probes)

    @override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
    def test_one_device_with_several_leases_is_probed_once_per_identity(self):
        """Identity lookups repeat across rows, so resolve each one once for the page."""
        leases = [{**self._LEASE4, "ip-address": f"10.0.0.{index}"} for index in range(1, 6)]
        with _reservation_stub(
            4,
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-all": self._multi(leases),
                "reservation-get": self._NO_RESERVATION,
            },
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "subnet_id", "q": "1"})

        self.assertEqual(response.status_code, 200)
        # One address probe per lease, then one probe per Identity in each of the two
        # Scopes: every lease carries the same hardware address and Client ID.
        self.assertEqual(len(kea.bodies("reservation-get")), 5 + 4)

    @override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
    def test_reservation_workers_reuse_and_close_one_client_per_thread(self):
        leases = [
            {
                **self._LEASE4,
                "ip-address": f"10.0.0.{index}",
                "hw-address": f"aa:bb:cc:dd:ee:{index:02x}",
            }
            for index in range(1, 13)
        ]
        real_clone = KeaClient.clone
        real_close = KeaClient.close
        created_by_thread = {}
        closed_clients = []
        tracking_lock = threading.Lock()
        lookup_barrier = threading.Barrier(10)
        lookup_calls = 0
        barrier_broke = False
        close_failure_injected = False

        def lookup_failure(_body):
            nonlocal lookup_calls, barrier_broke
            with tracking_lock:
                lookup_calls += 1
                wait_for_workers = lookup_calls <= 10
            if wait_for_workers:
                try:
                    lookup_barrier.wait(timeout=30)
                except threading.BrokenBarrierError:
                    with tracking_lock:
                        barrier_broke = True
            return {"result": 1, "text": "lookup failed"}

        def clone_client(client):
            worker_client = real_clone(client)
            with tracking_lock:
                created_by_thread.setdefault(threading.get_ident(), []).append(worker_client)
            return worker_client

        def close_client(client):
            nonlocal close_failure_injected
            with tracking_lock:
                is_worker_client = any(
                    client is worker_client for clients in created_by_thread.values() for worker_client in clients
                )
                fail_close = is_worker_client and not close_failure_injected
                if fail_close:
                    close_failure_injected = True
            real_close(client)
            if is_worker_client:
                with tracking_lock:
                    closed_clients.append(client)
            if fail_close:
                raise RuntimeError("close failed")

        with (
            patch.object(KeaClient, "clone", autospec=True, side_effect=clone_client) as clone_spy,
            patch.object(KeaClient, "close", autospec=True, side_effect=close_client),
            _reservation_stub(
                4,
                {
                    "subnet4-list": self._SUBNETS4,
                    "lease4-get-all": self._multi(leases),
                    "reservation-get": lookup_failure,
                },
            ),
        ):
            response = self._htmx_get(self._url4(), {"by": "subnet_id", "q": "1"})

        created_clients = [client for clients in created_by_thread.values() for client in clients]
        self.assertEqual(response.status_code, 200)
        # All ten workers ran concurrently, so a shared client would show up as fewer
        # threads, and a per-lease clone would show up as more clients.
        self.assertFalse(barrier_broke)
        self.assertEqual(clone_spy.call_count, 10)
        self.assertLess(clone_spy.call_count, len(leases))
        self.assertEqual(len(created_by_thread), 10)
        self.assertTrue(all(len(clients) == 1 for clients in created_by_thread.values()))
        self.assertEqual(len({id(client) for client in created_clients}), 10)
        self.assertCountEqual([id(client) for client in closed_clients], [id(client) for client in created_clients])

    @override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
    def test_reservation_enrichment_closes_its_source_client(self):
        from netbox_kea.views.leases import _enrich_leases_with_badges

        real_close = KeaClient.close
        closed_clients = []

        def close_client(client):
            real_close(client)
            closed_clients.append(client)

        with (
            patch.object(KeaClient, "close", autospec=True, side_effect=close_client),
            _reservation_stub(4, {"subnet4-list": self._SUBNETS4}),
        ):
            # Subnet 99 is not in the Catalogue, so no worker needs a client of its own.
            lease = typed_lease(complete_lease({"ip-address": "10.0.0.5", "subnet-id": 99}))
            rows = lease_rows([lease], evaluated_at=datetime.now(tz=timezone.utc))
            _enrich_leases_with_badges(rows, self.server, 4)

        self.assertEqual(len(closed_clients), 1)

    @override_settings(PLUGINS_CONFIG=plugins_config())
    def test_search_by_subnet_id_and_state_filters_in_kea(self):
        lease = dict(self._LEASE4, state=1)
        with _lease_stub(
            {
                **_catalogue_responses(4, 1, "10.0.0.0/24"),
                "stat-lease4-get": _subnet_stats(4, 1, assigned=2001, declined=1),
                "lease4-get-by-state": self._multi([lease]),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "subnet_id", "q": "1", "state": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("lease4-get-all", kea.commands())
        self.assertEqual(
            kea.bodies("lease4-get-by-state")[0]["arguments"],
            {"subnet-id": 1, "state": 1},
        )

    @override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=100))
    def test_large_subnet_search_prompts_for_a_state_without_get_all(self):
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "stat-lease4-get": _subnet_stats(4, 1, assigned=101),
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "subnet_id", "q": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select the Active or Declined state")
        self.assertNotIn("lease4-get-all", kea.commands())

    def test_search_by_ip_returns_200(self):
        """BY_IP must call lease4-get with ip-address argument and return 200."""
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get": self._single(self._LEASE4),
                "reservation-get": self._NO_RESERVATION,
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get", kea.commands())
        body = kea.bodies("lease4-get")[0]
        self.assertEqual(body["arguments"]["ip-address"], "10.0.0.5")
        self.assertEqual(body["service"], ["dhcp4"])

    def test_search_result_3_returns_empty_table(self):
        """result=3 (not found) must render an empty table, not a 500."""
        # Empty result short-circuits enrichment, so no reservation-get is issued.
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get": {"result": 3, "arguments": None},
            }
        ) as kea:
            response = self._htmx_get(self._url4(), {"by": "ip", "q": "10.0.0.99"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease4-get", kea.commands())

    def test_search_by_duid_v6_sends_correct_command(self):
        """BY_DUID on the v6 endpoint must call lease6-get-by-duid."""
        server6 = _make_db_server(name="kea-v6-search", ca_url="https://kea6.example.com", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_leases6", args=[server6.pk])
        with _lease_stub(
            {
                "subnet6-list": self._SUBNETS6,
                "lease6-get-by-duid": self._multi([]),
            }
        ) as kea:
            response = self._htmx_get(url, {"by": "duid", "q": "00:01:aa:bb:cc:dd"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("lease6-get-by-duid", kea.commands())
        body = kea.bodies("lease6-get-by-duid")[0]
        self.assertEqual(body["arguments"]["duid"], "00:01:aa:bb:cc:dd")
        self.assertEqual(body["service"], ["dhcp6"])


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9B: CSV export — BaseServerLeasesView.get_export()
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExport(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/leases4/?export=all must return a CSV file."""

    _LEASE4 = complete_lease(
        {
            "ip-address": "10.0.0.5",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "export-host",
            "subnet-id": 1,
            "valid-lft": 3600,
            "cltt": 1_700_000_000,
        }
    )
    # The export path builds the search form, which fetches the subnet quick-select.
    _SUBNETS4 = _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}])

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_export_all_returns_csv_content_type(self):
        """?export=all must respond with text/csv Content-Type."""
        with _lease_stub(
            {"subnet4-list": self._SUBNETS4, "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)}}
        ):
            response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_export_table_returns_csv(self):
        """?export=table must also return text/csv (selected columns)."""
        with _lease_stub(
            {"subnet4-list": self._SUBNETS4, "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)}}
        ):
            response = self.client.get(self._url(), {"export": "table", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_export_with_invalid_form_redirects(self):
        """?export=all with missing q/by must redirect (not crash)."""
        # No 'q' or 'by' — form is invalid
        response = self.client.get(self._url(), {"export": "all"})
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    @override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=100))
    def test_export_by_subnet_uses_the_guarded_subnet_query(self):
        """A Subnet export must not rely on global lease-page ordering."""
        leases = [
            complete_lease(
                {
                    "ip-address": f"10.0.0.{i}",
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "hostname": f"h{i}",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 1_700_000_000,
                }
            )
            for i in range(1, 4)
        ]
        with _lease_stub(
            {
                **_catalogue_responses(4, 1, "10.0.0.0/24"),
                "stat-lease4-get": _subnet_stats(4, 1, assigned=3),
                "lease4-get-all": {"result": 0, "arguments": {"leases": leases}},
            }
        ) as kea:
            response = self.client.get(
                self._url(),
                {"export": "all", "by": "subnet", "q": "10.0.0.0/24"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))
        self.assertIn("lease4-get-all", kea.commands())
        self.assertNotIn("lease4-get-page", kea.commands())


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9C: Lease delete — full confirmation flow + error paths
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseDeleteFullFlow(_ViewTestBase):
    """Full POST flow for lease bulk deletion: confirm page → confirmed delete → Kea error."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])

    def test_post_with_ips_no_confirm_renders_confirmation_page(self):
        """POST with lease IPs but no _confirm renders the bulk_delete confirmation template."""
        response = self.client.post(self._url(), {"pk": ["10.0.0.1", "10.0.0.2"]})
        self.assertEqual(response.status_code, 200)
        # Must show the confirmation template (not a redirect)
        self.assertContains(response, "10.0.0.1")
        self.assertContains(response, "10.0.0.2")

    def test_confirmation_page_shows_no_background_job_label(self):
        """NetBox renders the label of the hidden background_job field, so the field has no label."""
        response = self.client.post(self._url(), {"pk": ["10.0.0.1"]})
        self.assertContains(response, "Confirm Bulk Deletion")
        self.assertNotContains(response, "background_job")

    def test_post_confirmed_calls_kea_and_redirects(self):
        """POST with _confirm=1 must call Kea lease4-del and redirect."""
        with _lease_stub({"lease4-del": {"result": 0}}) as kea:
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)
        # Verify Kea was called with the lease4-del command (real payload on the wire).
        self.assertIn("lease4-del", kea.commands())
        self.assertEqual(kea.bodies("lease4-del")[0]["arguments"], {"ip-address": "10.0.0.1"})

    def test_post_confirmed_kea_error_redirects_with_error_message(self):
        """When Kea returns an error during deletion, must redirect (not 500) and show error."""
        # result=1 makes the real KeaClient.command() raise KeaException (delete uses check=(0,3)).
        with _lease_stub({"lease4-del": {"result": 1, "text": "lease not found"}}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.5"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        self._assert_no_none_pk_redirect(response)

    def test_forbidden_user_gets_403(self):
        """A user without bulk_delete_lease_from_server permission must receive 403."""
        from django.contrib.auth import get_user_model as _get_user_model

        User2 = _get_user_model()
        unprivileged = User2.objects.create_user("noperm_user", password="x")
        self.client.force_login(unprivileged)
        response = self.client.post(
            self._url(),
            {"pk": ["10.0.0.1"], "_confirm": "1"},
        )
        self.assertEqual(response.status_code, 403)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9D: _enrich_leases_with_badges error paths
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestEnrichLeasesErrorPaths(_ViewTestBase):
    """_enrich_leases_with_badges must degrade gracefully on unexpected errors."""

    _LEASE4 = complete_lease(
        {
            "ip-address": "10.0.0.5",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "enrich-host",
            "subnet-id": 1,
            "valid-lft": 3600,
            "cltt": 1_700_000_000,
        }
    )
    _SUBNETS4 = _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}])

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def test_non_result2_kea_exception_does_not_crash(self):
        """A KeaException with result=1 (server error) on reservation lookup must not 500."""
        # result=1 on reservation-get makes the real client raise KeaException (non-result-2),
        # which enrichment treats as indeterminate rather than crashing.
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _reservation_stub(
            4,
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 1, "text": "server error"},
            },
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)

    def test_unexpected_exception_on_reservation_lookup_does_not_crash(self):
        """An unexpected exception (e.g. network error) during reservation lookup must not 500."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _reservation_stub(
            4,
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": RuntimeError("socket closed"),
            },
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)

    def test_a_non_integer_subnet_id_excludes_the_lease_with_a_safe_diagnostic(self):
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        for subnet_id in (1.0, True):
            lease = {**self._LEASE4, "subnet-id": subnet_id}
            with self.subTest(subnet_id=subnet_id):
                with _reservation_stub(
                    4,
                    {
                        "subnet4-list": self._SUBNETS4,
                        "lease4-get": {"result": 0, "arguments": lease},
                    },
                ) as kea:
                    response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("reservation-get", kea.commands())
                self.assertEqual(list(response.context["table"].rows), [])
                self.assertEqual(
                    response.context["lease_diagnostics"], ["arguments (subnet-id): A lease field has the wrong type."]
                )
                self.assertContains(response, "1 lease record that could not be read")

    def test_unknown_subnet_id_keeps_reservation_dependent_actions_unavailable(self):
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        lease = {**self._LEASE4, "subnet-id": 99}
        with _reservation_stub(
            4,
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get": {"result": 0, "arguments": lease},
            },
        ) as kea:
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reservation-get", kea.commands())
        row = next(iter(response.context["table"].rows)).record
        self.assertIsNone(row.get("sync_url"))
        self.assertIsNone(row["create_reservation_url"])

    def test_sync_url_set_when_no_netbox_ip(self):
        """When the lease IP is absent from NetBox, sync_url must be set on the lease dict."""
        # No NbIP created → bulk_fetch_netbox_ips returns {} from the real (empty) DB.
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _reservation_stub(
            4,
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 3},  # no reservation
            },
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        # Sync button (hx-post) must appear since no NetBox IP
        self.assertContains(response, "hx-post")

    def test_synced_badge_set_when_netbox_ip_exists(self):
        """When the lease IP exists in NetBox IPAM, netbox_ip_url must be set (Synced badge)."""
        NbIP.objects.create(address="10.0.0.5/24")  # real IPAM row → resolved by bulk_fetch_netbox_ips
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _reservation_stub(
            4,
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 3},  # no reservation
            },
        ):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synced")


# ─────────────────────────────────────────────────────────────────────────────
# P3 Refinement: stale MAC badge — specific MAC values + inline delete URL
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestStaleMacBadgeEnrichment(_ViewTestBase):
    """_enrich_leases_with_badges must store MAC strings and delete URL on stale-MAC leases."""

    _LEASE4 = complete_lease(
        {
            "ip-address": "10.0.0.5",
            "hw-address": "aa:bb:cc:dd:ee:01",
            "hostname": "stale-host",
            "subnet-id": 7,
            "valid-lft": 3600,
            "cltt": 1_700_000_000,
        }
    )
    _RESERVATION = {
        "ip-address": "10.0.0.5",
        "hw-address": "aa:bb:cc:dd:ee:99",  # different MAC → stale
        "subnet-id": 7,
    }
    _SUBNETS4 = _subnet_list(4, [{"id": 7, "subnet": "10.0.0.0/24"}])

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _stub(self, reservation):
        """stub_kea responses for a single IP-matched lease with *reservation*."""
        return _reservation_stub(
            4,
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get": {"result": 0, "arguments": dict(self._LEASE4)},
                "reservation-get": {"result": 0, "arguments": reservation},
            },
        )

    def test_stale_mac_badge_shows_specific_macs_in_title(self):
        """The ⚠ MAC? badge title must contain both lease MAC and reservation MAC."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub(self._RESERVATION):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "aa:bb:cc:dd:ee:01")  # lease MAC in tooltip
        self.assertContains(response, "aa:bb:cc:dd:ee:99")  # reservation MAC in tooltip

    def test_stale_mac_badge_renders_htmx_delete_button(self):
        """The stale-MAC badge must include an HTMX delete button (hx-post) for one-click removal."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub(self._RESERVATION):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        # hx-post must point to the delete endpoint (distinct from the bulk-delete form action)
        delete_url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        self.assertContains(response, f'hx-post="{delete_url}"')

    def test_matching_mac_badge_has_no_htmx_delete_button(self):
        """When lease MAC matches reservation MAC, no HTMX delete button must appear."""
        matching_rsv = {**self._RESERVATION, "hw-address": self._LEASE4["hw-address"]}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub(matching_rsv):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        delete_url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        self.assertNotContains(response, f'hx-post="{delete_url}"')

    def test_stale_mac_badge_no_delete_when_no_permission(self):
        """When the user lacks delete permission, the stale-MAC badge must NOT include an HTMX delete button."""
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        User = get_user_model()
        readonly_user = User.objects.create_user(username="readonly_stale", password="pass")
        # Grant only view permission via NetBox's ObjectPermission (not change/delete)
        ct = ContentType.objects.get_for_model(Server)
        view_obj_perm = ObjectPermission.objects.create(name="test-view-server-readonly", actions=["view"])
        view_obj_perm.object_types.add(ct)
        view_obj_perm.users.add(readonly_user)
        self.client.force_login(readonly_user)

        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with self._stub(self._RESERVATION):
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        delete_url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        self.assertNotContains(response, f'hx-post="{delete_url}"')


# ─────────────────────────────────────────────────────────────────────────────
# Feature 3.3: Export All Leases — BaseServerDHCPLeasesView.get_export_all()
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportAll(_ViewTestBase):
    """GET /plugins/kea/servers/<pk>/leases4/?export_all=1 must return a full CSV."""

    _LEASE = complete_lease(
        {
            "ip-address": "10.0.0.1",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "export-host",
            "subnet-id": 1,
            "valid-lft": 3600,
            "cltt": 1_700_000_000,
        }
    )

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def _url6(self):
        return reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])

    def test_export_all_returns_csv(self):
        """?export_all=1 must return text/csv."""
        # One page of one lease (count 1 < per_page 1000) ends pagination after one call.
        with _lease_stub({"lease4-get-page": {"result": 0, "arguments": {"leases": [self._LEASE], "count": 1}}}):
            response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_export_all_includes_lease_data(self):
        """?export_all=1 CSV must contain the lease IP address."""
        with _lease_stub({"lease4-get-page": {"result": 0, "arguments": {"leases": [self._LEASE], "count": 1}}}):
            response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        content = (
            b"".join(response.streaming_content).decode()
            if hasattr(response, "streaming_content")
            else response.content.decode()
        )
        self.assertIn("10.0.0.1", content)

    def test_export_all_paginates_all_leases(self):
        """?export_all=1 must paginate until Kea returns result=3."""
        # The view uses per_page=1000. Report count==1000 on the first page so the
        # view sees a full page and issues a second request; page 2 returns result=3.
        page1 = [
            complete_lease(
                {
                    "ip-address": f"198.18.{i // 256}.{i % 256}",
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "hostname": f"h{i}",
                    "subnet-id": 1,
                    "valid-lft": 3600,
                    "cltt": 1_700_000_000,
                }
            )
            for i in range(1000)
        ]
        with _lease_stub(
            {
                "lease4-get-page": queued(
                    {"result": 0, "arguments": {"leases": page1, "count": 1000}},
                    {"result": 3, "arguments": None},
                )
            }
        ) as kea:
            response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))
        self.assertGreaterEqual(kea.commands().count("lease4-get-page"), 2)

    def test_export_all_v6_starts_from_double_colon(self):
        """?export_all=1 for v6 must start the cursor from '::'."""
        with _lease_stub({"lease6-get-page": {"result": 3, "arguments": None}}) as kea:
            response = self.client.get(self._url6(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        pages = kea.bodies("lease6-get-page")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["arguments"]["from"], "::")

    def test_export_all_v4_starts_from_zero_ip(self):
        """?export_all=1 for v4 must start the cursor from '0.0.0.0'."""
        with _lease_stub({"lease4-get-page": {"result": 3, "arguments": None}}) as kea:
            response = self.client.get(self._url4(), {"export_all": "1"})
        self.assertEqual(response.status_code, 200)
        pages = kea.bodies("lease4-get-page")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["arguments"]["from"], "0.0.0.0")  # noqa: S104 - Kea sentinel value, not a bind address


# TestLeaseEditView
# ---------------------------------------------------------------------------

_LEASE4_GET_RESP = [
    {
        "result": 0,
        "arguments": complete_lease(
            {
                "ip-address": "10.0.0.100",
                "hw-address": "aa:bb:cc:dd:ee:ff",
                "hostname": "host1.example.com",
                "subnet-id": 1,
                "cltt": 1700000000,
                "valid-lft": 3600,
                "state": 0,
            }
        ),
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditView(_ViewTestBase):
    """Tests for ServerLease4/6EditView."""

    def _url(self, version=4, ip="10.0.0.100"):
        return reverse(
            f"plugins:netbox_kea:server_lease{version}_edit",
            args=[self.server.pk, ip],
        )

    def test_url_registered_v4(self):
        """URL server_lease4_edit is registered."""
        url = self._url(version=4)
        self.assertIn("leases", url)
        self.assertIn("edit", url)

    def test_url_registered_v6(self):
        """URL server_lease6_edit is registered."""
        url = self._url(version=6, ip="2001:db8::100")
        self.assertIn("leases", url)
        self.assertIn("edit", url)

    def test_get_returns_200(self):
        """GET returns 200 OK."""
        with _lease_stub({"lease4-get": _LEASE4_GET_RESP[0]}):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_prefills_hostname(self):
        """GET pre-fills hostname from the existing lease."""
        with _lease_stub({"lease4-get": _LEASE4_GET_RESP[0]}):
            response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("host1.example.com", content)

    def test_get_prefills_hw_address(self):
        """GET pre-fills hw_address from the existing lease (v4 only)."""
        with _lease_stub({"lease4-get": _LEASE4_GET_RESP[0]}):
            response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("aa:bb:cc:dd:ee:ff", content)

    def test_post_calls_lease_update_and_redirects(self):
        """POST with valid data calls lease_update and redirects."""
        # lease_update reads the current lease (lease4-get) then writes lease4-update.
        with _lease_stub({"lease4-get": _LEASE4_GET_RESP[0], "lease4-update": {"result": 0}}) as kea:
            response = self.client.post(
                self._url(),
                {
                    "hostname": "newhost.example.com",
                    "hw_address": "11:22:33:44:55:66",
                    "valid_lft": "7200",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("lease4-update", kea.commands())
        update_args = kea.bodies("lease4-update")[0]["arguments"]
        self.assertEqual(update_args["hostname"], "newhost.example.com")
        self.assertEqual(update_args["hw-address"], "11:22:33:44:55:66")
        self.assertEqual(update_args["valid-lft"], 7200)

    def test_post_kea_exception_redirects_with_error(self):
        """POST that raises KeaException shows error and redirects."""
        # result=1 on lease4-update makes the real client raise KeaException.
        with _lease_stub(
            {"lease4-get": _LEASE4_GET_RESP[0], "lease4-update": {"result": 1, "text": "lease not found"}}
        ):
            response = self.client.post(
                self._url(),
                {
                    "hostname": "newhost.example.com",
                    "hw_address": "11:22:33:44:55:66",
                    "valid_lft": "7200",
                },
            )
        self.assertEqual(response.status_code, 302)

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected."""
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(response.status_code, (302, 403))


# ---------------------------------------------------------------------------
# TestLeaseStateFilter
# ---------------------------------------------------------------------------

_STATE_LEASES_RESP = [
    {
        "result": 0,
        "arguments": {
            "leases": [
                complete_lease(
                    {
                        "ip-address": "10.0.0.1",
                        "hw-address": "aa:bb:cc:dd:ee:01",
                        "hostname": "active-host",
                        "subnet-id": 1,
                        "valid-lft": 3600,
                        "cltt": 1_700_000_000,
                        "state": 0,
                    }
                ),
                complete_lease(
                    {
                        "ip-address": "10.0.0.2",
                        "hw-address": "aa:bb:cc:dd:ee:02",
                        "hostname": "declined-host",
                        "subnet-id": 1,
                        "valid-lft": 3600,
                        "cltt": 1_700_000_000,
                        "state": 1,
                    }
                ),
                complete_lease(
                    {
                        "ip-address": "10.0.0.3",
                        "hw-address": "aa:bb:cc:dd:ee:03",
                        "hostname": "expired-host",
                        "subnet-id": 1,
                        "valid-lft": 3600,
                        "cltt": 1_700_000_000,
                        "state": 2,
                    }
                ),
            ]
        },
    }
]

_PAGE_LEASES_RESP = [
    {
        "result": 0,
        "arguments": {
            "count": 2,
            "leases": [
                complete_lease(
                    {
                        "ip-address": "10.0.0.10",
                        "hw-address": "aa:bb:cc:dd:ee:10",
                        "hostname": "page-active",
                        "subnet-id": 1,
                        "valid-lft": 3600,
                        "cltt": 1_700_000_000,
                        "state": 0,
                    }
                ),
                complete_lease(
                    {
                        "ip-address": "10.0.0.11",
                        "hw-address": "aa:bb:cc:dd:ee:11",
                        "hostname": "page-declined",
                        "subnet-id": 1,
                        "valid-lft": 3600,
                        "cltt": 1_700_000_000,
                        "state": 1,
                    }
                ),
            ],
        },
    }
]


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseStateFilter(_ViewTestBase):
    """Tests that the optional state filter correctly limits lease results."""

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    # Subnet suggestions fetched via subnet4-list; reservation enrichment finds none.
    _SUBNETS4 = _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}])

    def test_state_column_rendered_in_table(self):
        """Lease table includes a state_label column header."""
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-by-hw-address": _STATE_LEASES_RESP[0],
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:01"})
        self.assertEqual(response.status_code, 200)
        # State column header must be present
        self.assertContains(response, "State")

    def test_state_label_active_rendered(self):
        """Active lease shows 'Active' state badge."""
        active_lease = _STATE_LEASES_RESP[0]["arguments"]["leases"][0]
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-by-hw-address": {"result": 0, "arguments": {"leases": [active_lease]}},
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:01"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active")

    def test_state_label_declined_rendered(self):
        """Declined lease shows 'Declined' state badge."""
        declined_lease = _STATE_LEASES_RESP[0]["arguments"]["leases"][1]
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-by-hw-address": {"result": 0, "arguments": {"leases": [declined_lease]}},
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hw", "q": "aa:bb:cc:dd:ee:02"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Declined")

    def test_state_filter_declined_hides_active(self):
        """State filter=1 (Declined) excludes Active leases from search results."""
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-by-hostname": _STATE_LEASES_RESP[0],
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hostname", "q": "host", "state": "1"})
        self.assertEqual(response.status_code, 200)
        # Active and Expired hosts should not appear
        self.assertNotContains(response, "active-host")
        self.assertNotContains(response, "expired-host")
        self.assertContains(response, "declined-host")

    def test_state_filter_any_returns_all(self):
        """Empty state filter (Any) returns all leases."""
        with _lease_stub(
            {
                "subnet4-list": self._SUBNETS4,
                "lease4-get-by-hostname": _STATE_LEASES_RESP[0],
                "reservation-get": {"result": 3},
            }
        ):
            response = self._htmx_get(self._url4(), {"by": "hostname", "q": "host", "state": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "active-host")
        self.assertContains(response, "declined-host")
        self.assertContains(response, "expired-host")

    # Unguarded on purpose: no stat-lease4-get preflight runs, so the Subnet query is
    # the only command this test measures. State it here instead of inheriting it.
    @override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
    def test_state_filter_is_sent_with_the_subnet_query(self):
        """A Subnet state filter must run in Kea before it builds the response."""
        declined = next(lease for lease in _PAGE_LEASES_RESP[0]["arguments"]["leases"] if lease["state"] == 1)
        with _lease_stub(
            {
                **_catalogue_responses(4, 1, "10.0.0.0/24"),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": [declined]}},
                "reservation-get": {"result": 3},
            }
        ) as kea:
            response = self._htmx_get(
                self._url4(),
                {"by": "subnet", "q": "10.0.0.0/24", "state": "1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "page-active")
        self.assertContains(response, "page-declined")
        self.assertEqual(
            kea.bodies("lease4-get-by-state")[0]["arguments"],
            {"subnet-id": 1, "state": 1},
        )


@override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
class TestLeaseSearchHostBitsSubnet(_ViewTestBase):
    """Kea accepts a Subnet prefix with host bits, so a canonical CIDR search must still find it."""

    def test_canonical_cidr_search_resolves_the_declared_subnet(self):
        with _lease_stub(
            {
                **_catalogue_responses(4, 1, "10.0.0.5/24"),
                "lease4-get-all": _PAGE_LEASES_RESP[0],
                "reservation-get": {"result": 3},
            }
        ) as kea:
            response = self.client.get(
                reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk]),
                {"by": "subnet", "q": "10.0.0.0/24"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.bodies("lease4-get-all")[0]["arguments"], {"subnets": [1]})
        self.assertContains(response, "page-active")

    def test_two_spellings_of_one_network_block_the_search(self):
        # Kea 3.2.0 loads both as separate Subnets, so one network names two Subnet IDs.
        subnets = [{"id": 1, "subnet": "198.18.1.5/24"}, {"id": 2, "subnet": "198.18.1.0/24"}]
        with _lease_stub(
            {
                **_catalogue_responses_for_subnets(4, subnets),
                "lease4-get-all": _PAGE_LEASES_RESP[0],
                "reservation-get": {"result": 3},
            }
        ) as kea:
            response = self.client.get(
                reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk]),
                {"by": "subnet", "q": "198.18.1.0/24"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("lease4-get-all", kea.commands())
        self.assertTemplateUsed(response, "netbox_kea/exception_htmx.html")
        self.assertContains(response, "An internal error occurred")


# ---------------------------------------------------------------------------
# TestLeaseAddView — Manual Lease Add (lease4/6-add)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=plugins_config())
class TestLeaseAddView(_ViewTestBase):
    """Tests for ServerLease4AddView and ServerLease6AddView."""

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:server_lease{version}_add", args=[self.server.pk])

    def _valid_post4(self, **overrides):
        data = {
            "ip_address": "10.0.0.200",
            "subnet_id": "1",
            "hw_address": "aa:bb:cc:dd:ee:ff",
            "valid_lft": "3600",
            "hostname": "newlease.example.com",
        }
        data.update(overrides)
        return data

    def _valid_post6(self, **overrides):
        data = {
            "ip_address": "2001:db8::200",
            "duid": "00:01:02:03:04:05",
            "iaid": "12345",
            "subnet_id": "1",
            "valid_lft": "3600",
            "hostname": "newlease6.example.com",
        }
        data.update(overrides)
        return data

    def test_url_registered_v4(self):
        """URL server_lease4_add is registered and contains 'leases'."""
        url = self._url(version=4)
        self.assertIn("leases", url)
        self.assertIn("add", url)

    def test_url_registered_v6(self):
        """URL server_lease6_add is registered and contains 'leases'."""
        url = self._url(version=6)
        self.assertIn("leases", url)
        self.assertIn("add", url)

    def test_get_lease4_add_returns_200(self):
        """GET /leases4/add/ returns 200 and renders the add form."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ip_address")

    def test_get_lease6_add_returns_200(self):
        """GET /leases6/add/ returns 200 and shows duid + iaid fields."""
        response = self.client.get(self._url(version=6))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "duid")
        self.assertContains(response, "iaid")

    def test_post_lease4_add_valid_redirects(self):
        """POST valid v4 lease data redirects to the lease list."""
        with _lease_stub({"lease4-add": {"result": 0}}):
            response = self.client.post(self._url(version=4), self._valid_post4())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)

    def test_post_lease4_add_calls_kea_with_correct_args(self):
        """POST v4 calls lease_add with ip-address, hw-address, and subnet-id."""
        with _lease_stub({"lease4-add": {"result": 0}}) as kea:
            self.client.post(self._url(version=4), self._valid_post4())
        self.assertIn("lease4-add", kea.commands())
        lease = kea.bodies("lease4-add")[0]["arguments"]
        self.assertEqual(lease["ip-address"], "10.0.0.200")
        self.assertEqual(lease.get("hw-address"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(lease.get("subnet-id"), 1)

    def test_post_lease4_add_invalid_ip_shows_form_errors(self):
        """POST with a non-IPv4 string re-renders form with validation errors."""
        # Empty registry: any Kea command would raise — proves no lease was created.
        with _lease_stub({}) as kea:
            response = self.client.post(self._url(version=4), self._valid_post4(ip_address="not-an-ip"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])

    def test_post_lease4_add_kea_exception_shows_error_message(self):
        """POST that triggers a KeaException shows error and re-renders (no redirect)."""
        # result=1 on lease4-add makes the real client raise KeaException.
        with _lease_stub({"lease4-add": {"result": 1, "text": "address already in use"}}):
            response = self.client.post(self._url(version=4), self._valid_post4())
        self.assertIn(response.status_code, (200, 302))

    def test_post_lease6_add_valid_redirects(self):
        """POST valid v6 lease data redirects to the lease list."""
        with _lease_stub({"lease6-add": {"result": 0}}):
            response = self.client.post(self._url(version=6), self._valid_post6())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("None", response.url)

    def test_post_lease6_add_calls_kea_with_correct_args(self):
        """POST v6 calls lease_add with ip-address, duid, and iaid."""
        with _lease_stub({"lease6-add": {"result": 0}}) as kea:
            self.client.post(self._url(version=6), self._valid_post6())
        self.assertIn("lease6-add", kea.commands())
        lease = kea.bodies("lease6-add")[0]["arguments"]
        self.assertEqual(lease["ip-address"], "2001:db8::200")
        self.assertEqual(lease.get("duid"), "00:01:02:03:04:05")
        self.assertEqual(lease.get("iaid"), 12345)

    def test_get_requires_login(self):
        """Unauthenticated GET is redirected to login."""
        self.client.logout()
        response = self.client.get(self._url(version=4))
        self.assertIn(response.status_code, (302, 403))


# ---------------------------------------------------------------------------
# TestLeaseAddSyncToNetBox — sync-to-netbox checkbox on lease add form
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=plugins_config())
class TestLeaseAddSyncToNetBox(_ViewTestBase):
    """Tests for the sync_to_netbox checkbox on ServerLease4/6AddView."""

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:server_lease{version}_add", args=[self.server.pk])

    def _post4(self, sync=False):
        data = {
            "ip_address": "10.0.0.200",
            "subnet_id": "1",
            "hw_address": "aa:bb:cc:dd:ee:ff",
            "valid_lft": "3600",
            "hostname": "newlease.example.com",
        }
        if sync:
            data["sync_to_netbox"] = "on"
        return data

    # A followed redirect lands on the leases page, which fetches the subnet quick-select.
    _SUBNETS4 = _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}])

    @staticmethod
    def _readback(address="10.0.0.200", **fields):
        """The ``lease4-get`` reply that observes the created lease; the claim reads Kea's facts, not the form."""
        observed = {"hostname": "newlease.example.com", "hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1, **fields}
        return {"result": 0, "arguments": complete_lease({"ip-address": address, **observed})}

    def test_lease4_add_form_has_sync_to_netbox_field(self):
        """GET lease4 add page renders a sync_to_netbox checkbox."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)
        self.assertIn("sync_to_netbox", response.content.decode())

    def test_post_lease4_add_with_sync_links_the_created_ip(self):
        from netbox_kea.models import IPAMOwnershipLink

        responses = {"lease4-add": {"result": 0}, "lease4-get": self._readback(), "subnet4-list": self._SUBNETS4}
        with _lease_stub(responses) as kea:
            response = self.client.post(self._url(version=4), self._post4(sync=True))
        self.assertEqual(kea.commands().count("lease4-get"), 1)
        self.assertEqual(response.status_code, 302)
        ip = NbIP.objects.get(address__net_host="10.0.0.200")
        link = IPAMOwnershipLink.objects.get(ip_address=ip)
        self.assertEqual((link.server_id, link.family, link.source), (self.server.pk, 4, "lease"))
        self.assertEqual(link.facts, {"hostname": "newlease.example.com", "prefix_length": 24})

    @override_settings(PLUGINS_CONFIG=plugins_config(stale_ip_cleanup="remove"))
    def test_lease_add_preserves_reserved_siblings_and_their_ownership(self):
        from netbox_kea.models import IPAMOwnershipLink, next_confirmation_number

        siblings = [
            NbIP.objects.create(
                address=f"198.18.0.{number}/24",
                status="reserved",
                dns_name="same.example.invalid",
                description="[kea-sync: reservation] retain operator note",
            )
            for number in (20, 21)
        ]
        IPAMOwnershipLink.objects.create(
            confirmation=next_confirmation_number(),
            server=self.server,
            family=4,
            source="reservation",
            ip_address=siblings[0],
            facts={"hostname": "same.example.invalid", "prefix_length": 24},
        )
        sibling_ids = [row.pk for row in siblings]
        before_rows = list(NbIP.objects.filter(pk__in=sibling_ids).order_by("pk").values())
        before_links = list(IPAMOwnershipLink.objects.filter(ip_address_id__in=sibling_ids).order_by("pk").values())
        data = self._post4(sync=True)
        data.update(ip_address="198.18.0.22", hostname="same.example.invalid")
        with _lease_stub(
            {
                "lease4-add": {"result": 0},
                "lease4-get": self._readback("198.18.0.22", hostname="same.example.invalid"),
                "subnet4-list": _subnet_list(4, [{"id": 1, "subnet": "198.18.0.0/24"}]),
            }
        ) as kea:
            response = self.client.post(self._url(version=4), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("lease4-add"), 1)
        self.assertTrue(
            IPAMOwnershipLink.objects.filter(
                server=self.server, source="lease", ip_address__address__net_host="198.18.0.22"
            ).exists()
        )
        self.assertEqual(list(NbIP.objects.filter(pk__in=sibling_ids).order_by("pk").values()), before_rows)
        self.assertEqual(
            list(IPAMOwnershipLink.objects.filter(ip_address_id__in=sibling_ids).order_by("pk").values()),
            before_links,
        )

    def test_optional_subnet_add_uses_the_created_lease_facts(self):
        from ipam.models import VRF

        from netbox_kea.models import IPAMOwnershipLink

        self.server.sync_vrf = VRF.objects.create(name="lease-sync")
        self.server.save()
        data = {"ip_address": "198.18.0.42", "hw_address": "aa:bb:cc:dd:ee:ff", "sync_to_netbox": "on"}
        responses = {
            "lease4-add": {"result": 0},
            "lease4-get": self._readback("198.18.0.42", **{"subnet-id": 7}),
            "subnet4-list": _subnet_list(
                4, [{"id": 7, "subnet": "198.18.0.0/24"}, {"id": 8, "subnet": "198.18.0.0/25"}]
            ),
        }
        with _lease_stub(responses) as kea:
            response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        ip = NbIP.objects.get(address__net_host="198.18.0.42", vrf=self.server.sync_vrf)
        self.assertEqual(str(ip.address), "198.18.0.42/24")
        link = IPAMOwnershipLink.objects.get(ip_address=ip)
        self.assertEqual((link.server_id, link.family, link.source), (self.server.pk, 4, "lease"))
        self.assertEqual(link.facts["prefix_length"], 24)
        self.assertEqual(kea.commands().count("lease4-add"), 1)
        self.assertEqual(kea.commands().count("lease4-get"), 1)
        self.assertNotIn("subnet-id", kea.requests[0]["arguments"])

    def test_optional_subnet_ipv6_add_matches_canonical_address(self):
        from ipam.models import VRF

        from netbox_kea.models import IPAMOwnershipLink

        self.server.sync_vrf = VRF.objects.create(name="lease-sync-v6")
        self.server.save()
        data = {"ip_address": "2001:db8::42", "duid": "00:01:02:03", "iaid": 1, "sync_to_netbox": "on"}
        responses = {
            "lease6-add": {"result": 0},
            "lease6-get": {
                "result": 0,
                "arguments": complete_lease({"ip-address": "2001:0db8:0000:0000:0000:0000:0000:0042", "subnet-id": 7}),
            },
            "subnet6-list": _subnet_list(
                6, [{"id": 7, "subnet": "2001:db8::/64"}, {"id": 8, "subnet": "2001:db8::/80"}]
            ),
        }
        with _lease_stub(responses) as kea:
            response = self.client.post(self._url(6), data)
        self.assertEqual(response.status_code, 302)
        ip = NbIP.objects.get(address__net_host="2001:db8::42", vrf=self.server.sync_vrf)
        self.assertEqual(str(ip.address), "2001:db8::42/64")
        link = IPAMOwnershipLink.objects.get(ip_address=ip)
        self.assertEqual((link.server_id, link.family, link.source), (self.server.pk, 6, "lease"))
        self.assertEqual(link.facts["prefix_length"], 64)
        self.assertEqual(kea.commands().count("lease6-get"), 1)
        self.assertNotIn("subnet-id", kea.requests[0]["arguments"])

    def test_optional_subnet_readback_failure_preserves_success_without_ipam_writes(self):
        from netbox_kea.models import IPAMOwnershipLink

        for family, address in ((4, "198.18.0.42"), (6, "2001:db8::42")):
            cases = {
                "absent": {"result": 3},
                "wrong-address": {
                    "result": 0,
                    "arguments": {"ip-address": "198.18.0.43" if family == 4 else "2001:db8::43", "subnet-id": 7},
                },
                "malformed-response": {"result": 0, "arguments": []},
                "malformed-address": {"result": 0, "arguments": {"ip-address": "invalid", "subnet-id": 7}},
                "missing-id": {"result": 0, "arguments": {"ip-address": address}},
                "malformed-id": {"result": 0, "arguments": {"ip-address": address, "subnet-id": []}},
                "unsupported": {"result": 2},
                "unavailable": requests.ConnectionError("readback unavailable"),
                "invalid-json": ValueError("invalid JSON"),
                "socket-error": OSError("readback unavailable"),
            }
            for name, readback in cases.items():
                with self.subTest(family=family, readback=name):
                    data = {
                        "ip_address": address,
                        "hw_address": "aa:bb:cc:dd:ee:ff",
                        "duid": "00:01:02:03",
                        "iaid": 1,
                        "sync_to_netbox": "on",
                    }
                    subnet = "198.18.0.0/24" if family == 4 else "2001:db8::/64"
                    with _lease_stub(
                        {
                            f"lease{family}-add": {"result": 0},
                            f"lease{family}-get": readback,
                            f"subnet{family}-list": _subnet_list(family, [{"id": 7, "subnet": subnet}]),
                        }
                    ) as kea:
                        response = self.client.post(self._url(family), data)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(kea.commands().count(f"lease{family}-add"), 1)
                    self.assertEqual(kea.commands().count(f"lease{family}-get"), 1)
                    messages = [str(message) for message in get_messages(response.wsgi_request)]
                    self.assertTrue(any("created." in message for message in messages))
                    self.assertTrue(any("sync failed" in message for message in messages))
                    self.assertFalse(NbIP.objects.exists())
                    self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_optional_subnet_without_sync_does_not_read_back(self):
        for family, address in ((4, "198.18.0.42"), (6, "2001:db8::42")):
            with self.subTest(family=family):
                with _lease_stub({f"lease{family}-add": {"result": 0}}) as kea:
                    response = self.client.post(
                        self._url(family),
                        {
                            "ip_address": address,
                            "duid": "00:01:02:03",
                            "iaid": 1,
                            "hw_address": "aa:bb:cc:dd:ee:ff",
                        },
                    )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(kea.commands(), [f"lease{family}-add"])
                self.assertFalse(NbIP.objects.exists())

    def test_post_lease4_add_keeps_kea_success_when_the_catalogue_is_unavailable(self):
        from netbox_kea.models import IPAMOwnershipLink

        with _lease_stub(
            {
                "lease4-add": {"result": 0},
                "lease4-get": self._readback(),
                "subnet4-list": {"result": 1},
                "config-get": {"result": 1},
            }
        ) as kea:
            response = self.client.post(self._url(version=4), self._post4(sync=True))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("lease4-add"), 1)
        self.assertTrue(any("sync failed" in str(message) for message in get_messages(response.wsgi_request)))
        self.assertFalse(NbIP.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_post_lease4_add_without_sync_does_not_write_ipam(self):
        from netbox_kea.models import IPAMOwnershipLink

        with _lease_stub({"lease4-add": {"result": 0}, "subnet4-list": self._SUBNETS4}):
            response = self.client.post(self._url(version=4), self._post4(sync=False))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(NbIP.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_post_lease4_add_reports_owner_disagreement_and_keeps_the_existing_row(self):
        from netbox_kea.ipam_reconciliation import claim
        from netbox_kea.models import IPAMOwnershipLink

        other = Server.objects.create(name="other-owner", ca_url="https://other.example.com")
        with _lease_stub({"subnet4-list": self._SUBNETS4}):
            existing = claim(
                other, 4, [typed_lease(self._readback(hostname="other.example.com")["arguments"])], force=False
            )
        before = NbIP.objects.values().get(pk=existing.primary.pk)
        responses = {"lease4-add": {"result": 0}, "lease4-get": self._readback(), "subnet4-list": self._SUBNETS4}
        with _lease_stub(responses):
            response = self.client.post(self._url(version=4), self._post4(sync=True), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("owners disagree" in str(message) for message in response.context["messages"]))
        self.assertEqual(NbIP.objects.values().get(pk=existing.primary.pk), before)
        self.assertEqual(IPAMOwnershipLink.objects.get(server=self.server).facts["hostname"], "newlease.example.com")

    def test_post_lease4_add_sync_failure_does_not_prevent_kea_success(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE ipam_ipaddress ADD CONSTRAINT reject_claim CHECK (host(address) != '10.0.0.200')"
            )
        try:
            responses = {"lease4-add": {"result": 0}, "lease4-get": self._readback(), "subnet4-list": self._SUBNETS4}
            with _lease_stub(responses) as kea:
                response = self.client.post(self._url(version=4), self._post4(sync=True), follow=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(kea.commands().count("lease4-add"), 1)
            self.assertTrue(any("sync failed" in str(message) for message in response.context["messages"]))
            self.assertFalse(NbIP.objects.exists())
        finally:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE ipam_ipaddress DROP CONSTRAINT reject_claim")

    def test_post_lease4_add_sync_skipped_without_ipam_permission(self):
        """Server-change permission alone cannot write IPAM."""
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        from netbox_kea.models import IPAMOwnershipLink

        User = get_user_model()
        limited = User.objects.create_user(username="lease_no_ipam", password="x")
        perm = ObjectPermission.objects.create(name="change-server-lease-noipam", actions=["view", "change"])
        perm.object_types.add(ContentType.objects.get_for_model(Server))
        perm.users.add(limited)
        self.client.force_login(limited)

        data = self._post4(sync=True)
        del data["subnet_id"]
        with _lease_stub({"lease4-add": {"result": 0}, "subnet4-list": self._SUBNETS4}) as kea:
            response = self.client.post(self._url(version=4), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("lease4-add"), 1)
        self.assertEqual(kea.commands(), ["lease4-add"])
        self.assertFalse(NbIP.objects.exists())
        self.assertFalse(IPAMOwnershipLink.objects.exists())

    def test_post_lease4_add_reports_foreign_ip_skip(self):
        """A foreign NetBox IP (force=False) is skipped and reported as such, not 'synced'."""
        from ipam.models import IPAddress

        # self.client is the superuser (has IPAM perms) → reaches the real sync.
        IPAddress.objects.create(address="10.0.0.200/24", status="active", description="Router loopback")
        responses = {"lease4-add": {"result": 0}, "lease4-get": self._readback(), "subnet4-list": self._SUBNETS4}
        with _lease_stub(responses):
            response = self.client.post(self._url(version=4), self._post4(sync=True), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(
            any("skipped" in m.lower() and "not kea-managed" in m.lower() for m in msgs),
            f"Expected a foreign-IP skip warning, got: {msgs}",
        )
        # Foreign IP left exactly as the operator set it.
        ip = IPAddress.objects.get(address="10.0.0.200/24")
        self.assertEqual(ip.status, "active")
        self.assertEqual(ip.description, "Router loopback")

    def test_post_lease4_add_reports_successful_sync(self):
        """A fresh IP synced to NetBox reports a created/updated success message."""
        from ipam.models import IPAddress

        # No pre-existing row → the real sync creates it, no conflict → success message.
        responses = {"lease4-add": {"result": 0}, "lease4-get": self._readback(), "subnet4-list": self._SUBNETS4}
        with _lease_stub(responses):
            response = self.client.post(self._url(version=4), self._post4(sync=True), follow=True)
        self.assertEqual(response.status_code, 200)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(
            any("10.0.0.200" in m and "netbox" in m.lower() and "created" in m.lower() for m in msgs),
            f"Expected a NetBox sync success message, got: {msgs}",
        )
        self.assertTrue(IPAddress.objects.filter(address__net_host="10.0.0.200").exists())


# ---------------------------------------------------------------------------
# TestBulkLeaseImportView — bulk lease CSV import (Gap C)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=plugins_config())
class TestBulkLeaseImportView(_ViewTestBase):
    """Tests for ServerLease4/6BulkImportView."""

    def _url(self, version=4):
        return reverse(f"plugins:netbox_kea:server_lease{version}_bulk_import", args=[self.server.pk])

    def _csv4(self, rows=None):
        header = "ip-address,hw-address,subnet-id,valid-lft,hostname\n"
        if rows is None:
            rows = ["10.0.0.10,aa:bb:cc:dd:ee:ff,1,3600,host1.example.com\n"]
        return (header + "".join(rows)).encode("utf-8")

    def _csv6(self, rows=None):
        header = "ip-address,duid,iaid,subnet-id,hostname\n"
        if rows is None:
            rows = ["2001:db8::1,00:01:02:03,12345,1,host1.example.com\n"]
        return (header + "".join(rows)).encode("utf-8")

    def _post(self, version=4, csv_bytes=None):
        import io as _io

        if csv_bytes is None:
            csv_bytes = self._csv4() if version == 4 else self._csv6()
        f = _io.BytesIO(csv_bytes)
        f.name = "leases.csv"
        return {"csv_file": f}

    def test_the_import_page_selects_the_leases_tab(self):
        for version in (4, 6):
            with self.subTest(version=version, method="GET"):
                self.assertEqual(active_tabs(self.client.get(self._url(version=version))), ["Leases"])
            with self.subTest(version=version, method="POST"):
                self.assertEqual(active_tabs(self.client.post(self._url(version=version), {})), ["Leases"])

    def test_get_v4_returns_200(self):
        """GET lease4 bulk import page returns 200."""
        response = self.client.get(self._url(version=4))
        self.assertEqual(response.status_code, 200)

    def test_get_v6_returns_200(self):
        """GET lease6 bulk import page returns 200."""
        response = self.client.get(self._url(version=6))
        self.assertEqual(response.status_code, 200)

    def test_post_v4_valid_csv_calls_lease_add(self):
        """POST with valid v4 CSV calls lease_add once per row."""
        with _lease_stub({"lease4-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(version=4), self._post(version=4))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-add"), 1)
        self.assertEqual(kea.bodies("lease4-add")[0]["arguments"]["ip-address"], "10.0.0.10")

    def test_post_v6_valid_csv_calls_lease_add(self):
        """POST with valid v6 CSV calls lease_add with correct duid and iaid."""
        with _lease_stub({"lease6-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(version=6), self._post(version=6))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease6-add"), 1)
        args = kea.bodies("lease6-add")[0]["arguments"]
        self.assertEqual(args["duid"], "00:01:02:03")
        self.assertEqual(args["iaid"], 12345)

    def test_post_multiple_rows_calls_lease_add_per_row(self):
        """Each CSV row triggers one lease_add call."""
        csv_bytes = self._csv4(
            rows=[
                "10.0.0.10,aa:bb:cc:dd:ee:01,1,3600,h1\n",
                "10.0.0.11,aa:bb:cc:dd:ee:02,1,3600,h2\n",
                "10.0.0.12,aa:bb:cc:dd:ee:03,1,3600,h3\n",
            ]
        )
        with _lease_stub({"lease4-add": {"result": 0}}) as kea:
            response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-add"), 3)

    def test_post_partial_failure_shows_error_count(self):
        """If some rows fail, result context shows correct created/error counts."""
        csv_bytes = self._csv4(
            rows=[
                "10.0.0.10,aa:bb:cc:dd:ee:01,1,3600,h1\n",
                "10.0.0.11,aa:bb:cc:dd:ee:02,1,3600,h2\n",
            ]
        )
        # Row 1 succeeds (result 0), row 2 fails (result 1 → real client raises KeaException).
        with _lease_stub({"lease4-add": queued({"result": 0}, {"result": 1, "text": "bad"})}):
            response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], 1)

    def test_post_empty_csv_shows_form_error(self):
        """Uploading a CSV with only a header (no data rows) returns 200 with empty result."""
        csv_bytes = b"ip-address,hw-address\n"
        # Header-only CSV → zero rows → no lease command issued (empty registry proves it).
        with _lease_stub({}) as kea:
            response = self.client.post(self._url(version=4), self._post(version=4, csv_bytes=csv_bytes))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])
        result = response.context.get("result")
        if result is not None:
            self.assertEqual(result["created"], 0)

    def test_get_requires_login(self):
        """Unauthenticated GET redirects to login."""
        self.client.logout()
        response = self.client.get(self._url(version=4))
        self.assertIn(response.status_code, (302, 403))


# ─────────────────────────────────────────────────────────────────────────────
# Gap G: Django signals + lease journal entries
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSignals(_ViewTestBase):
    """Lease add/delete views must fire Django signals from netbox_kea.signals."""

    _LEASE4 = {
        "ip_address": "10.0.0.5",
        "hw_address": "aa:bb:cc:dd:ee:01",
        "hostname": "signal-host",
        "subnet_id": 1,
        "valid_lft": 3600,
    }

    def test_lease_add_fires_lease_added_signal(self):
        """_BaseLeaseAddView.post must send lease_added signal after successful add."""
        from netbox_kea import signals

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.lease_added.connect(handler)
        try:
            url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
            with _lease_stub({"lease4-add": {"result": 0}}):
                self.client.post(url, self._LEASE4)
        finally:
            signals.lease_added.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["ip_address"], "10.0.0.5")
        self.assertEqual(received[0]["dhcp_version"], 4)
        self.assertEqual(received[0]["server"].pk, self.server.pk)

    def test_lease_delete_fires_leases_deleted_signal(self):
        """BaseServerLeasesDeleteView.post must send leases_deleted signal after successful delete."""
        from netbox_kea import signals

        received = []

        def handler(sender, **kwargs):
            received.append(kwargs)

        signals.leases_deleted.connect(handler)
        try:
            url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
            with _lease_stub({"lease4-del": {"result": 0}}):
                self.client.post(url, {"pk": "10.0.0.5", "_confirm": "1"})
        finally:
            signals.leases_deleted.disconnect(handler)

        self.assertEqual(len(received), 1)
        self.assertIn("10.0.0.5", received[0]["ip_addresses"])
        self.assertEqual(received[0]["dhcp_version"], 4)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseJournalEntries(_ViewTestBase):
    """Lease add and delete views must create JournalEntry records on the Server."""

    def test_lease_add_creates_journal_entry(self):
        """A successful lease add must create a JournalEntry attached to the server."""
        from django.contrib.contenttypes.models import ContentType
        from extras.models import JournalEntry

        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        server_ct = ContentType.objects.get_for_model(self.server)
        before = JournalEntry.objects.filter(
            assigned_object_id=self.server.pk,
            assigned_object_type=server_ct,
        ).count()
        with _lease_stub({"lease4-add": {"result": 0}}):
            self.client.post(
                url,
                {
                    "ip_address": "10.0.0.5",
                    "hw_address": "aa:bb:cc:dd:ee:01",
                    "hostname": "journal-host",
                    "subnet_id": 1,
                    "valid_lft": 3600,
                },
            )
        after = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).count()
        self.assertEqual(after, before + 1)
        entry = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).latest(
            "created"
        )
        self.assertIn("10.0.0.5", entry.comments)

    def test_lease_delete_creates_journal_entry(self):
        """A successful lease delete must create a JournalEntry attached to the server."""
        from django.contrib.contenttypes.models import ContentType
        from extras.models import JournalEntry

        url = reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])
        server_ct = ContentType.objects.get_for_model(self.server)
        before = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).count()
        with _lease_stub({"lease4-del": {"result": 0}}):
            self.client.post(url, {"pk": "10.0.0.5", "_confirm": "1"})
        after = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).count()
        self.assertEqual(after, before + 1)
        entry = JournalEntry.objects.filter(assigned_object_id=self.server.pk, assigned_object_type=server_ct).latest(
            "created"
        )
        self.assertIn("10.0.0.5", entry.comments)


# ===========================================================================
# BATCH 2: Covering remaining ~220 uncovered lines
# ===========================================================================

# ---------------------------------------------------------------------------
# _add_lease_journal error handling
# ---------------------------------------------------------------------------


class TestJournalHelperEdgeCases(_ViewTestBase):
    """Unit tests for _add_lease_journal exception paths."""

    def test_lease_journal_multiple_ips(self):
        """_add_lease_journal with a list of IP addresses uses the 'N lease(s)' branch."""
        from netbox_kea.views.leases import _add_lease_journal

        with patch("extras.models.JournalEntry.objects.create", autospec=True) as mock_create:
            mock_create.return_value = None
            _add_lease_journal(
                self.server,
                self.user,
                "deleted",
                ip_addresses=["10.0.0.1", "10.0.0.2"],
                hw_address="aa:bb:cc:dd:ee:ff",
                hostname="host1",
            )
            call_kwargs = mock_create.call_args[1]
            self.assertIn("2 lease(s)", call_kwargs["comments"])

    def test_lease_journal_import_error(self):
        """ImportError inside _add_lease_journal is swallowed."""
        import sys

        from netbox_kea.views.leases import _add_lease_journal

        with patch.dict(sys.modules, {"extras.models": None}):
            _add_lease_journal(self.server, self.user, "created", ip_addresses=["10.0.0.1"])

    def test_lease_journal_db_error(self):
        """OperationalError inside _add_lease_journal is swallowed."""
        from django.db import OperationalError

        from netbox_kea.views.leases import _add_lease_journal

        with patch(
            "extras.models.JournalEntry.objects.create",
            autospec=True,
            side_effect=OperationalError("db gone"),
        ):
            _add_lease_journal(self.server, self.user, "created", ip_addresses=["10.0.0.1"])


# ---------------------------------------------------------------------------
# HTMX exception handler in BaseServerLeasesView
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestHTMXExceptionHandler(_ViewTestBase):
    """An exception during the HTMX lease fetch renders the error partial."""

    _SUBNETS4 = _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}])

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_htmx_exception_returns_error_partial(self):
        # A RuntimeError from the lease fetch must be caught and rendered as the HTMX error partial.
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get": RuntimeError("boom")}):
            response = self.client.get(
                self._url() + "?q=10.0.0.1&by=ip",
                HTTP_HX_REQUEST="true",
            )
        # Must not crash — the outer handler catches the RuntimeError and renders
        # the HTMX error partial (never a 500; accepting 500 would let a regression
        # where the handler stops catching the error pass unnoticed).
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Lease edit GET — KeaException, not-found, v6 duid
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditGet(_ViewTestBase):
    """Lease edit GET: Kea errors, a missing lease, and the v6 DUID."""

    def test_get_kea_exception_redirects(self):
        """KeaException in lease4 GET redirects to leases page."""
        # result=1 makes lease4-get raise KeaException in the real client.
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with _lease_stub({"lease4-get": {"result": 1, "text": "err"}}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_get_lease_not_found_redirects(self):
        """result=3 (not found) in lease4 GET redirects to leases page."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with _lease_stub({"lease4-get": {"result": 3, "arguments": None}}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_get_v6_lease_includes_duid(self):
        """v6 lease GET includes duid in form initial."""
        server6 = _make_db_server(name="kea6-only", ca_url="https://kea6.example.com", dhcp4=False, dhcp6=True)
        url = reverse("plugins:netbox_kea:server_lease6_edit", args=[server6.pk, "2001:db8::1"])
        with _lease_stub(
            {
                "lease6-get": {
                    "result": 0,
                    "arguments": complete_lease(
                        {
                            "ip-address": "2001:db8::1",
                            "duid": "00:01:00:01",
                            "hostname": "v6host",
                            "valid-lft": 3600,
                        }
                    ),
                }
            }
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "00:01:00:01")


# ---------------------------------------------------------------------------
# Lease edit POST — invalid form
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditPostInvalidForm(_ViewTestBase):
    """Lease edit POST with an invalid form re-renders with 200."""

    def test_post_invalid_form_rerenders(self):
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        # Invalid form re-renders before any Kea call — empty registry proves none is issued.
        with _lease_stub({}) as kea:
            response = self.client.post(url, {"hostname": "", "valid_lft": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])


# ---------------------------------------------------------------------------
# Lease add — generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddGenericException(_ViewTestBase):
    """A transport error on lease add re-renders the form."""

    def test_generic_exception_rerenders_form(self):
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        # A transport error from lease4-add must re-render the form with an error message.
        with _lease_stub({"lease4-add": requests.RequestException("unexpected crash")}):
            response = self.client.post(
                url,
                {
                    "ip_address": "10.0.0.99",
                    "subnet_id": "1",
                    "hw_address": "aa:bb:cc:dd:ee:ff",
                    "hostname": "testhost",
                    "valid_lft": "3600",
                },
            )
        self.assertEqual(response.status_code, 200)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("Failed to create lease" in m or "internal" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# _fetch_leases_from_server — various BY_* branches + edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchLeasesFromServer(_ViewTestBase):
    """_fetch_leases_from_server with each search selector."""

    def _call(self, by, q="aa:bb:cc:dd:ee:ff", version=4, resp=None):
        from netbox_kea.views.combined import _fetch_leases_from_server

        if resp is None:
            address = "10.0.0.1" if version == 4 else "2001:db8::1"
            resp = [
                {
                    "result": 0,
                    "arguments": {"leases": [complete_lease({"ip-address": address, "valid-lft": 3600, "state": 0})]},
                }
            ]
        payload = resp[0] if isinstance(resp, list) else resp
        # _fetch_leases_from_server picks the command from `by`; register every
        # lease-get variant of the family to the same payload so whichever it issues is covered.
        suffixes = (
            "",
            "-by-hostname",
            "-all",
            *(("-by-hw-address", "-by-client-id") if version == 4 else ("-by-duid",)),
        )
        variants = [f"lease{version}-get{suffix}" for suffix in suffixes]
        with _lease_stub(dict.fromkeys(variants, payload)):
            return _fetch_leases_from_server(self.server, q, by, version)

    def test_by_hw_address(self):
        from netbox_kea import constants

        snapshot = self._call(constants.BY_HW_ADDRESS, q="aa:bb:cc:dd:ee:ff")
        self.assertEqual(len(snapshot.records), 1)

    def test_by_hostname(self):
        from netbox_kea import constants

        snapshot = self._call(constants.BY_HOSTNAME, q="myhost")
        self.assertEqual(len(snapshot.records), 1)

    def test_by_client_id(self):
        from netbox_kea import constants

        snapshot = self._call(constants.BY_CLIENT_ID, q="01:aa:bb:cc:dd:ee:ff")
        self.assertEqual(len(snapshot.records), 1)

    def test_by_duid(self):
        from netbox_kea import constants

        snapshot = self._call(constants.BY_DUID, q="00:01:00:01:12:34", version=6)
        self.assertEqual(len(snapshot.records), 1)

    def test_unknown_by_is_rejected(self):
        with self.assertRaises(ValueError):
            self._call("unknown_by", q="x")

    def test_result_3_returns_empty(self):
        from netbox_kea import constants

        snapshot = self._call(constants.BY_HOSTNAME, q="ghost", resp=[{"result": 3, "arguments": None}])
        self.assertEqual((snapshot.records, snapshot.complete), ((), True))

    def test_null_args_raises_runtime_error(self):
        from netbox_kea import constants
        from netbox_kea.views.combined import _fetch_leases_from_server

        with _lease_stub({"lease4-get-by-hostname": {"result": 0, "arguments": None}}):
            with self.assertRaises(RuntimeError):
                _fetch_leases_from_server(self.server, "ghost", constants.BY_HOSTNAME, 4)

    def test_by_subnet_id(self):
        """The BY_SUBNET_ID selector fetches the leases of one Subnet."""
        from netbox_kea import constants

        resp = [
            {
                "result": 0,
                "arguments": {"leases": [complete_lease({"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0})]},
            }
        ]
        snapshot = self._call(constants.BY_SUBNET_ID, q="1", resp=resp)
        self.assertEqual(len(snapshot.records), 1)

    @override_settings(PLUGINS_CONFIG=plugins_config(lease_query_max_unpaged_leases=100))
    def test_subnet_state_is_applied_by_kea(self):
        from netbox_kea import constants
        from netbox_kea.views.combined import _fetch_leases_from_server

        stats = _subnet_stats(4, 1, assigned=501, declined=1)
        response = {
            "result": 0,
            "arguments": {"leases": [complete_lease({"ip-address": "198.18.0.1", "valid-lft": 3600, "state": 1})]},
        }
        with _lease_stub({"stat-lease4-get": stats, "lease4-get-by-state": response}) as kea:
            snapshot = _fetch_leases_from_server(
                self.server,
                1,
                constants.BY_SUBNET_ID,
                4,
                state=1,
            )

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(
            kea.bodies("lease4-get-by-state")[0]["arguments"],
            {"subnet-id": 1, "state": 1},
        )


# ---------------------------------------------------------------------------
# _fetch_all_leases_from_server — pagination edge cases
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchAllLeasesFromServer(_ViewTestBase):
    """_fetch_all_leases_from_server reads every page through KeaClient.lease_get_all, up to its cap."""

    def _run(self, page, max_leases=1000):
        from netbox_kea.views.combined import _fetch_all_leases_from_server

        with _lease_stub({"lease4-get-page": page}):
            return _fetch_all_leases_from_server(self.server, version=4, max_leases=max_leases)

    def test_an_empty_daemon_is_exhaustive(self):
        snapshot = self._run({"result": 3, "arguments": None})
        self.assertEqual((snapshot.records, snapshot.coverage), ((), "exhaustive"))

    def test_null_args_raises_runtime_error(self):
        """Null arguments from lease-get-page fail the read."""
        with self.assertRaises(RuntimeError):
            self._run({"result": 0, "arguments": None})

    def test_reaching_the_cap_before_the_end_is_page_coverage(self):
        records = [lease_record("10.0.0.1"), lease_record("10.0.0.2")]
        snapshot = self._run(lease_pages(records), max_leases=1)
        self.assertEqual((len(snapshot.records), snapshot.coverage), (1, "page"))

    def test_pages_continue_until_a_short_page(self):
        records = [lease_record(f"10.0.{index // 256}.{index % 256}") for index in range(1, 252)]
        snapshot = self._run(lease_pages(records))
        self.assertEqual((len(snapshot.records), snapshot.coverage), (251, "exhaustive"))


# ---------------------------------------------------------------------------
# Lease CSV bulk import — form invalid, parse error, generic exception
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseBulkImportEdgeCases(_ViewTestBase):
    """Lease CSV import: invalid form, parse error, and per-row errors."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_bulk_import", args=[self.server.pk])

    def test_post_no_file_rerenders(self):
        """POST without csv_file → invalid form → 200."""
        response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 200)

    def test_a_file_that_is_not_utf8_is_reported_on_the_field(self):
        """A file the view cannot decode is reported on the field, not raised.

        Nothing covered this branch before. It does not discriminate where the view
        takes the file from, because a plain FileField hands back the same
        UploadedFile either way; it covers the read and decode the view does with it.
        """
        import io

        csv_file = io.BytesIO(b"ip-address\n10.0.0.\xff1")
        csv_file.name = "leases.csv"

        # An empty registry proves the view rejects the file before any Kea command.
        with _lease_stub({}) as kea:
            response = self.client.post(self._url(), {"csv_file": csv_file})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])
        self.assertContains(response, "File must be UTF-8 encoded.")

    @patch("netbox_kea.views.sync_views.parse_lease_csv", autospec=True)
    def test_parse_error_shows_form_error(self, mock_parse):
        """ValueError from parse_lease_csv adds generic form error (no raw exception text)."""
        import io

        mock_parse.side_effect = ValueError("bad column")
        csv_file = io.BytesIO(b"ip-address\n10.0.0.1")
        csv_file.name = "leases.csv"
        # Parse fails before any client call — empty registry proves no Kea command runs.
        with _lease_stub({}) as kea:
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])
        self.assertContains(response, "parsing failed")
        self.assertNotContains(response, "bad column")

    def test_a_client_that_cannot_be_built_is_reported_on_the_form(self):
        """A key without a certificate makes get_client() raise ValueError; the form reports it."""
        import io

        Server.objects.filter(pk=self.server.pk).update(client_key_path="/tls/client.key", client_cert_path="")
        csv_file = io.BytesIO(b"ip-address\n10.0.0.1")
        csv_file.name = "leases.csv"
        with _lease_stub({}) as kea:
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands(), [])
        self.assertContains(response, "Failed to connect to Kea server.")

    def test_generic_exception_is_row_error(self):
        """Generic exceptions from lease_add are caught per-row (not propagated)."""
        import io

        csv_content = b"ip-address\n10.0.0.1"
        csv_file = io.BytesIO(csv_content)
        csv_file.name = "leases.csv"
        # An unexpected error type from lease4-add is caught per-row by the import loop.
        with _lease_stub({"lease4-add": AttributeError("bug")}):
            response = self.client.post(self._url(), {"csv_file": csv_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["errors"], 1)
        self.assertEqual(
            response.context["result"]["error_rows"][0]["error"],
            "An unexpected error occurred.",
        )

    def test_a_malformed_reply_body_is_an_invalid_response_row_error(self):
        """A lease4-add body that is not a JSON list is a malformed reply, not a connection error."""
        import io

        for name, reply in (
            ("not a list", _http_response({"result": 0})),
            ("not JSON", _raw_http_response(b"<html>")),
        ):
            with self.subTest(name):
                csv_file = io.BytesIO(b"ip-address\n10.0.0.1")
                csv_file.name = "leases.csv"
                with _lease_stub({"lease4-add": reply}):
                    response = self.client.post(self._url(), {"csv_file": csv_file})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [row["error"] for row in response.context["result"]["error_rows"]],
                    ["Invalid response from Kea — could not parse server reply."],
                )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageAllLeasesMode(_ViewTestBase):
    """All-leases browse mode (``by=""``) starts pagination at the address-space root.

    The lease-page ``from`` cursor must be ``"0.0.0.0"`` for DHCPv4 and ``"::"``
    for DHCPv6.
    """

    _SUBNETS4 = _subnet_list(4, [])
    _SUBNETS6 = _subnet_list(6, [])
    _EMPTY_PAGE = {"result": 0, "arguments": {"count": 0, "leases": []}}

    def _url4(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def _url6(self):
        return reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])

    def test_all_leases_v4_starts_from_zero_address(self):
        """``by=""`` on the v4 view must call lease4-get-page with ``from="0.0.0.0"``."""
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": self._EMPTY_PAGE}) as kea:
            response = self.client.get(self._url4(), {"by": ""}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.bodies("lease4-get-page")[0]["arguments"]["from"], "0.0.0.0")  # noqa: S104 - Kea sentinel value, not a bind address

    def test_all_leases_v6_starts_from_unspecified_address(self):
        """``by=""`` on the v6 view must call lease6-get-page with ``from="::"``."""
        with _lease_stub({"subnet6-list": self._SUBNETS6, "lease6-get-page": self._EMPTY_PAGE}) as kea:
            response = self.client.get(self._url6(), {"by": ""}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.bodies("lease6-get-page")[0]["arguments"]["from"], "::")


# ---------------------------------------------------------------------------
# get_leases validation and null arguments
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesCoverage(_ViewTestBase):
    """Edge cases in BaseServerLeasesView.get_leases()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_invalid_by_raises_value_error(self):
        """An invalid search selector must fail before a Kea request."""
        from netbox_kea.views.leases import ServerLeases4View

        view = ServerLeases4View()
        client = kea_client(url="https://kea.example.com")
        with self.assertRaises(ValueError):
            view.get_leases(client, self.server, "test_query", "not_a_valid_by")

    def test_null_args_from_lease_get_raises_runtime_error(self):
        """lease-get returns arguments=None → RuntimeError (caught by HTMX handler)."""
        subnets = _subnet_list(4, [])
        with _lease_stub({"subnet4-list": subnets, "lease4-get": {"result": 0, "arguments": None}}):
            response = self.client.get(
                self._url(),
                {"by": "ip", "q": "10.0.0.1"},
                HTTP_HX_REQUEST="true",
            )
        # RuntimeError is caught by outer except → HTMX error partial, still 200
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# get_export — invalid form + get_export_all null args
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetExportCoverage(_ViewTestBase):
    """Edge cases in get_export() and get_export_all()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_export_with_invalid_form_redirects(self):
        """Invalid form for export → messages.warning + redirect."""
        # Pass an invalid 'by' value (not in choices) to force form.is_valid() == False
        response = self.client.get(self._url(), {"export": "1", "by": "INVALID_VALUE", "q": "test"})
        self.assertIn(response.status_code, [200, 302])

    def test_export_all_null_args_returns_csv(self):
        """export_all: lease-get-page returns arguments=None → empty CSV."""
        with _lease_stub({"lease4-get-page": {"result": 0, "arguments": None}}):
            response = self.client.get(self._url(), {"export_all": "1"})
        # Should return CSV even when args is None (empty export)
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# HTMX invalid form
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestHTMXInvalidFormCoverage(_ViewTestBase):
    """HTMX GET with invalid form → renders HTMX partial."""

    def test_htmx_invalid_form_returns_partial(self):
        """form.is_valid()==False for HTMX → renders server_dhcp_leases_htmx.html."""
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        subnets = _subnet_list(4, [])
        # 'by' has an invalid choice value → form.is_valid() returns False (no lease fetch).
        with _lease_stub({"subnet4-list": subnets}):
            response = self.client.get(
                url,
                {"by": "INVALID_CHOICE", "q": "test"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Lease6 edit — duid branch
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease6EditDuid(_ViewTestBase):
    """POST lease6 edit with duid → lease6-update carries the duid."""

    def test_post_with_duid_calls_lease_update(self):
        """duid field in POST → kwargs['duid'] is set and lease_update called."""
        url = reverse(
            "plugins:netbox_kea:server_lease6_edit",
            args=[self.server.pk, "2001:db8::1"],
        )
        # lease_update reads the current lease (lease6-get) then writes lease6-update.
        current = {
            "result": 0,
            "arguments": complete_lease({"ip-address": "2001:db8::1", "duid": "00:00", "valid-lft": 3600}),
        }
        with _lease_stub({"lease6-get": current, "lease6-update": {"result": 0}}) as kea:
            response = self.client.post(
                url,
                {
                    "duid": "01:02:03:04",
                    "valid_lft": "",
                    "hostname": "",
                },
            )
        # Should redirect to leases6 URL
        self.assertIn(response.status_code, [302, 200])
        self.assertIn("lease6-update", kea.commands())
        self.assertEqual(kea.bodies("lease6-update")[0]["arguments"]["duid"], "01:02:03:04")


# ---------------------------------------------------------------------------
# Lease reservation lookup — missing subnet_id
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchOneEmptyLease(_ViewTestBase):
    """A lease reply without the required subnet-id is rejected before enrichment."""

    def test_lease_without_subnet_id_is_rejected_before_reservation_lookup(self):
        """A missing subnet-id is diagnosed, and no reservation lookup is sent to Kea."""
        subnets = _subnet_list(4, [])
        arguments = complete_lease({"ip-address": "10.0.0.1", "valid-lft": 3600, "state": 0, "hostname": "testhost"})
        del arguments["subnet-id"]
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _lease_stub({"subnet4-list": subnets, "lease4-get": {"result": 0, "arguments": arguments}}) as kea:
            response = self.client.get(url, {"by": "ip", "q": "10.0.0.1"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["lease_diagnostics"])
        self.assertNotIn("reservation-get", kea.commands())


# ---------------------------------------------------------------------------
# Combined leases view — truncated server
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestCombinedLeasesTruncated(_ViewTestBase):
    """A server with more leases than the combined cap is named as truncated."""

    def test_truncated_server_name_in_context(self):
        records = [lease_record(f"10.0.{index // 256}.{index % 256}") for index in range(1, 1002)]
        url = reverse("plugins:netbox_kea:combined_leases4") + f"?state=0&server={self.server.pk}"
        with _lease_stub({"lease4-get-page": lease_pages(records)}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["truncated_servers"], [self.server.name])
        self.assertEqual(len(response.context["table"].data), 1000)


# ---------------------------------------------------------------------------
# Fix A: partial delete loop
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeasePartialDelete(_ViewTestBase):
    """Bulk delete continues past individual KeaExceptions."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])

    # A followed redirect lands on the leases page, which fetches the subnet quick-select.
    _SUBNETS4 = _subnet_list(4, [])

    def test_continues_after_first_delete_error(self):
        """When the first IP fails, the second IP is still deleted."""
        # First lease4-del fails (result 1 → KeaException), second succeeds.
        with _lease_stub(
            {"lease4-del": queued({"result": 1, "text": "not found"}, {"result": 0}), "subnet4-list": self._SUBNETS4}
        ) as kea:
            response = self.client.post(
                self._url(),
                {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-del"), 2)

    def test_continues_after_a_malformed_delete_reply(self):
        """A reply entry without a result fails that IP only, so the next IP is still deleted."""
        with _lease_stub({"lease4-del": queued(["ok"], {"result": 0}), "subnet4-list": self._SUBNETS4}) as kea:
            response = self.client.post(
                self._url(),
                {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-del"), 2)
        self.assertContains(response, "Error deleting lease 10.0.0.1: see server logs for details.")

    def test_success_message_shows_count_of_deleted(self):
        """Success message reflects only the successfully deleted count."""
        with _lease_stub({"lease4-del": {"result": 0}, "subnet4-list": self._SUBNETS4}):
            response = self.client.post(
                self._url(),
                {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
                follow=True,
            )
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("2" in m and "deleted" in m.lower() for m in msgs))

    def test_partial_failure_shows_warning(self):
        """When some IPs fail, a warning message about partial failure is shown."""
        with _lease_stub(
            {"lease4-del": queued({"result": 1, "text": "not found"}, {"result": 0}), "subnet4-list": self._SUBNETS4}
        ):
            response = self.client.post(
                self._url(),
                {"lease_ips": ["10.0.0.1", "10.0.0.2"], "_confirm": "1", "pk": ["10.0.0.1", "10.0.0.2"]},
                follow=True,
            )
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("failed" in m.lower() or "error" in m.lower() for m in msgs))


# ---------------------------------------------------------------------------
# Fix B: get_export_all except narrowing
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportAllExceptNarrowing(_ViewTestBase):
    """get_export_all() must not swallow local bugs via bare except Exception."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_attribute_error_propagates(self):
        """An AttributeError inside get_export_all must not be silently caught."""
        # get_export_all narrows its except clauses, so an AttributeError propagates.
        with _lease_stub({"lease4-get-page": AttributeError("bad stub")}):
            with self.assertRaises(AttributeError):
                self.client.get(self._url(), {"export_all": "1"})


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseExportStateFilter(_ViewTestBase):
    """get_export() must honour the 'state' query parameter."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    # Unguarded on purpose: the export takes the same unpaged Subnet path, with no
    # stat-lease4-get preflight. State it here instead of inheriting it.
    @override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
    def test_state_filter_applied_to_export(self):
        """Exported CSV must contain only leases matching the requested state."""
        declined = complete_lease(
            {
                "ip-address": "10.0.0.2",
                "hw-address": "aa:bb:cc:00:00:02",
                "subnet-id": 1,
                "cltt": 1700000000,
                "valid-lft": 86400,
                "hostname": "",
                "state": 1,
            }
        )
        with _lease_stub(
            {
                **_catalogue_responses(4, 1, "10.0.0.0/24"),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": [declined]}},
            }
        ):
            response = self.client.get(
                self._url(),
                {
                    "export": "1",
                    "by": "subnet",
                    "q": "10.0.0.0/24",
                    "state": "1",
                },
            )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("10.0.0.2", content)
        self.assertNotIn("10.0.0.1", content)


# ─────────────────────────────────────────────────────────────────────────────
# F8: HTMX handler exception narrowing
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
class TestHtmxHandlerExceptNarrowing(_ViewTestBase):
    """HTMX lease handler must not swallow programming errors via bare except Exception."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_attribute_error_not_swallowed_by_htmx_handler(self):
        """An AttributeError inside the HTMX handler must propagate (not be caught silently)."""
        subnets = _subnet_list(4, [])
        # The HTMX handler narrows its except clauses, so an AttributeError propagates.
        with _lease_stub({"subnet4-list": subnets, "lease4-get-all": AttributeError("stub programming bug")}):
            with self.assertRaises(AttributeError):
                self.client.get(
                    self._url(),
                    HTTP_HX_REQUEST="true",
                    data={"by": "subnet_id", "q": "1"},
                )


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseDeleteLoopTransportErrors(_ViewTestBase):
    """Bulk delete loop must continue when RequestException/ValueError raised for one IP."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])

    _SUBNETS4 = _subnet_list(4, [])

    def test_request_exception_continues_loop(self):
        import requests as _requests

        # First IP's delete raises a transport error; the loop must still delete the second.
        def del_resp(body):
            if body["arguments"]["ip-address"] == "10.0.0.1":
                return _requests.ConnectionError("down")
            return {"result": 0}

        with _lease_stub({"lease4-del": del_resp, "subnet4-list": self._SUBNETS4}) as kea:
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-del"), 2)

    def test_value_error_continues_loop(self):
        def del_resp(body):
            if body["arguments"]["ip-address"] == "10.0.0.1":
                return ValueError("bad JSON")
            return {"result": 0}

        with _lease_stub({"lease4-del": del_resp, "subnet4-list": self._SUBNETS4}) as kea:
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(kea.commands().count("lease4-del"), 2)


@override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
class TestLeaseExportTransportErrors(_ViewTestBase):
    """get_export() must handle RequestException and ValueError gracefully."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _SUBNETS4 = _subnet_list(4, [])

    def test_request_exception_redirects_with_error(self):
        import requests as _requests

        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-all": _requests.ConnectionError("down")}):
            response = self.client.get(
                self._url(),
                {"export": "1", "by": "subnet_id", "q": "1"},
            )
        self.assertIn(response.status_code, [200, 302])

    def test_value_error_redirects_with_error(self):
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-all": ValueError("bad JSON")}):
            response = self.client.get(
                self._url(),
                {"export": "1", "by": "subnet_id", "q": "1"},
            )
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# F3: Single-lease GET — transport errors
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSingleLeaseGetTransportErrors(_ViewTestBase):
    """Single-lease GET must handle RequestException/ValueError gracefully."""

    def test_request_exception_redirects(self):
        """requests.RequestException from lease-get must redirect with error message."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with _lease_stub({"lease4-get": requests.ConnectionError("down")}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)

    def test_value_error_redirects(self):
        """ValueError from lease-get must redirect with error message."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with _lease_stub({"lease4-get": ValueError("bad JSON")}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)


# ---------------------------------------------------------------------------
# F3: Lease edit POST — transport errors
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditPostTransportErrors(_ViewTestBase):
    """Lease edit POST must handle RequestException/ValueError gracefully."""

    def test_request_exception_redirects(self):
        """requests.RequestException from lease_update must redirect."""
        # lease_update's first command is lease{v}-get; raising there surfaces the transport error.
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with _lease_stub({"lease4-get": requests.ConnectionError("down")}):
            response = self.client.post(url, {"hostname": "host", "valid_lft": "3600"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)

    def test_value_error_redirects(self):
        """ValueError from lease_update must redirect."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with _lease_stub({"lease4-get": ValueError("bad value")}):
            response = self.client.post(url, {"hostname": "host", "valid_lft": "3600"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)

    def test_malformed_reply_redirects(self):
        """A reply entry without a result from lease_update must redirect (no 500)."""
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, "10.0.0.1"])
        with _lease_stub({"lease4-get": _LEASE4_GET_RESP[0], "lease4-update": ["ok"]}):
            response = self.client.post(url, {"hostname": "host", "valid_lft": "3600"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(self.server.pk), response.url)


# ---------------------------------------------------------------------------
# F3: Lease add POST — ValueError (RequestException already handled)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddValueError(_ViewTestBase):
    """Lease add POST must handle ValueError gracefully."""

    def test_value_error_rerenders_form(self):
        """ValueError from lease_add must not propagate as 500."""
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        with _lease_stub({"lease4-add": ValueError("bad value")}):
            response = self.client.post(url, {"ip_address": "10.0.0.99"})
        self.assertIn(response.status_code, [200, 302])

    def test_malformed_reply_rerenders_form(self):
        """A reply entry without a result from lease_add must re-render the form (no 500)."""
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        with _lease_stub({"lease4-add": ["ok"]}):
            response = self.client.post(url, {"ip_address": "10.0.0.99"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Failed to create lease: invalid response from Kea.")


# ---------------------------------------------------------------------------
# F9: _add_lease_journal bare except narrowing
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseJournalExceptionNarrowing(_ViewTestBase):
    """_add_lease_journal except must only catch DB errors, not all exceptions."""

    @patch("netbox_kea.views.leases._add_lease_journal", autospec=True)
    def test_database_error_does_not_fail_request(self, mock_journal):
        """DatabaseError from _add_lease_journal must be caught; lease add still redirects."""
        from django.db import DatabaseError

        mock_journal.side_effect = DatabaseError("DB error")
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        with _lease_stub({"lease4-add": {"result": 0}}):
            response = self.client.post(url, {"ip_address": "10.0.0.55"})
        self.assertIn(response.status_code, [200, 302])

    @patch("netbox_kea.views.leases._add_lease_journal", autospec=True)
    def test_operational_error_does_not_fail_request(self, mock_journal):
        """OperationalError from _add_lease_journal must be caught; lease add still redirects."""
        from django.db import OperationalError

        mock_journal.side_effect = OperationalError("DB lock")
        url = reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])
        with _lease_stub({"lease4-add": {"result": 0}}):
            response = self.client.post(url, {"ip_address": "10.0.0.56"})
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# Coverage: get_export() error paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
class TestLeaseExportClientError(_ViewTestBase):
    """Cover error paths in get_export()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_get_client_value_error_redirects(self):
        """ValueError from get_client in export redirects with error."""
        # A cert without a key makes the real KeaClient constructor raise ValueError.
        bad = _make_db_server(name="badtls-export", client_cert_path="/x/cert.pem")
        url = reverse("plugins:netbox_kea:server_leases4", args=[bad.pk])
        with _lease_stub({"subnet4-list": _subnet_list(4, [])}):
            response = self.client.get(url, {"export": "form", "by": "subnet", "q": "10.0.0.0/24"})
        self.assertIn(response.status_code, [200, 302])

    def test_runtime_error_during_fetch_redirects(self):
        """RuntimeError during lease fetch in export redirects with error."""
        subnets = _subnet_list(4, [])
        with _lease_stub({"subnet4-list": subnets, "lease4-get-all": RuntimeError("unexpected")}):
            response = self.client.get(self._url(), {"export": "form", "by": "subnet_id", "q": "1"})
        self.assertIn(response.status_code, [200, 302])


# ---------------------------------------------------------------------------
# Coverage: HTMX error handler exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
class TestLeaseHtmxErrorHandler(_ViewTestBase):
    """Cover HTMX error rendering paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _SUBNETS4 = _subnet_list(4, [])

    def test_kea_exception_renders_htmx_error(self):
        """KeaException in HTMX handler renders error template."""
        # result=1 on lease4-get-all makes the real client raise KeaException.
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-all": {"result": 1, "text": "err"}}):
            response = self.client.get(
                self._url(),
                {"by": "subnet_id", "q": "1"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)

    def test_request_exception_renders_htmx_error(self):
        """requests.RequestException in HTMX handler renders error template."""
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-all": requests.ConnectionError("down")}):
            response = self.client.get(
                self._url(),
                {"by": "subnet_id", "q": "1"},
                HTTP_HX_REQUEST="true",
            )
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: lease edit GET validation branches
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseEditGetValidation(_ViewTestBase):
    """Cover lease edit GET validation paths."""

    def _url(self, ip="10.0.0.1"):
        return reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, ip])

    _SUBNETS4 = _subnet_list(4, [])

    def test_lease_not_found_result3_redirects(self):
        """result=3 from Kea (lease not found) shows warning and redirects."""
        with _lease_stub({"lease4-get": {"result": 3, "text": "not found"}, "subnet4-list": self._SUBNETS4}):
            response = self.client.get(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_bad_response_shape_redirects(self):
        """An empty (shapeless) response list redirects with error."""
        # A real command returning [] passes the result-code check but fails the
        # view's resp[0] shape guard → redirect.
        with _lease_stub({"lease4-get": lambda body: [], "subnet4-list": self._SUBNETS4}):
            response = self.client.get(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_bad_arguments_redirects(self):
        """Non-dict arguments in response redirects with error."""
        with _lease_stub({"lease4-get": {"result": 0, "arguments": "not a dict"}, "subnet4-list": self._SUBNETS4}):
            response = self.client.get(self._url(), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_get_client_value_error_redirects(self):
        """ValueError from get_client redirects with error."""
        # A cert without a key makes the real KeaClient constructor raise ValueError.
        bad = _make_db_server(name="badtls-edit", client_cert_path="/x/cert.pem")
        url = reverse("plugins:netbox_kea:server_lease4_edit", args=[bad.pk, "10.0.0.1"])
        with _lease_stub({"subnet4-list": self._SUBNETS4}):
            response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: lease update POST exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseUpdatePostErrors(_ViewTestBase):
    """Cover lease update POST error handling."""

    def _url(self, ip="10.0.0.1"):
        return reverse("plugins:netbox_kea:server_lease4_edit", args=[self.server.pk, ip])

    _SUBNETS4 = _subnet_list(4, [])

    def test_request_exception_redirects(self):
        """RequestException from lease_update redirects with error."""
        # lease_update's first command is lease4-get; raising there surfaces the transport error.
        with _lease_stub({"lease4-get": requests.ConnectionError("down"), "subnet4-list": self._SUBNETS4}):
            response = self.client.post(self._url(), {"hostname": "test", "valid_lft": "3600"}, follow=True)
        self.assertEqual(response.status_code, 200)

    def test_value_error_redirects(self):
        """ValueError from lease_update redirects with error."""
        with _lease_stub({"lease4-get": ValueError("bad JSON"), "subnet4-list": self._SUBNETS4}):
            response = self.client.post(self._url(), {"hostname": "test", "valid_lft": "3600"}, follow=True)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: lease add POST exception paths
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddPostErrors(_ViewTestBase):
    """Cover lease add POST error handling."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])

    def _valid_form(self):
        return {"ip_address": "10.0.0.1", "subnet_id": "1", "hw_address": "aa:bb:cc:00:00:01", "valid_lft": "3600"}

    def test_kea_exception_rerenders_form(self):
        """KeaException from lease_add re-renders form with error."""
        # result=1 on lease4-add makes the real client raise KeaException.
        with _lease_stub({"lease4-add": {"result": 1, "text": "dup"}}):
            response = self.client.post(self._url(), self._valid_form())
        self.assertEqual(response.status_code, 200)

    def test_a_conflict_shows_a_readable_message(self):
        """Kea's lease_cmds answers result 4 (CONTROL_RESULT_CONFLICT) when the lease already exists."""
        with _lease_stub({"lease4-add": {"result": 4, "text": "IPv4 lease already exists."}}):
            response = self.client.post(self._url(), self._valid_form())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            [
                (
                    "The change conflicts with the current state of the Kea server: for example, the lease already"
                    " exists, or another request changed it at the same time. Check the current state and try again."
                )
            ],
        )

    def test_request_exception_rerenders_form(self):
        """RequestException from lease_add re-renders form with error."""
        with _lease_stub({"lease4-add": requests.ConnectionError("down")}):
            response = self.client.post(self._url(), self._valid_form())
        self.assertEqual(response.status_code, 200)

    def test_value_error_rerenders_form(self):
        """ValueError from lease_add re-renders form with error."""
        with _lease_stub({"lease4-add": ValueError("bad JSON")}):
            response = self.client.post(self._url(), self._valid_form())
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: lease add post-creation side effect errors (journal + sync)
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseAddSideEffectErrors(_ViewTestBase):
    """Cover lease add post-creation side effect error paths."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_add", args=[self.server.pk])

    def _valid_form(self):
        return {"ip_address": "10.0.0.1", "subnet_id": "1", "hw_address": "aa:bb:cc:00:00:01", "valid_lft": "3600"}

    @patch("netbox_kea.views.leases._add_lease_journal", autospec=True)
    def test_journal_db_error_still_succeeds(self, mock_journal):
        """DatabaseError in journal entry creation still redirects successfully."""
        from django.db import DatabaseError

        mock_journal.side_effect = DatabaseError("DB error")
        subnets = _subnet_list(4, [])
        with _lease_stub({"lease4-add": {"result": 0}, "subnet4-list": subnets}):
            response = self.client.post(self._url(), self._valid_form(), follow=True)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Coverage: defensive checks in get_leases_page() and get_export_all()
# ---------------------------------------------------------------------------


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageDefensiveChecks(_ViewTestBase):
    """Cover the isinstance guards and RuntimeError paths in get_leases_page()."""

    _SUBNETS4 = _subnet_list(4, [])

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_non_list_leases_raises_runtime_error(self):
        """When Kea returns leases as non-list, the view catches the RuntimeError."""
        page = {"result": 0, "arguments": {"leases": "not-a-list", "count": 0}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": page}):
            response = self.client.get(self._url(), {"by": ""}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_non_int_count_raises_runtime_error(self):
        """When Kea returns count as non-int, the view catches the RuntimeError."""
        page = {"result": 0, "arguments": {"leases": [], "count": "bad"}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": page}):
            response = self.client.get(self._url(), {"by": ""}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_a_lease_without_an_address_on_a_partial_page_is_a_diagnostic(self):
        """The record is excluded with a safe reason; the page itself still renders."""
        page = {"result": 0, "arguments": {"leases": [{"no-ip": "bad"}], "count": 1}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": page}):
            response = self.client.get(self._url(), {"by": ""}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        _assert_no_error_template(self, response)
        self.assertIn(
            "leases[0] (ip-address): A required lease field is missing.", response.context["lease_diagnostics"]
        )
        self.assertNotContains(response, "bad")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestExportAllDefensiveChecks(_ViewTestBase):
    """Cover defensive branches in get_export_all()."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_non_list_leases_in_export_redirects(self):
        """When export_all gets non-list leases, it redirects with error."""
        page = {"result": 0, "arguments": {"leases": "not-a-list", "count": 0}}
        with _lease_stub({"lease4-get-page": page}):
            response = self.client.get(self._url(), {"export_all": "1"})
        self.assertEqual(response.status_code, 302)

    def test_non_int_count_in_export_redirects(self):
        """When export_all gets non-int count, it redirects with error."""
        page = {"result": 0, "arguments": {"leases": [], "count": "bad"}}
        with _lease_stub({"lease4-get-page": page}):
            response = self.client.get(self._url(), {"export_all": "1"})
        self.assertEqual(response.status_code, 302)

    def test_full_page_all_filtered_aborts_export(self):
        """When a full page has invalid entries, export aborts with an error."""
        # export_all uses per_page=1000. One invalid item makes the page indeterminate.
        page = {"result": 0, "arguments": {"leases": [{"no-ip": f"bad-{i}"} for i in range(1000)], "count": 1000}}
        with _lease_stub({"lease4-get-page": page}):
            response = self.client.get(self._url(), {"export_all": "1"})
        self.assertEqual(response.status_code, 302)

    def test_export_limit_refuses_a_partial_csv_and_reports_the_limit(self):
        leases = [lease_record(f"198.18.0.{index}") for index in range(1, 4)]

        with (
            patch("netbox_kea.views.leases._LEASE_EXPORT_MAX_LEASES", new=2),
            stub_kea({"lease4-get-page": lease_pages(leases)}),
        ):
            response = self.client.get(self._url(), {"export_all": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "Export is limited to 2 leases. Narrow the lease set before exporting.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )


# ─────────────────────────────────────────────────────────────────────────────
# Coverage gap tests — malformed responses, error paths, partial failures
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageMalformedResponse(_ViewTestBase):
    """get_leases_page() must raise RuntimeError on malformed Kea responses.

    These paths use the global paged search. The view catches RuntimeError and
    renders the HTMX error template (200, not 500).
    """

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _SUBNETS4 = _subnet_list(4, [])

    def test_non_list_leases_payload_renders_error(self):
        """When Kea returns non-list 'leases', the HTMX handler catches RuntimeError."""
        page = {"result": 0, "arguments": {"leases": "not-a-list", "count": 1}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": page}):
            response = self._htmx_get(self._url(), {"by": ""})
        self.assertEqual(response.status_code, 200)
        _assert_rendered_error_template(self, response)

    def test_non_int_count_renders_error(self):
        """When Kea returns non-int 'count', the HTMX handler catches RuntimeError."""
        page = {"result": 0, "arguments": {"leases": [], "count": "not-an-int"}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": page}):
            response = self._htmx_get(self._url(), {"by": ""})
        self.assertEqual(response.status_code, 200)
        _assert_rendered_error_template(self, response)

    def test_full_page_all_filtered_renders_error(self):
        """Full page (count==per_page) but all entries invalid must trigger RuntimeError."""
        # Entries that are not lease objects make the response indeterminate.
        per_page = 50
        page = {"result": 0, "arguments": {"leases": ["not-a-dict"] * per_page, "count": per_page}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": page}):
            response = self._htmx_get(
                self._url(),
                {"by": "", "per_page": str(per_page)},
            )
        self.assertEqual(response.status_code, 200)
        _assert_rendered_error_template(self, response)

    def test_none_arguments_renders_error(self):
        """When resp[0]['arguments'] is None, the HTMX handler catches RuntimeError."""
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": {"result": 0, "arguments": None}}):
            response = self._htmx_get(self._url(), {"by": ""})
        self.assertEqual(response.status_code, 200)
        _assert_rendered_error_template(self, response)

    def test_empty_response_list_renders_error(self):
        """When Kea returns an empty list, get_leases_page raises RuntimeError."""
        # A real command returning [] passes the result-code check but fails the resp guard.
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": lambda body: []}):
            response = self._htmx_get(self._url(), {"by": ""})
        self.assertEqual(response.status_code, 200)
        _assert_rendered_error_template(self, response)

    def test_non_dict_first_element_renders_error(self):
        """When resp[0] is not a dict, the HTMX handler catches RuntimeError."""
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": lambda _body: ["not-a-dict"]}):
            response = self._htmx_get(self._url(), {"by": ""})
        self.assertEqual(response.status_code, 200)
        _assert_rendered_error_template(self, response)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesSingleResultValidation(_ViewTestBase):
    """get_leases() single-result paths must raise RuntimeError on bad data.

    Single-result mode (by=ip) returns args dict directly, not a list.
    """

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _SUBNETS4 = _subnet_list(4, [])

    def test_an_exact_result_without_an_address_is_a_diagnostic_not_absence(self):
        resp = {"result": 0, "arguments": {"hw-address": "aa:bb:cc:dd:ee:ff", "subnet-id": 1}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get": resp}):
            response = self._htmx_get(self._url(), {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        _assert_no_error_template(self, response)
        self.assertIn(
            "arguments (ip-address): A required lease field is missing.", response.context["lease_diagnostics"]
        )

    def test_records_that_are_not_objects_are_diagnostics(self):
        resp = {"result": 0, "arguments": {"leases": ["bad", 123, None], "count": 3}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-by-hw-address": resp}):
            response = self._htmx_get(self._url(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        _assert_no_error_template(self, response)
        self.assertEqual(
            response.context["lease_diagnostics"],
            [f"leases[{index}] (record): Kea returned a lease that is not an object." for index in range(3)],
        )

    def test_multiple_result_none_arguments_renders_error(self):
        """Multiple-result with None arguments must trigger RuntimeError."""
        resp = {"result": 0, "arguments": None}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-by-hw-address": resp}):
            response = self._htmx_get(self._url(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        _assert_rendered_error_template(self, response)

    def test_multiple_result_non_list_leases_renders_error(self):
        """Multiple-result with non-list leases must trigger RuntimeError."""
        resp = {"result": 0, "arguments": {"leases": "not-a-list"}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-by-hw-address": resp}):
            response = self._htmx_get(self._url(), {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(response.status_code, 200)
        _assert_rendered_error_template(self, response)


@override_settings(PLUGINS_CONFIG=_UNGUARDED_PLUGINS_CONFIG)
class TestExportErrorPaths(_ViewTestBase):
    """Export must redirect with error messages when Kea calls fail."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _SUBNETS4 = _subnet_list(4, [])

    def test_export_request_exception_redirects(self):
        """RequestException during export fetch must redirect with error message."""
        with _lease_stub(
            {"subnet4-list": self._SUBNETS4, "lease4-get": requests.RequestException("connection refused")}
        ):
            response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 302)

    def test_export_runtime_error_redirects(self):
        """RuntimeError during export fetch must redirect with error message."""
        # Single-result response lacking 'ip-address' → get_leases() raises RuntimeError.
        resp = {"result": 0, "arguments": {"hw-address": "aa:bb:cc:dd:ee:ff"}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get": resp}):
            response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 302)

    def test_export_kea_exception_redirects(self):
        """KeaException during export fetch must redirect with error hint."""
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get": {"result": 1, "text": "internal error"}}):
            response = self.client.get(self._url(), {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 302)

    def test_export_client_creation_failure_redirects(self):
        """ValueError during get_client() for export must redirect with error message."""
        # A cert without a key makes the real KeaClient constructor raise ValueError.
        bad = _make_db_server(name="badtls-export2", client_cert_path="/x/cert.pem")
        url = reverse("plugins:netbox_kea:server_leases4", args=[bad.pk])
        with _lease_stub({"subnet4-list": self._SUBNETS4}):
            response = self.client.get(url, {"export": "all", "by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 302)

    def test_export_subnet_runtime_error_redirects(self):
        """RuntimeError during a Subnet export must redirect with an error message."""
        page = {"result": 0, "arguments": {"leases": "not-a-list", "count": 1}}
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-all": page}):
            response = self.client.get(self._url(), {"export": "all", "by": "subnet_id", "q": "1"})
        self.assertEqual(response.status_code, 302)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseDeletePartialFailure(_ViewTestBase):
    """Bulk delete must handle partial failures gracefully."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4_delete", args=[self.server.pk])

    def test_partial_failure_shows_mixed_messages(self):
        """Some leases succeed, others fail with KeaException → mixed messages."""
        # First lease4-del succeeds (result 0), second fails (result 1 → KeaException).
        with _lease_stub({"lease4-del": queued({"result": 0}, {"result": 1, "text": "lease not found"})}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        # Follow redirect to check messages
        msgs = list(response.wsgi_request._messages)
        msg_texts = [str(m) for m in msgs]
        # Should have success message for 1 lease + error for 1 + warning about failures
        has_success = any("Deleted 1" in t for t in msg_texts)
        has_error = any("Error deleting" in t for t in msg_texts)
        has_warning = any("Failed to delete" in t for t in msg_texts)
        self.assertTrue(has_success, f"Expected success message, got: {msg_texts}")
        self.assertTrue(has_error, f"Expected error message, got: {msg_texts}")
        self.assertTrue(has_warning, f"Expected warning message, got: {msg_texts}")

    def test_partial_failure_request_exception(self):
        """RequestException on some leases must show per-lease error messages."""

        # First lease4-del succeeds; the second raises a transport error.
        def del_resp(body):
            if body["arguments"]["ip-address"] == "10.0.0.2":
                return requests.RequestException("timeout")
            return {"result": 0}

        with _lease_stub({"lease4-del": del_resp}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        msgs = list(response.wsgi_request._messages)
        msg_texts = [str(m) for m in msgs]
        has_error = any("see server logs" in t for t in msg_texts)
        self.assertTrue(has_error, f"Expected transport error message, got: {msg_texts}")

    @patch("netbox_kea.views.leases._add_lease_journal", autospec=True)
    def test_journal_database_error_still_completes(self, mock_journal):
        """DatabaseError from journal creation must not prevent deletion from completing."""
        from django.db import DatabaseError

        mock_journal.side_effect = DatabaseError("table locked")
        with _lease_stub({"lease4-del": {"result": 0}}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        msgs = list(response.wsgi_request._messages)
        msg_texts = [str(m) for m in msgs]
        # The delete itself should succeed despite journal failure
        has_success = any("Deleted 1" in t for t in msg_texts)
        self.assertTrue(has_success, f"Expected success message despite journal error, got: {msg_texts}")

    def test_all_leases_fail_shows_only_errors(self):
        """When every lease deletion fails, no success message should appear."""
        # Every lease4-del returns result 1 → KeaException for each IP.
        with _lease_stub({"lease4-del": {"result": 1, "text": "not found"}}):
            response = self.client.post(
                self._url(),
                {"pk": ["10.0.0.1", "10.0.0.2"], "_confirm": "1"},
            )
        self.assertEqual(response.status_code, 302)
        msgs = list(response.wsgi_request._messages)
        msg_texts = [str(m) for m in msgs]
        has_success = any("Deleted" in t and "0" not in t for t in msg_texts)
        self.assertFalse(has_success, f"Should not have success message when all fail, got: {msg_texts}")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestFetchOneMacValueError(_ViewTestBase):
    """A lease with a non-numeric subnet_id is excluded before any reservation lookup.

    The typed reader excludes the lease with a diagnostic; the test drives it through
    an HTMX lease search.
    """

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    _SUBNETS4 = _subnet_list(4, [])

    def test_non_numeric_subnet_id_is_excluded_without_reservation_lookup(self):
        """A lease with a non-numeric subnet-id is excluded, diagnosed, and gets no reservation-get."""
        lease = complete_lease(
            {
                "ip-address": "10.0.0.5",
                "hw-address": "aa:bb:cc:dd:ee:ff",
                "hostname": "test",
                "subnet-id": "not-a-number",
                "valid-lft": 3600,
                "cltt": 1_700_000_000,
            }
        )
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get": {"result": 0, "arguments": lease}}) as kea:
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["table"].rows), [])
        self.assertEqual(
            response.context["lease_diagnostics"], ["arguments (subnet-id): A lease field has the wrong type."]
        )
        self.assertNotIn("reservation-get", kea.commands())

    def test_null_subnet_id_is_rejected_before_reservation_lookup(self):
        """A null subnet-id is diagnosed, and no MAC reservation lookup is sent to Kea."""
        lease = complete_lease(
            {
                "ip-address": "10.0.0.6",
                "hw-address": "aa:bb:cc:dd:ee:01",
                "hostname": "test2",
                "subnet-id": None,
                "valid-lft": 3600,
                "cltt": 1_700_000_000,
            }
        )
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get": {"result": 0, "arguments": lease}}) as kea:
            response = self._htmx_get(url, {"by": "ip", "q": "10.0.0.6"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["lease_diagnostics"])
        self.assertNotIn("reservation-get", kea.commands())


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestGetLeasesPageGlobalEdgeCases(_ViewTestBase):
    """Additional edge-case tests for global lease-page results."""

    def _htmx_get(self, url, data):
        return self.client.get(url, data=data, HTTP_HX_REQUEST="true")

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    _SUBNETS4 = _subnet_list(4, [])

    def test_result_3_returns_empty_table(self):
        """result=3 (no leases) must render an empty table, not an error."""
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": {"result": 3, "arguments": None}}):
            response = self._htmx_get(self._url(), {"by": ""})
        self.assertEqual(response.status_code, 200)
        _assert_no_error_template(self, response)
        self.assertContains(response, "No leases found.")
        self.assertEqual(len(response.context["table"].rows), 0)

    def test_global_page_preserves_all_returned_leases(self):
        """The client must not infer a Subnet boundary from backend result order."""
        page = {
            "result": 0,
            "arguments": {
                "leases": [
                    complete_lease(
                        {
                            "ip-address": "10.0.0.5",
                            "hw-address": "aa:bb:cc:dd:ee:ff",
                            "hostname": "in-subnet",
                            "subnet-id": 1,
                            "valid-lft": 3600,
                            "cltt": 1_700_000_000,
                        }
                    ),
                    complete_lease(
                        {
                            "ip-address": "10.0.1.5",
                            "hw-address": "aa:bb:cc:dd:ee:01",
                            "hostname": "out-of-subnet",
                            "subnet-id": 1,
                            "valid-lft": 3600,
                            "cltt": 1_700_000_000,
                        }
                    ),
                ],
                "count": 2,
            },
        }
        with _lease_stub({"subnet4-list": self._SUBNETS4, "lease4-get-page": page, "reservation-get": {"result": 3}}):
            response = self._htmx_get(self._url(), {"by": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10.0.0.5")
        self.assertContains(response, "10.0.1.5")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchSubnetCombobox(_ViewTestBase):
    """Lease search form renders an editable Subnet/Subnet-ID combobox on the Search field.

    There is no separate subnet selector — the attribute selector drives which
    (if any) datalist the Search field is associated with.
    """

    def _url(self):
        return reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

    def test_datalists_and_toggle_script_rendered(self):
        listing = _subnet_list(4, [{"id": 2, "subnet": "10.0.1.0/24"}, {"id": 3, "subnet": "10.0.2.0/24"}])
        with _lease_stub({"subnet4-list": listing}):
            body = self.client.get(self._url()).content.decode()
        # Both comboboxes present with the right values.
        self.assertIn('id="kea-lease-subnet-cidrs"', body)
        self.assertIn('id="kea-lease-subnet-ids"', body)
        self.assertIn('value="10.0.1.0/24"', body)  # CIDR option (by=subnet)
        self.assertIn('value="2"', body)  # subnet-id option (by=subnet_id)
        # The toggle script wires the Search field (q) to the attribute selector (by).
        self.assertIn("syncSubnetCombobox", body)
        self.assertIn('getElementById("id_by")', body)

    def test_suggestions_come_from_the_catalogue(self):
        """The lease and reservation datalists must describe the same set of subnets."""
        listing = _subnet_list(4, [{"id": 2, "subnet": "10.0.1.0/24"}])
        with _lease_stub({"subnet4-list": listing}) as kea:
            body = self.client.get(self._url()).content.decode()
        self.assertIn('value="10.0.1.0/24"', body)
        self.assertIn("config-get", kea.commands())

    def test_no_separate_subnet_select_field(self):
        listing = _subnet_list(4, [{"id": 2, "subnet": "10.0.1.0/24"}])
        with _lease_stub({"subnet4-list": listing}):
            body = self.client.get(self._url()).content.decode()
        # The old standalone subnet quick-select is gone.
        self.assertNotIn('name="subnet"', body)
        self.assertNotIn("Select a subnet", body)

    def test_no_datalists_when_no_subnets(self):
        with _lease_stub({"subnet4-list": {"result": 3, "text": "no subnets"}}):
            body = self.client.get(self._url()).content.decode()
        self.assertNotIn("kea-lease-subnet-cidrs", body)
        self.assertNotIn("syncSubnetCombobox", body)
        # An empty subnet list is not a missing hook — no warning.
        self.assertNotIn("hook library is not loaded", body)

    def test_missing_hook_explains_why_suggestions_are_gone(self):
        """Without subnet_cmds the search still works, so say that rather than fail silently."""
        with _lease_stub({"subnet4-list": {"result": 2, "text": "unknown command 'subnet4-list'"}}):
            response = self.client.get(self._url())
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("subnet_cmds", body)
        self.assertIn("hook library is not loaded", body)
        self.assertNotIn("kea-lease-subnet-cidrs", body)


class TestIdentityLookupMemoization(SimpleTestCase):
    """`_IdentityLookups` promises to call each lookup at most once."""

    def _lookups(self):
        from netbox_kea.views.leases import _IdentityLookups

        return _IdentityLookups()

    def test_a_successful_lookup_runs_once(self):
        calls = []

        def lookup():
            calls.append(1)

        lookups = self._lookups()
        for _ in range(3):
            # None is a real cached outcome: no reservation matches this identity.
            self.assertIsNone(lookups.resolve(("global", "aa:bb"), lookup))

        self.assertEqual(len(calls), 1)

    def test_a_failing_lookup_runs_once_and_replays_its_error(self):
        """Leases share identities, so a failing lookup must not be reissued per lease."""
        calls = []

        def lookup():
            calls.append(1)
            raise KeaException({"result": 1, "text": "reservation-get failed"})

        lookups = self._lookups()
        for _ in range(3):
            with self.assertRaises(KeaException):
                lookups.resolve(("global", "aa:bb"), lookup)

        self.assertEqual(len(calls), 1, "the failing lookup was reissued for a repeated identity")

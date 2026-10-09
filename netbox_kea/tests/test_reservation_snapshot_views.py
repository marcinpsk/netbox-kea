# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from urllib.parse import parse_qs, urlencode, urlsplit

import requests
import yaml
from bs4 import BeautifulSoup
from django.contrib import messages as django_messages
from django.urls import reverse

from netbox_kea.views.notices import HEADLINES, Notice
from netbox_kea.views.reservations import _RESERVATION_PAGE_SIZE

from .kea_stub import (
    _catalogue_responses,
    _res_get,
    _res_page,
    _reservation_mutation_commands,
    complete_lease,
    queued,
    stub_kea,
)
from .utils import _ViewTestBase


def _return_url(url: str) -> list[str]:
    return parse_qs(urlsplit(url).query).get("return_url", [])


class TestPerServerReservationSnapshots(_ViewTestBase):
    def _url(self, version: int = 4) -> str:
        return reverse(f"plugins:netbox_kea:server_reservations{version}", args=[self.server.pk])

    def _searched_row(self, url: str, query: dict) -> tuple[dict, str]:
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        host = {"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "searched.example.invalid"}
        responses.update(
            {
                "reservation-get-page": _res_page([host]),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
                "list-commands": _reservation_mutation_commands(),
            }
        )
        with stub_kea(responses):
            response = self.client.get(url, query)
        self.assertContains(response, "searched.example.invalid")
        return response.context["table"].data.data[0], response.wsgi_request.get_full_path()

    def test_row_actions_return_to_the_reservation_search(self):
        row, search = self._searched_row(self._url(), {"q": "searched"})
        self.assertEqual(_return_url(row["edit_url"]), [search])
        self.assertEqual(_return_url(row["delete_url"]), [search])

    def test_the_add_button_returns_to_the_reservation_search(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page([]),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
                "list-commands": _reservation_mutation_commands(),
            }
        )
        with stub_kea(responses):
            page = self.client.get(self._url(), {"q": "searched"})
        search = page.wsgi_request.get_full_path()
        add = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        self.assertContains(page, f'href="{add}?{urlencode({"return_url": search})}"')

    def test_combined_row_actions_return_to_the_combined_search(self):
        url = reverse("plugins:netbox_kea:combined_reservations4")
        row, search = self._searched_row(url, {"server": self.server.pk, "q": "searched"})
        self.assertEqual(_return_url(row["edit_url"]), [search])
        self.assertEqual(_return_url(row["delete_url"]), [search])

    def test_configured_only_subnet_filter_does_not_authorize_a_scoped_read(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["subnet4-list"] = {"result": 2, "text": "subnet commands unavailable"}
        responses["reservation-get-page"] = _res_page([{"subnet-id": 20, "flex-id": "not-verified"}])

        with stub_kea(responses) as kea:
            response = self.client.get(self._url(), {"subnet_id": "20"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["table"].data.data, [])
        self.assertIn(
            "unverified-scope", [diagnostic.code for diagnostic in response.context["reservation_notice"].diagnostics]
        )
        self.assertNotIn("subnet-id", kea.bodies("reservation-get-page")[0]["arguments"])

    def test_substring_search_continues_past_a_nonmatching_page(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        first = [{"subnet-id": 20, "flex-id": f"unrelated-{index}"} for index in range(100)]
        match = {"subnet-id": 20, "flex-id": "printer", "hostname": "office-needle-printer"}
        responses["reservation-get-page"] = queued(_res_page(first, next_from=100, next_source=1), _res_page([match]))
        with stub_kea(responses):
            response = self.client.get(self._url() + "?q=needle")
        self.assertContains(response, "office-needle-printer")

    def test_search_preserves_earlier_diagnostics_when_a_later_read_fails(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        first = [{"subnet-id": 20, "flex-id": "printer", "hostname": 42}]
        responses["reservation-get-page"] = queued(
            _res_page(first, next_from=7, next_source=2),
            requests.ConnectionError("private failure detail"),
            _res_page([{"subnet-id": 20, "flex-id": "office", "hostname": "office-needle-printer"}]),
        )
        with stub_kea(responses) as kea:
            response = self.client.get(self._url() + "?q=needle")
            self.assertIsNotNone(response.context["next_page_url"])
            retried = self.client.get(response.context["next_page_url"])
        self.assertContains(response, "invalid-hostname")
        self.assertContains(response, "page-fetch-failed")
        self.assertNotContains(response, "private failure detail")
        self.assertIsNotNone(response.context["next_page_url"])
        self.assertFalse(response.context["snapshot_complete"])
        self.assertContains(retried, "office-needle-printer")
        failed_arguments, retried_arguments = [body["arguments"] for body in kea.bodies("reservation-get-page")[1:]]
        self.assertEqual(retried_arguments, failed_arguments)
        self.assertEqual(retried_arguments["from"], 7)
        self.assertEqual(retried_arguments["source-index"], 2)

    def test_search_preserves_earlier_diagnostics_when_a_later_page_matches(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        first = [{"subnet-id": 20, "flex-id": "printer", "hostname": 42}]
        match = {"subnet-id": 20, "flex-id": "office", "hostname": "office-needle-printer"}
        responses["reservation-get-page"] = queued(_res_page(first, next_from=7, next_source=1), _res_page([match]))
        with stub_kea(responses):
            response = self.client.get(self._url() + "?q=needle")
        self.assertContains(response, "invalid-hostname")
        self.assertContains(response, "office-needle-printer")
        self.assertFalse(response.context["snapshot_complete"])

    def test_empty_advancing_pages_stop_with_a_visible_search_limit_warning(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = queued(
            *[_res_page([], next_from=index, next_source=1) for index in range(1, 6)]
        )
        with stub_kea(responses) as kea:
            response = self.client.get(self._url() + "?q=needle")
        self.assertEqual(len(kea.bodies("reservation-get-page")), 5)
        self.assertContains(response, "Search is incomplete.")
        self.assertContains(response, "At most 500 Reservations per server were inspected.")
        self.assertContains(response, "Narrow the filters or continue with Next page.")
        self.assertNotContains(response, "No reservations found.")
        self.assertIsNotNone(response.context["next_page_url"])

    def test_an_unavailable_hook_does_not_also_report_an_unreadable_snapshot(self):
        """One cause must produce one banner: the empty snapshot is not a partial read."""
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update({"reservation-get-page": {"result": 2, "text": "command not supported"}})

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hook library is not loaded")
        self.assertNotContains(response, "Snapshot is incomplete")
        self.assertNotContains(response, "This bounded Snapshot is complete")

    def test_renders_valid_records_scope_and_incomplete_diagnostics_from_one_page(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        hosts = [
            {"subnet-id": 0, "flex-id": "global-class", "hostname": "global.example.invalid"},
            {
                "subnet-id": 20,
                "hw-address": "AA-BB-CC-DD-EE-FF",
                "ip-address": "198.18.0.20",
                "hostname": "valid.example.invalid",
                "option-data": [{"name": "domain-name-servers", "data": "198.18.0.53"}],
            },
            {"subnet-id": 20, "remote-id": "private rejected value"},
            *[{"subnet-id": 20, "flex-id": f"page-filler-{index}"} for index in range(_RESERVATION_PAGE_SIZE - 3)],
        ]
        responses.update(
            {
                "reservation-get-page": _res_page(
                    hosts,
                    next_from=3,
                    next_source=1,
                ),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        )

        with stub_kea(responses) as kea:
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "global.example.invalid")
        self.assertContains(response, "valid.example.invalid")
        self.assertContains(response, "Global")
        self.assertContains(response, "198.18.0.0/24")
        self.assertContains(response, "domain-name-servers")
        self.assertContains(response, "Snapshot is incomplete")
        self.assertContains(response, "See the 1 diagnostic below.")
        self.assertNotContains(response, "private rejected value")
        self.assertContains(response, "Next page")
        self.assertEqual(len(kea.bodies("reservation-get-page")), 1)

        rows = response.context["table"].data.data
        global_row = next(row for row in rows if row["scope_kind"] == "global")
        self.assertIsNone(global_row["edit_url"])
        self.assertIsNone(global_row["delete_url"])
        self.assertIsNone(global_row["sync_url"])

    def test_a_failed_page_read_warns_that_the_snapshot_is_incomplete(self):
        """A read failure is the one path that is incomplete and carries no diagnostic.

        ``_parse_reservation_page`` sets ``complete`` from the diagnostics, so a page that
        stops early with every record parsed is complete. Only the view's empty fallback
        reports incomplete with nothing to list, which is the branch the banner guards.
        """
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": requests.ConnectionError("kea unreachable"),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["snapshot_complete"])
        self.assertEqual(response.context["reservation_notice"], Notice("reservation", django_messages.ERROR))
        self.assertEqual(response.context["table"].data.data, [])
        self.assertContains(response, "Snapshot is incomplete")
        self.assertNotContains(response, "diagnostic below")
        self.assertNotContains(response, "This bounded Snapshot is complete")
        self.assertIn(HEADLINES["reservation"], [str(message) for message in response.context["messages"]])

    def test_a_full_page_with_more_to_come_is_reported_complete(self):
        """A filled page offers the next cursor and is still complete for this page.

        The old version of the test above assumed the opposite, so pin the real contract:
        pagination is surfaced by the next-page link, not by the completeness banner.
        """
        hosts = [
            {
                "subnet-id": 20,
                "hw-address": f"aa:bb:cc:00:{index // 256:02x}:{index % 256:02x}",
                "ip-address": f"198.18.0.{index + 1}",
            }
            for index in range(_RESERVATION_PAGE_SIZE)
        ]
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(hosts, next_from=3, next_source=1),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["snapshot_complete"])
        self.assertIsNone(response.context["reservation_notice"])
        self.assertIsNotNone(response.context["next_page_url"])
        self.assertContains(response, "This bounded Snapshot is complete")

    def test_scope_filter_keeps_only_global_records_on_the_current_page(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = _res_page(
            [
                {"subnet-id": 0, "flex-id": "global-class", "hostname": "global.example.invalid"},
                {
                    "subnet-id": 20,
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "hostname": "local.example.invalid",
                },
            ]
        )

        with stub_kea(responses):
            response = self.client.get(self._url(), {"scope": "global"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "global.example.invalid")
        self.assertNotContains(response, "local.example.invalid")

    def test_capability_failure_preserves_records_and_hides_mutation_controls(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [
                        {
                            "subnet-id": 20,
                            "hw-address": "aa:bb:cc:dd:ee:ff",
                            "hostname": "read-only.example.invalid",
                        }
                    ]
                ),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
                "list-commands": RuntimeError("capability discovery failed"),
            }
        )

        with stub_kea(responses) as kea:
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "read-only.example.invalid")
        self.assertContains(response, "Reservation mutation controls are unavailable")
        row = response.context["table"].data.data[0]
        self.assertIsNone(row["edit_url"])
        self.assertIsNone(row["delete_url"])
        self.assertIsNone(response.context["add_url"])
        self.assertIsNone(response.context["import_url"])
        self.assertEqual(kea.commands().count("reservation-get-page"), 1)
        self.assertEqual(kea.commands().count("list-commands"), 1)

    def test_renders_every_exposed_dhcp_option_fact(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [
                        {
                            "subnet-id": 0,
                            "flex-id": "option-display",
                            "option-data": [
                                {
                                    "code": 222,
                                    "name": "vendor-option",
                                    "space": "vendor-space",
                                    "data": "0a:0b",
                                    "csv-format": False,
                                    "always-send": True,
                                    "never-send": False,
                                }
                            ],
                        }
                    ]
                ),
                "list-commands": _reservation_mutation_commands(),
            }
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vendor-space")
        self.assertContains(response, "vendor-option")
        self.assertContains(response, "Code 222")
        self.assertContains(response, "CSV format: No")
        self.assertContains(response, "Always send: Yes")
        self.assertContains(response, "Never send: No")

    def test_active_lease_matches_by_identity_when_the_reserved_address_differs(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [
                        {
                            "subnet-id": 20,
                            "hw-address": "aa:bb:cc:dd:ee:ff",
                            "ip-address": "198.18.0.20",
                        }
                    ]
                ),
                "lease4-get-by-state": {
                    "result": 0,
                    "arguments": {
                        "leases": [
                            complete_lease(
                                {
                                    "subnet-id": 20,
                                    "hw-address": "AA-BB-CC-DD-EE-FF",
                                    "ip-address": "198.18.0.21",
                                    "state": 0,
                                }
                            )
                        ]
                    },
                },
            }
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        row = response.context["table"].data.data[0]
        self.assertTrue(row["has_active_lease"])

    def test_disables_sync_controls_without_ipam_write_permissions(self):
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        limited = get_user_model().objects.create_user(username="reservation_editor")
        permission = ObjectPermission.objects.create(
            name="change-server-without-ipam-write",
            actions=["view", "change"],
        )
        permission.object_types.add(ContentType.objects.get_for_model(type(self.server)))
        permission.users.add(limited)
        self.client.force_login(limited)

        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [
                        {
                            "subnet-id": 20,
                            "hw-address": "aa:bb:cc:dd:ee:ff",
                            "ip-address": "198.18.0.20",
                        }
                    ]
                ),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        row = response.context["table"].data.data[0]
        self.assertIsNone(row["sync_url"])
        self.assertIsNone(response.context["bulk_sync_url"])
        self.assertIn("ipam.add_ipaddress", row["sync_refusal"])
        self.assertIn("ipam.add_ipaddress", response.context["bulk_sync_refusal"])

    def test_shows_sync_controls_without_server_change_permission(self):
        from dcim.models import MACAddress
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress
        from users.models import ObjectPermission

        limited = get_user_model().objects.create_user(username="reservation_sync_operator")
        server_permission = ObjectPermission.objects.create(name="view-server-for-reservation-sync", actions=["view"])
        server_permission.object_types.add(ContentType.objects.get_for_model(type(self.server)))
        server_permission.users.add(limited)
        ipam_permission = ObjectPermission.objects.create(
            name="write-ip-addresses-for-reservation-sync",
            actions=["add", "change"],
        )
        ipam_permission.object_types.add(
            ContentType.objects.get_for_model(IPAddress), ContentType.objects.get_for_model(MACAddress)
        )
        ipam_permission.users.add(limited)
        self.client.force_login(limited)

        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [
                        {
                            "subnet-id": 20,
                            "hw-address": "aa:bb:cc:dd:ee:ff",
                            "ip-address": "198.18.0.20",
                        }
                    ]
                ),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        row = response.context["table"].data.data[0]
        self.assertIsNotNone(row["sync_url"])
        self.assertIsNone(row["edit_url"])
        self.assertIsNone(row["delete_url"])
        self.assertIsNotNone(response.context["bulk_sync_url"])
        self.assertIsNone(response.context["add_url"])
        self.assertIsNone(response.context["import_url"])

    def test_the_row_sync_control_sends_a_csrf_token(self):
        """The Reservation table is not inside a form, so the token must travel as a header.

        Without it the HTMX POST is rejected with 403 before the view runs, and the
        button silently does nothing.
        """
        import re

        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [{"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"}]
                ),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        )

        with stub_kea(responses):
            response = self.client.get(self._url())

        content = response.content.decode()
        # Scope to the row control's own element: a token anywhere else on the page
        # must not satisfy a claim about this button.
        button = re.search(r"<button[^>]*hx-post=\"[^\"]*/sync/[^\"]*\"[^>]*>", content)
        self.assertIsNotNone(button, "The row sync control is not rendered.")
        token = re.search(r'hx-headers=\'{"X-CSRFToken": "([^"]*)"}\'', button.group(0))
        self.assertIsNotNone(token, "The row sync control sends no CSRF header.")
        self.assertTrue(token.group(1), "The CSRF header is present but empty.")

    def test_exports_the_complete_current_snapshot_as_yaml(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = _res_page(
            [
                {
                    "subnet-id": 20,
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "ip-address": "198.18.0.20",
                    "hostname": "export.example.invalid",
                }
            ]
        )

        with stub_kea(responses):
            response = self.client.get(self._url(), {"export": "yaml"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/yaml")
        document = yaml.safe_load(response.content)
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["reservations"][0]["scope"]["subnet"]["cidr"], "198.18.0.0/24")
        self.assertNotIn("subnet-id", response.content.decode())

    def test_refuses_to_export_an_incomplete_snapshot(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = _res_page([{"subnet-id": 20, "remote-id": "not-native"}])

        with stub_kea(responses):
            response = self.client.get(self._url(), {"export": "json"})

        self.assertEqual(response.status_code, 409)


class TestCombinedReservationSnapshots(_ViewTestBase):
    def test_failed_page_read_warns_that_the_combined_snapshot_is_incomplete(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": requests.ConnectionError("kea unreachable"),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
            }
        )
        url = reverse("plugins:netbox_kea:combined_reservations4")

        with stub_kea(responses):
            response = self.client.get(url, {"server": self.server.pk})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["snapshot_complete"])
        self.assertEqual(response.context["reservation_diagnostics"], [])
        self.assertContains(response, "Snapshot is incomplete")
        self.assertNotContains(response, "diagnostic below")
        self.assertNotContains(response, "This bounded Snapshot is complete")

    def test_combined_view_fetches_one_bounded_page_and_offers_the_next_cursor(self):
        responses = _catalogue_responses(6, 30, "2001:db8::/64")
        hosts = [
            {
                "subnet-id": 30,
                "duid": "00-01-02-03",
                "ip-addresses": ["2001:db8::20", "2001:db8::21"],
                "prefixes": ["2001:db8:100::/56"],
            },
            *[{"subnet-id": 30, "flex-id": f"page-filler-{index}"} for index in range(_RESERVATION_PAGE_SIZE - 1)],
        ]
        responses["reservation-get-page"] = _res_page(
            hosts,
            next_from=1,
            next_source=1,
        )
        responses["lease6-get-by-state"] = {"result": 0, "arguments": {"leases": []}}
        url = reverse("plugins:netbox_kea:combined_reservations6")

        with stub_kea(responses) as kea:
            response = self.client.get(url, {"server": self.server.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2001:db8::20")
        self.assertContains(response, "2001:db8::21")
        self.assertContains(response, "2001:db8:100::/56")
        self.assertContains(response, "Next page")
        self.assertEqual(len(kea.bodies("reservation-get-page")), 1)

    def test_capability_failure_preserves_combined_records_without_mutation_controls(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [
                        {
                            "subnet-id": 20,
                            "hw-address": "aa:bb:cc:dd:ee:ff",
                            "hostname": "combined-read-only.example.invalid",
                        }
                    ]
                ),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
                "list-commands": RuntimeError("capability discovery failed"),
            }
        )
        url = reverse("plugins:netbox_kea:combined_reservations4")

        with stub_kea(responses) as kea:
            response = self.client.get(url, {"server": self.server.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "combined-read-only.example.invalid")
        self.assertContains(response, "Mutation controls are unavailable for some servers")
        row = response.context["table"].data.data[0]
        self.assertIsNone(row["edit_url"])
        self.assertIsNone(row["delete_url"])
        self.assertEqual(kea.commands().count("reservation-get-page"), 1)
        self.assertEqual(kea.commands().count("list-commands"), 1)

    def test_scope_only_filter_can_be_cleared_without_losing_server_selection(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "reservation-get-page": _res_page(
                    [
                        {"subnet-id": 0, "flex-id": "global-class", "hostname": "global.example.invalid"},
                        {
                            "subnet-id": 20,
                            "hw-address": "aa:bb:cc:dd:ee:ff",
                            "hostname": "local.example.invalid",
                        },
                    ]
                ),
                "lease4-get-by-state": {"result": 0, "arguments": {"leases": []}},
                "list-commands": _reservation_mutation_commands(),
            }
        )
        url = reverse("plugins:netbox_kea:combined_reservations4")

        with stub_kea(responses):
            response = self.client.get(url, {"server": self.server.pk, "scope": "global"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "global.example.invalid")
        self.assertNotContains(response, "local.example.invalid")
        self.assertContains(response, "Clear")
        self.assertContains(response, f"?server={self.server.pk}")

    def test_combined_export_uses_the_normalized_json_schema(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["reservation-get-page"] = queued(
            _res_page(
                [{"subnet-id": 20, "flex-id": "first-page", "hostname": "first.example.invalid"}],
                next_from=1,
                next_source=1,
            ),
            _res_page([{"subnet-id": 20, "flex-id": "second-page", "hostname": "second.example.invalid"}]),
        )
        url = reverse("plugins:netbox_kea:combined_reservations4")

        with stub_kea(responses) as kea:
            response = self.client.get(url, {"server": self.server.pk, "export": "json"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [record["identity"] for record in response.json()["reservations"]],
            [
                {"type": "flex-id", "value": "first-page"},
                {"type": "flex-id", "value": "second-page"},
            ],
        )
        self.assertEqual(len(kea.bodies("reservation-get-page")), 2)


_HOST_RESERVATION_TITLE = "This Reservation holds no address, so Kea assigns this lease from the pool."


def _reserved_badges(response) -> list:
    """Return the badges in the Reserved cell of the first lease row."""
    cell = next(iter(response.context["table"].rows)).get_cell("reserved")
    return BeautifulSoup(str(cell), "html.parser").select(".badge")


class TestLeaseReservationIdentityMatching(_ViewTestBase):
    lease = complete_lease(
        {
            "ip-address": "198.18.0.20",
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "subnet-id": 20,
            "hostname": "lease.example.invalid",
            "cltt": 1_700_000_000,
            "valid-lft": 3600,
            "state": 0,
        }
    )

    def _get(self, reservation_responses):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses.update(
            {
                "lease4-get": {"result": 0, "arguments": self.lease},
                "reservation-get": reservation_responses,
            }
        )
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])
        with stub_kea(responses):
            return self.client.get(url, {"by": "ip", "q": "198.18.0.20"}, HTTP_HX_REQUEST="true")

    def test_configured_only_subnet_does_not_trigger_a_reservation_lookup(self):
        responses = _catalogue_responses(4, 20, "198.18.0.0/24")
        responses["subnet4-list"] = {"result": 2, "text": "subnet commands unavailable"}
        responses["lease4-get"] = {"result": 0, "arguments": self.lease}
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

        with stub_kea(responses) as kea:
            response = self.client.get(url, {"by": "ip", "q": "198.18.0.20"}, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reservation-get", kea.commands())
        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIsNone(row["create_reservation_url"])
        self.assertIsNone(row.get("sync_url"))

    def test_the_reserve_link_returns_to_the_lease_search(self):
        response = self._get({"result": 3})

        row = response.context["table"].data.data[0]
        self.assertEqual(_return_url(row["create_reservation_url"]), [response.wsgi_request.get_full_path()])

    def test_the_reservation_link_returns_to_the_lease_search(self):
        response = self._get(
            _res_get({"subnet-id": 20, "hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "198.18.0.20"})
        )

        row = response.context["table"].data.data[0]
        self.assertEqual(_return_url(row["reservation_url"]), [response.wsgi_request.get_full_path()])

    _HOSTNAME_ONLY = {"subnet-id": 20, "hw-address": "AA-BB-CC-DD-EE-FF", "hostname": "classified"}

    def test_addressless_reservation_matches_normalized_identity_in_the_same_subnet(self):
        response = self._get(queued({"result": 3}, _res_get(self._HOSTNAME_ONLY)))

        self.assertEqual(response.status_code, 200)
        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"], "the Reservation holds no address, so the lease is from the pool")
        self.assertTrue(row["host_reservation"])
        self.assertFalse(row["pending_ip_change"])
        self.assertIn("identifier_type=hw-address", row["reservation_url"])
        self.assertIn("identifier=aa%3Abb%3Acc%3Add%3Aee%3Aff", row["reservation_url"])
        [badge] = _reserved_badges(response)
        self.assertEqual(
            (badge.name, badge["class"], badge.get_text(strip=True)),
            ("a", ["badge", "text-bg-secondary", "text-decoration-none"], "Host reservation"),
        )
        self.assertEqual(badge["href"], row["reservation_url"])
        self.assertEqual(badge["title"], _HOST_RESERVATION_TITLE)

    def test_a_hostname_only_reservation_without_change_permission_is_a_plain_badge(self):
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        viewer = get_user_model().objects.create_user(username="lease_viewer")
        permission = ObjectPermission.objects.create(name="view-server-for-leases", actions=["view"])
        permission.object_types.add(ContentType.objects.get_for_model(type(self.server)))
        permission.users.add(viewer)
        self.client.force_login(viewer)

        response = self._get(queued({"result": 3}, _res_get(self._HOSTNAME_ONLY)))

        [badge] = _reserved_badges(response)
        self.assertEqual(
            (badge.name, badge["class"], badge.get_text(strip=True), badge["title"]),
            ("span", ["badge", "text-bg-secondary"], "Host reservation", _HOST_RESERVATION_TITLE),
        )

    def test_a_prefix_only_reservation_matched_by_identity_is_not_a_host_reservation(self):
        lease = complete_lease(
            {
                "ip-address": "2001:db8:20::20",
                "duid": "00:01:02:03",
                "iaid": 1,
                "subnet-id": 20,
                "cltt": 1_700_000_000,
                "valid-lft": 3600,
                "state": 0,
            }
        )

        def lease6_get(body):
            if "type" in body["arguments"]:
                return {"result": 3, "text": "Lease not found."}
            return {"result": 0, "arguments": lease}

        responses = _catalogue_responses(6, 20, "2001:db8:20::/64")
        responses.update(
            {
                "lease6-get": lease6_get,
                "reservation-get": queued(
                    {"result": 3},
                    _res_get({"subnet-id": 20, "duid": "00:01:02:03", "prefixes": ["2001:db8:100::/56"]}),
                ),
            }
        )
        url = reverse("plugins:netbox_kea:server_leases6", args=[self.server.pk])
        with stub_kea(responses):
            response = self.client.get(url, {"by": "ip", "q": "2001:db8:20::20"}, HTTP_HX_REQUEST="true")

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"], "the Reservation holds only a delegated prefix, not this address")
        self.assertFalse(row["host_reservation"])
        self.assertTrue(row["other_kind_reservation"])
        self.assertEqual([badge.get_text(strip=True) for badge in _reserved_badges(response)], ["Reservation"])

    def test_global_reservation_matches_by_identity_and_has_no_mutation_link(self):
        response = self._get(
            queued(
                {"result": 3},
                {"result": 3},
                _res_get({"subnet-id": 0, "hw-address": "aa:bb:cc:dd:ee:ff", "hostname": "global"}),
            )
        )

        row = response.context["table"].data.data[0]
        self.assertTrue(row["is_reserved"])
        self.assertFalse(row["host_reservation"], "a global Reservation keeps the Reserved badge")
        self.assertIsNone(row["reservation_url"])
        self.assertIsNone(row["create_reservation_url"])
        [badge] = _reserved_badges(response)
        self.assertEqual((badge["class"], badge.get_text(strip=True)), (["badge", "text-bg-success"], "Reserved"))

    def test_wrong_subnet_address_result_is_indeterminate_and_offers_no_action(self):
        response = self._get(
            _res_get(
                {
                    "subnet-id": 21,
                    "hw-address": "aa:bb:cc:dd:ee:ff",
                    "ip-address": "198.18.0.20",
                }
            )
        )

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIsNone(row["reservation_url"])
        self.assertIsNone(row["create_reservation_url"])

    def test_identity_match_at_another_address_reports_one_pending_change(self):
        response = self._get(
            queued(
                {"result": 3},
                _res_get(
                    {
                        "subnet-id": 20,
                        "hw-address": "aa:bb:cc:dd:ee:ff",
                        "ip-address": "198.18.0.21",
                    }
                ),
            )
        )

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"], "the lease address is not the reserved one yet")
        self.assertTrue(row["pending_ip_change"])
        self.assertEqual(row["pending_reservation_ip"], "198.18.0.21")
        badges = [
            badge.get_text(strip=True) for badge in BeautifulSoup(response.content, "html.parser").select(".badge")
        ]
        self.assertIn("Pending 198.18.0.21", badges)
        self.assertNotIn("Reserved", badges)
        self.assertIsNone(row["create_reservation_url"])
        self.assertIn("/reservations4/20/edit/", row["reservation_url"])
        self.assertIn("identifier_type=hw-address", row["reservation_url"])

    def test_confirmed_absence_offers_a_prefilled_create_action(self):
        response = self._get(queued({"result": 3}, {"result": 3}, {"result": 3}))

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIn("/reservations4/add/", row["create_reservation_url"])
        self.assertIn("subnet_cidr=198.18.0.0%2F24", row["create_reservation_url"])

    def test_the_create_action_prefills_the_published_name_of_the_lease(self):
        # An FQDN lease keeps the trailing dot; the Reservation form takes the name without it.
        self.lease = {**self.lease, "hostname": "lease.example.invalid."}
        response = self._get(queued({"result": 3}, {"result": 3}, {"result": 3}))

        url = response.context["table"].data.data[0]["create_reservation_url"]
        self.assertEqual(parse_qs(urlsplit(url).query)["hostname"], ["lease.example.invalid"])

    def test_unavailable_host_commands_do_not_offer_a_false_create_action(self):
        response = self._get({"result": 2, "text": "command not supported"})

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIsNone(row["create_reservation_url"])

    def test_lookup_failure_is_indeterminate_and_offers_no_action(self):
        response = self._get(RuntimeError("transport failed"))

        row = response.context["table"].data.data[0]
        self.assertFalse(row["is_reserved"])
        self.assertIsNone(row["reservation_url"])
        self.assertIsNone(row["create_reservation_url"])

    def test_address_match_with_a_different_hardware_identity_is_stale(self):
        response = self._get(
            _res_get(
                {
                    "subnet-id": 20,
                    "hw-address": "aa:bb:cc:dd:ee:01",
                    "ip-address": "198.18.0.20",
                }
            )
        )

        row = response.context["table"].data.data[0]
        self.assertTrue(row["is_reserved"])
        self.assertTrue(row["stale_mac"])
        self.assertEqual(row["stale_lease_mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(row["reservation_mac"], "aa:bb:cc:dd:ee:01")

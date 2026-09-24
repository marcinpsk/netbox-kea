"""Subnet pickers follow catalogue invalidation across configuration writes."""

from django.contrib import messages
from django.test import override_settings
from django.urls import reverse

from .kea_stub import _catalogue_responses_for_subnets, stub_kea
from .utils import _PLUGINS_CONFIG, _ViewTestBase

_DISAGREEMENT = "Kea subnet identity and configuration facts disagree after a fresh retry."


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSubnetPickerViews(_ViewTestBase):
    def test_lease_picker_refreshes_after_subnet_creation(self):
        self._assert_refreshes_after_subnet_creation("server_leases4")

    def test_reservation_picker_refreshes_after_subnet_creation(self):
        self._assert_refreshes_after_subnet_creation("server_reservation4_add")

    def test_configured_choices_render_without_subnet_commands(self):
        for family, cidr in ((4, "198.18.1.0/24"), (6, "2001:db8:1::/64")):
            for view_name in (f"server_leases{family}", f"server_reservation{family}_add"):
                with self.subTest(family=family, view=view_name):
                    responses = _catalogue_responses_for_subnets(family, [{"id": 1, "subnet": cidr}])
                    responses[f"subnet{family}-list"] = {"result": 2, "text": "Command not supported"}
                    with stub_kea(responses):
                        response = self.client.get(reverse(f"plugins:netbox_kea:{view_name}", args=[self.server.pk]))
                    self.assertContains(response, f'value="{cidr}"')
                    self.assertContains(response, "hook library is not loaded")
                    self.assertContains(response, "Subnet suggestions come from the running configuration.")
                    self.assertNotContains(response, "cannot offer the configured subnets")
                    self.assertNotContains(response, "saving will fail")
                    if view_name.startswith("server_reservation"):
                        self.assertContains(
                            response, "Saving an in-Subnet Reservation requires this hook to verify Subnet identity."
                        )
                    else:
                        self.assertContains(response, "Searching by subnet CIDR or ID does not require this hook.")

    def test_invalid_reservation_post_keeps_catalogue_choices(self):
        for family, cidr in ((4, "198.18.1.0/24"), (6, "2001:db8:1::/64")):
            with self.subTest(family=family):
                responses = _catalogue_responses_for_subnets(family, [{"id": 1, "subnet": cidr}])
                with stub_kea(responses):
                    response = self.client.post(
                        reverse(f"plugins:netbox_kea:server_reservation{family}_add", args=[self.server.pk]),
                        {},
                    )
                self.assertContains(response, f'value="{cidr}"')

    def test_lease_picker_shows_catalogue_diagnostics_inline(self):
        for family, responses, listed, configured in self._disagreeing_catalogues():
            url = reverse(f"plugins:netbox_kea:server_leases{family}", args=[self.server.pk])
            for label, headers in (("page", {}), ("htmx", {"HTTP_HX_REQUEST": "true"})):
                with self.subTest(family=family, request=label), stub_kea(responses):
                    response = self.client.get(url, {"by": "ip", "q": ""}, **headers)
                    self.assertContains(response, 'class="alert alert-danger py-2 px-3 mb-3 small"')
                    self.assertContains(response, _DISAGREEMENT)
                    self.assertEqual(list(response.context["messages"]), [])
                    self.assertNotContains(response, f'value="{listed}"')
                    self.assertNotContains(response, f'value="{configured}"')

    def test_reservation_picker_shows_catalogue_diagnostics(self):
        for family, responses, listed, configured in self._disagreeing_catalogues():
            url = reverse(f"plugins:netbox_kea:server_reservation{family}_add", args=[self.server.pk])
            for method in ("get", "post"):
                with self.subTest(family=family, method=method), stub_kea(responses):
                    response = self.client.get(url) if method == "get" else self.client.post(url, {})
                    shown = [(message.level, message.message) for message in response.context["messages"]]
                    self.assertIn((messages.ERROR, _DISAGREEMENT), shown)
                    self.assertNotContains(response, f'value="{listed}"')
                    self.assertNotContains(response, f'value="{configured}"')

    @staticmethod
    def _disagreeing_catalogues():
        for family, listed, configured in (
            (4, "198.18.1.0/24", "198.18.9.0/24"),
            (6, "2001:db8:1::/64", "2001:db8:9::/64"),
        ):
            responses = _catalogue_responses_for_subnets(family, [{"id": 1, "subnet": listed}])
            responses["config-get"]["arguments"][f"Dhcp{family}"][f"subnet{family}"] = [{"id": 1, "subnet": configured}]
            yield family, responses, listed, configured

    def test_ipv6_pickers_keep_network_order_and_subnet_ids(self):
        subnets = [{"id": 10, "subnet": "2001:db8:10::/64"}, {"id": 2, "subnet": "2001:db8:2::/64"}]
        with stub_kea(_catalogue_responses_for_subnets(6, subnets)) as kea:
            for view_name in ("server_leases6", "server_reservation6_add"):
                response = self.client.get(reverse(f"plugins:netbox_kea:{view_name}", args=[self.server.pk]))
                self.assertContains(response, 'value="2001:db8:2::/64"')
                if view_name == "server_leases6":
                    self.assertContains(response, 'value="2"')
                    self.assertContains(response, 'value="10"')
                body = response.content.decode()
                self.assertLess(body.index('value="2001:db8:2::/64"'), body.index('value="2001:db8:10::/64"'))
            self.assertEqual(kea.commands().count("subnet6-list"), 1)
            self.assertEqual(kea.bodies("subnet6-list")[0]["service"], ["dhcp6"])

    def _assert_refreshes_after_subnet_creation(self, view_name):
        url = reverse(f"plugins:netbox_kea:{view_name}", args=[self.server.pk])
        subnets = [{"id": 1, "subnet": "198.18.1.0/24"}]
        responses = _catalogue_responses_for_subnets(4, subnets)
        with stub_kea(responses) as kea:
            self.assertContains(self.client.get(url), 'value="198.18.1.0/24"')
            self.assertContains(self.client.get(url), 'value="198.18.1.0/24"')
            self.assertEqual(kea.commands().count("subnet4-list"), 1)

        with stub_kea(
            {
                **responses,
                "subnet4-add": {"result": 0, "arguments": {"subnets": [{"id": 2}]}},
                "config-test": {"result": 0},
                "config-write": {"result": 0},
            }
        ) as kea:
            created = self.client.post(
                reverse("plugins:netbox_kea:server_subnet4_add", args=[self.server.pk]),
                {
                    "subnet": "198.18.2.0/24",
                    "subnet_id": "2",
                    "pools": "",
                    "gateway": "",
                    "dns_servers": "",
                    "ntp_servers": "",
                    "shared_network": "",
                },
            )
            self.assertEqual(created.status_code, 302)
            self.assertEqual(kea.commands().count("subnet4-add"), 1)

        subnets.append({"id": 2, "subnet": "198.18.2.0/24"})
        with stub_kea(_catalogue_responses_for_subnets(4, subnets)):
            refreshed = self.client.get(url)
        self.assertContains(refreshed, 'value="198.18.2.0/24"')
        self.assertContains(refreshed, 'value="198.18.1.0/24"')

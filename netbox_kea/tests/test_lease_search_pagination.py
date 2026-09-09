from django.test import override_settings
from django.urls import reverse

from .kea_stub import _catalogue_responses_for_subnets, stub_kea
from .utils import _PLUGINS_CONFIG, _ViewTestBase


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLeaseSearchPagination(_ViewTestBase):
    def test_hostname_second_page_renders_and_enriches_only_remaining_leases(self):
        leases = [
            {
                "ip-address": f"198.18.0.{number}",
                "hw-address": "aa:bb:cc:dd:ee:ff",
                "hostname": "search-host",
                "subnet-id": 1,
                "valid-lft": 3600,
                "cltt": 1_700_000_000,
            }
            for number in range(1, 61)
        ]
        responses = {
            **_catalogue_responses_for_subnets(4, [{"id": 1, "subnet": "198.18.0.0/24"}]),
            "lease4-get-by-hostname": {"result": 0, "arguments": {"leases": leases}},
            "reservation-get": {"result": 3},
        }
        url = reverse("plugins:netbox_kea:server_leases4", args=[self.server.pk])

        with stub_kea(responses):
            response = self.client.get(
                url,
                {"by": "hostname", "q": "search-host", "page": "2", "per_page": "50"},
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].is_valid(), response.context["form"].errors)
        table = response.context["table"]
        expected_addresses = [f"198.18.0.{number}" for number in range(51, 61)]
        self.assertEqual([row.record["ip_address"] for row in table.paginated_rows], expected_addresses)
        enriched = [row["ip_address"] for row in table.data.data if "can_delete" in row]
        self.assertEqual(enriched, expected_addresses)
        for address in expected_addresses:
            self.assertContains(response, address)
        self.assertNotContains(response, "198.18.0.50")
        self.assertTrue(response.context["paginate"])
        self.assertIsNone(response.context["next_page"])

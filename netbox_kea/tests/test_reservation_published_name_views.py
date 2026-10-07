# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Reservation forms take the published name and store the hostname that makes Kea publish it (#304).

The Kea replies come from the configuration recorded from Kea 3.2.0: a global suffix ``dhcp.example.com``,
Subnet 10 with an empty suffix, Subnet 20 in the Shared Network ``office`` with ``office.example.net``,
and Subnet 21 with its own ``office.example.org.``.
"""

from typing import Any
from urllib.parse import urlencode

from django.urls import reverse

from netbox_kea import server_configuration

from .kea_stub import _res_get, queued, stub_kea
from .test_kea_recordings import _recording
from .utils import _ViewTestBase

_HW = "aa:bb:cc:dd:ee:21"
_SUBNETS: dict[int, dict[int, str]] = {
    4: {10: "192.0.2.0/24", 20: "198.51.100.0/25", 21: "198.51.100.128/25"},
    6: {10: "2001:db8:1::/64", 20: "2001:db8:2::/64", 21: "2001:db8:3::/64"},
}


def _recorded(family: int) -> dict[str, Any]:
    recording = _recording(family)
    return {
        "list-commands": recording["list-commands"],
        f"subnet{family}-list": recording[f"subnet{family}-list"],
        "config-get": recording["config-get"],
        "config-test": {"result": 0},
        "config-write": {"result": 0},
    }


def _stored(subnet_id: int, hostname: str) -> dict[str, Any]:
    raw: dict[str, Any] = {"subnet-id": subnet_id, "hw-address": _HW}
    if hostname:
        raw["hostname"] = hostname
    return raw


class _PublishedNameViewTest(_ViewTestBase):
    def setUp(self):
        super().setUp()
        for family in (4, 6):
            server_configuration.invalidate(self.server, family)

    def _form_data(self, family: int, subnet_id: int, hostname: str, **extra: str) -> dict[str, str]:
        return {
            "subnet_cidr": _SUBNETS[family][subnet_id],
            "identifier_type": "hw-address",
            "identifier": _HW,
            "hostname": hostname,
            **extra,
        }


class TestReservationAddPublishedName(_PublishedNameViewTest):
    def _add(self, family: int, subnet_id: int, hostname: str) -> dict[str, Any]:
        intended = _stored(subnet_id, "")
        with stub_kea(
            {
                **_recorded(family),
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(intended),
            }
        ) as kea:
            response = self.client.post(
                reverse(f"plugins:netbox_kea:server_reservation{family}_add", args=[self.server.pk]),
                self._form_data(family, subnet_id, hostname),
            )
        self.assertEqual(response.status_code, 302)
        return kea.bodies("reservation-add")[0]["arguments"]["reservation"]

    def test_add_stores_the_hostname_that_publishes_the_entered_name(self):
        for family in (4, 6):
            for subnet_id, entered, stored in (
                (21, "host.office.example.org", "host"),
                (21, "Host.Office.Example.org.", "Host"),
                (21, "web.example.com", "web.example.com."),
                (21, "printer", "printer"),
                (20, "host.office.example.net", "host"),
                (10, "web.example.com.", "web.example.com"),
            ):
                with self.subTest(family=family, subnet_id=subnet_id, entered=entered):
                    self.assertEqual(self._add(family, subnet_id, entered)["hostname"], stored)

    def test_add_without_a_hostname_sends_none(self):
        self.assertNotIn("hostname", self._add(4, 21, ""))


class TestReservationEditPublishedName(_PublishedNameViewTest):
    def _url(self, family: int, subnet_id: int) -> str:
        url = reverse(f"plugins:netbox_kea:server_reservation{family}_edit", args=[self.server.pk, subnet_id])
        return f"{url}?{urlencode({'identifier_type': 'hw-address', 'identifier': _HW})}"

    def _open(self, family: int, subnet_id: int, stored: str):
        with stub_kea({**_recorded(family), "reservation-get": _res_get(_stored(subnet_id, stored))}):
            return self.client.get(self._url(family, subnet_id))

    def _save(self, family: int, subnet_id: int, stored: str, entered: str, *, form_page=None, config_get=None):
        form_page = form_page or self._open(family, subnet_id, stored)
        current = _stored(subnet_id, stored)
        responses = {
            **_recorded(family),
            "reservation-get": queued(_res_get(current), _res_get(current), _res_get(current)),
            "reservation-update": {"result": 0},
        }
        if config_get is not None:
            responses["config-get"] = config_get
        with stub_kea(responses) as kea:
            response = self.client.post(
                self._url(family, subnet_id),
                self._form_data(
                    family,
                    subnet_id,
                    entered,
                    managed_fingerprint=form_page.context["form"].initial["managed_fingerprint"],
                ),
            )
        return response, kea

    def test_the_form_shows_the_published_name_of_the_stored_hostname(self):
        for family in (4, 6):
            for subnet_id, stored, published in (
                (21, "host", "host.office.example.org"),
                (21, "host.example.org", "host.example.org.office.example.org"),
                (21, "host.example.org.", "host.example.org"),
                (20, "host", "host.office.example.net"),
                (10, "host", "host"),
            ):
                with self.subTest(family=family, subnet_id=subnet_id, stored=stored):
                    response = self._open(family, subnet_id, stored)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context["form"].initial["hostname"], published)
                    self.assertContains(response, f'value="{published}"')

    def test_saving_the_shown_name_sends_the_stored_hostname_unchanged(self):
        # A bare label and a name without a trailing dot keep their stored form until the user edits the name.
        for family in (4, 6):
            for subnet_id, stored in (
                (21, "host"),
                (21, "host.example.org"),
                (21, "Host.office.example.org."),
                (10, "host"),
            ):
                with self.subTest(family=family, subnet_id=subnet_id, stored=stored):
                    form_page = self._open(family, subnet_id, stored)
                    shown = form_page.context["form"].initial["hostname"]
                    response, kea = self._save(family, subnet_id, stored, shown, form_page=form_page)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(
                        kea.bodies("reservation-update")[0]["arguments"]["reservation"]["hostname"], stored
                    )

    def test_an_edited_name_stores_the_hostname_that_publishes_it(self):
        for family in (4, 6):
            for entered, stored in (
                ("db.office.example.org", "db"),
                ("db.example.com", "db.example.com."),
                ("db", "db"),
            ):
                with self.subTest(family=family, entered=entered):
                    response, kea = self._save(family, 21, "host", entered)
                    self.assertEqual(response.status_code, 302)
                    sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
                    self.assertEqual(sent["hostname"], stored)

    def test_clearing_the_name_removes_the_hostname(self):
        response, kea = self._save(4, 21, "host", "")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("hostname", kea.bodies("reservation-update")[0]["arguments"]["reservation"])

    def test_a_changed_suffix_refuses_the_save(self):
        # The form showed the name under one suffix; Kea now has another, so the shown name is stale.
        form_page = self._open(4, 21, "host")
        changed = _recording(4)["config-get"]
        changed = {
            **changed,
            "arguments": {
                **changed["arguments"],
                "Dhcp4": {
                    **changed["arguments"]["Dhcp4"],
                    "shared-networks": [
                        {
                            **network,
                            "subnet4": [
                                {**subnet, "ddns-qualifying-suffix": "moved.example.org"}
                                for subnet in network["subnet4"]
                            ],
                        }
                        for network in changed["arguments"]["Dhcp4"]["shared-networks"]
                    ],
                },
            },
        }
        response, kea = self._save(4, 21, "host", "host.office.example.org", form_page=form_page, config_get=changed)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reservation-update", kea.commands())
        self.assertContains(response, "DDNS qualifying suffix")

    def test_an_unknown_suffix_does_not_open_the_form_of_a_named_reservation(self):
        with stub_kea(
            {
                **_recorded(4),
                "config-get": RuntimeError("config-get failed"),
                "reservation-get": _res_get(_stored(21, "host")),
            }
        ) as kea:
            response = self.client.get(self._url(4, 21))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("reservation-update", kea.commands())

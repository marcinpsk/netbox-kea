# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The Reservation forms take the published name and store the hostname that makes Kea publish it (#304).

The Kea replies come from the configuration recorded from Kea 3.2.0: a global suffix ``dhcp.example.com``,
Subnet 10 with an empty suffix, Subnet 20 in the Shared Network ``office`` with ``office.example.net``,
and Subnet 21 with its own ``office.example.org.`` and one Pool with ``pool.example.org``.
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
# An address of each Subnet outside every Pool, and one in the Pool of Subnet 21.
_ADDRESSES: dict[int, dict[int, str]] = {
    4: {10: "192.0.2.30", 20: "198.51.100.60", 21: "198.51.100.130"},
    6: {10: "2001:db8:1::5", 20: "2001:db8:2::5", 21: "2001:db8:3::5"},
}
_IN_POOL = {4: "198.51.100.210", 6: "2001:db8:3::150"}


def _address_field(family: int) -> str:
    return "ip_address" if family == 4 else "ip_addresses"


def _recorded(family: int) -> dict[str, Any]:
    recording = _recording(family)
    return {
        "list-commands": recording["list-commands"],
        f"subnet{family}-list": recording[f"subnet{family}-list"],
        "config-get": recording["config-get"],
        "config-test": {"result": 0},
        "config-write": {"result": 0},
    }


def _stored(family: int, subnet_id: int, hostname: str, address: str | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {"subnet-id": subnet_id, "hw-address": _HW}
    address = _ADDRESSES[family][subnet_id] if address is None else address
    if address:
        raw.update({"ip-address": address} if family == 4 else {"ip-addresses": [address]})
    if hostname:
        raw["hostname"] = hostname
    return raw


class _PublishedNameViewTest(_ViewTestBase):
    def setUp(self):
        super().setUp()
        for family in (4, 6):
            server_configuration.invalidate(self.server, family)

    def _url(self, family: int, subnet_id: int) -> str:
        url = reverse(f"plugins:netbox_kea:server_reservation{family}_edit", args=[self.server.pk, subnet_id])
        return f"{url}?{urlencode({'identifier_type': 'hw-address', 'identifier': _HW})}"

    def _open(self, family: int, subnet_id: int, stored: str):
        with stub_kea({**_recorded(family), "reservation-get": _res_get(_stored(family, subnet_id, stored))}):
            return self.client.get(self._url(family, subnet_id))

    def _form_data(
        self, family: int, subnet_id: int, hostname: str, address: str | None = None, **extra: str
    ) -> dict[str, str]:
        return {
            "subnet_cidr": _SUBNETS[family][subnet_id],
            _address_field(family): _ADDRESSES[family][subnet_id] if address is None else address,
            "identifier_type": "hw-address",
            "identifier": _HW,
            "hostname": hostname,
            **extra,
        }


class TestReservationAddPublishedName(_PublishedNameViewTest):
    def _post_add(self, family: int, subnet_id: int, hostname: str, address: str | None = None):
        intended = _stored(family, subnet_id, "", address)
        with stub_kea(
            {
                **_recorded(family),
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(intended),
            }
        ) as kea:
            response = self.client.post(
                reverse(f"plugins:netbox_kea:server_reservation{family}_add", args=[self.server.pk]),
                self._form_data(family, subnet_id, hostname, address),
            )
        return response, kea

    def _add(self, family: int, subnet_id: int, hostname: str, address: str | None = None) -> dict[str, Any]:
        response, kea = self._post_add(family, subnet_id, hostname, address)
        self.assertEqual(response.status_code, 302)
        return kea.bodies("reservation-add")[0]["arguments"]["reservation"]

    def test_add_stores_the_hostname_that_publishes_the_entered_name(self):
        for family in (4, 6):
            for subnet_id, entered, stored, address in (
                (21, "host.office.example.org", "host", None),
                (21, "Host.Office.Example.org.", "Host", None),
                (21, "web.example.com", "web.example.com.", None),
                (21, "printer", "printer", None),
                (21, "host.pool.example.org", "host", _IN_POOL[family]),
                (21, "host.office.example.org", "host.office.example.org.", _IN_POOL[family]),
                (20, "host.office.example.net", "host", None),
                (10, "web.example.com.", "web.example.com", None),
            ):
                with self.subTest(family=family, subnet_id=subnet_id, entered=entered, address=address):
                    self.assertEqual(self._add(family, subnet_id, entered, address)["hostname"], stored)

    def test_a_name_without_an_address_in_a_subnet_with_a_pool_suffix_is_refused(self):
        # Kea takes the suffix from the Pool of the dynamic lease, so no stored form is known.
        response, kea = self._post_add(4, 21, "host.office.example.org", "")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reservation-add", kea.commands())

    def test_add_without_a_hostname_sends_none(self):
        self.assertNotIn("hostname", self._add(4, 21, ""))
        self.assertNotIn("hostname", self._add(4, 21, "", ""))


class TestReservationEditPublishedName(_PublishedNameViewTest):
    def _save(
        self,
        family: int,
        subnet_id: int,
        stored: str,
        entered: str,
        *,
        form_page=None,
        config_get=None,
        address: str | None = None,
    ):
        form_page = form_page or self._open(family, subnet_id, stored)
        current = _stored(family, subnet_id, stored)
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
                    address,
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

    def test_a_moved_address_stores_the_shown_name_under_the_suffix_of_its_pool(self):
        # The shown name is what the user keeps: moving into the Pool must not change the name that clients get.
        for family in (4, 6):
            with self.subTest(family=family):
                response, kea = self._save(family, 21, "host", "host.office.example.org", address=_IN_POOL[family])
                self.assertEqual(response.status_code, 302)
                sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
                self.assertEqual(sent["hostname"], "host.office.example.org.")

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
        self.assertIn(
            "The DDNS qualifying suffix of the Subnet changed after the edit form was opened.",
            " ".join(response.context["form"].non_field_errors()),
        )

    def _save_addressless(self, family: int, entered: str):
        # A Pool of Subnet 21 sets a suffix, so the name of a Reservation without an address depends on the lease.
        current = _stored(family, 21, "host", address="")
        with stub_kea({**_recorded(family), "reservation-get": _res_get(current)}):
            form_page = self.client.get(self._url(family, 21))
        self.assertEqual(form_page.status_code, 200)
        self.assertEqual(form_page.context["form"].initial["hostname"], "host")
        with stub_kea(
            {
                **_recorded(family),
                "reservation-get": queued(_res_get(current), _res_get(current), _res_get(current)),
                "reservation-update": {"result": 0},
            }
        ) as kea:
            response = self.client.post(
                self._url(family, 21),
                self._form_data(
                    family,
                    21,
                    entered,
                    "",
                    managed_fingerprint=form_page.context["form"].initial["managed_fingerprint"],
                ),
            )
        return response, kea

    def test_a_reservation_without_an_address_keeps_its_stored_hostname_under_a_pool_suffix(self):
        for family in (4, 6):
            with self.subTest(family=family):
                response, kea = self._save_addressless(family, "host")
                self.assertEqual(response.status_code, 302)
                sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
                self.assertEqual(sent["hostname"], "host")

    def test_a_reservation_without_an_address_refuses_a_new_name_under_a_pool_suffix(self):
        response, kea = self._save_addressless(4, "db")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reservation-update", kea.commands())
        self.assertIn(
            "Save an address first, then change the hostname.",
            " ".join(response.context["form"].non_field_errors()),
        )

    def test_an_unknown_suffix_does_not_open_the_form_of_a_named_reservation(self):
        with stub_kea(
            {
                **_recorded(4),
                "config-get": RuntimeError("config-get failed"),
                "reservation-get": _res_get(_stored(4, 21, "host")),
            }
        ) as kea:
            response = self.client.get(self._url(4, 21))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("reservation-update", kea.commands())


class TestPublishedNamePreview(_PublishedNameViewTest):
    """The preview endpoint renders the name that Kea publishes, from the cached Subnet Catalogue."""

    def _preview(self, family: int, **params: str):
        url = reverse(f"plugins:netbox_kea:server_reservation{family}_published_name", args=[self.server.pk])
        return self.client.get(url, params, headers={"HX-Request": "true"})

    def _subnet(self, family: int, subnet_id: int, address: str | None = None) -> dict[str, str]:
        address = _ADDRESSES[family][subnet_id] if address is None else address
        return {"subnet_cidr": _SUBNETS[family][subnet_id], _address_field(family): address}

    def test_the_preview_shows_the_published_name(self):
        for family in (4, 6):
            for subnet_id, entered, published, address in (
                (21, "printer", "printer.office.example.org", None),
                (21, "web.example.com", "web.example.com", None),
                (21, "DB.Office.Example.org.", "db.office.example.org", None),
                (21, "printer", "printer.pool.example.org", _IN_POOL[family]),
                (20, "printer", "printer.office.example.net", None),
                (20, "printer", "printer.office.example.net", ""),
                (10, "printer", "printer", None),
            ):
                with (
                    self.subTest(family=family, subnet_id=subnet_id, entered=entered, address=address),
                    stub_kea(_recorded(family)),
                ):
                    response = self._preview(family, **self._subnet(family, subnet_id, address), hostname=entered)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context["published"], published)
                    self.assertContains(response, f"<code>{published}</code>", html=False)

    def test_the_preview_shows_the_suffix_without_its_trailing_dot(self):
        with stub_kea(_recorded(4)):
            response = self._preview(4, **self._subnet(4, 21), hostname="printer")
        self.assertContains(response, "with the DDNS qualifying suffix <code>office.example.org</code>.")
        self.assertNotContains(response, "office.example.org.</code>")

    def test_the_preview_reads_the_cached_catalogue(self):
        with stub_kea(_recorded(4)) as kea:
            self._preview(4, **self._subnet(4, 21), hostname="printer")
            commands = kea.commands()
            response = self._preview(4, **self._subnet(4, 20), hostname="printer")
            self.assertEqual(kea.commands(), commands)
        self.assertEqual(response.context["published"], "printer.office.example.net")

    def test_the_preview_without_a_name_or_a_known_subnet(self):
        with stub_kea(_recorded(4)):
            self.assertEqual(self._preview(4, subnet_cidr=_SUBNETS[4][21], hostname="").content.strip(), b"")
            for cidr in ("203.0.113.0/24", "not-a-subnet", ""):
                with self.subTest(cidr=cidr):
                    response = self._preview(4, subnet_cidr=cidr, hostname="printer")
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("published", response.context)
                    self.assertContains(response, "Enter a Subnet of this server")

    def test_an_unknown_suffix_says_so(self):
        with stub_kea({**_recorded(4), "config-get": RuntimeError("config-get failed")}):
            response = self._preview(4, **self._subnet(4, 21), hostname="printer")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("published", response.context)
        self.assertContains(response, "The DDNS qualifying suffix for this Subnet and address is unknown")

    def test_a_pool_suffix_without_an_address_says_the_name_is_unknown(self):
        for family in (4, 6):
            with self.subTest(family=family), stub_kea(_recorded(family)):
                for address in ("", "not-an-address"):
                    response = self._preview(family, **self._subnet(family, 21, address), hostname="printer")
                    self.assertNotIn("published", response.context)
                    self.assertContains(response, "The DDNS qualifying suffix for this Subnet and address is unknown")

    def test_the_preview_needs_the_change_permission(self):
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        self.user.is_superuser = False
        self.user.save()
        permission = ObjectPermission.objects.create(name="view-published-name-server", actions=["view"])
        permission.object_types.add(ContentType.objects.get_for_model(type(self.server)))
        permission.users.add(self.user)
        with stub_kea(_recorded(4)) as kea:
            response = self._preview(4, subnet_cidr=_SUBNETS[4][21], hostname="printer")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(kea.commands(), [])

    def test_the_forms_request_the_preview(self):
        preview = reverse("plugins:netbox_kea:server_reservation4_published_name", args=[self.server.pk])
        with stub_kea(_recorded(4)):
            add = self.client.get(reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk]))
        self.assertContains(add, f'hx-get="{preview}"')
        self.assertContains(add, 'hx-include="#id_hostname, #id_subnet_cidr, #id_ip_address"')
        edit = self._open(4, 21, "host")
        self.assertContains(edit, f'hx-get="{preview}?{urlencode({"subnet_cidr": _SUBNETS[4][21]})}"')

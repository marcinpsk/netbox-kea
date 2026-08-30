# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import json
from unittest.mock import patch
from urllib.parse import urlencode

import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from .kea_stub import _res_get, _res_page, _reservation_mutation_commands, queued, stub_kea
from .utils import _ViewTestBase


def _live_config(version: int, subnet_id: int, cidr: str, identifiers: list[str]) -> dict:
    return {
        "result": 0,
        "arguments": {
            f"Dhcp{version}": {
                f"subnet{version}": [{"id": subnet_id, "subnet": cidr}],
                "host-reservation-identifiers": identifiers,
                "hooks-libraries": [{"library": "/usr/lib/kea/hooks/libdhcp_flex_id.so"}],
            },
            "hash": "reservation-mutation-config",
        },
    }


def _mutation_responses(version: int, subnet_id: int, cidr: str, identifiers: list[str]) -> dict:
    return {
        "list-commands": _reservation_mutation_commands(),
        f"subnet{version}-list": {
            "result": 0,
            "arguments": {"subnets": [{"id": subnet_id, "subnet": cidr}]},
        },
        "config-get": _live_config(version, subnet_id, cidr, identifiers),
        "config-test": {"result": 0},
        "config-write": {"result": 0},
    }


def _identity_query(identifier: str = "aa:bb:cc:dd:ee:ff", identifier_type: str = "hw-address") -> str:
    """The identity query string every reservation edit and delete route requires."""
    return urlencode({"identifier_type": identifier_type, "identifier": identifier})


class TestReservationMutationViews(_ViewTestBase):
    def test_add_form_uses_live_identifier_choices_and_explains_relay_remote_id(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address", "flex-id"])
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

        with stub_kea(responses):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hw-address")
        self.assertContains(response, "flex-id")
        self.assertNotContains(response, 'value="remote-id"')
        self.assertContains(response, "relay remote ID")
        self.assertContains(response, "Flexible Identifiers for Host Reservations")

    def test_capability_failure_leaves_the_add_form_visible_but_disables_save(self):
        responses = {
            "list-commands": RuntimeError("capability read failed"),
            "subnet4-list": {
                "result": 0,
                "arguments": {"subnets": [{"id": 20, "subnet": "198.18.0.0/24"}]},
            },
        }
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

        with stub_kea(responses):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mutation is unavailable")
        # Assert the save control itself: a bare "disabled" also matches CSS class
        # names and unrelated markup, so it passed even with the button enabled.
        self.assertContains(
            response,
            '<button type="submit" class="btn btn-primary" disabled aria-disabled="true">',
        )

    def test_live_capabilities_leave_the_save_control_enabled(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])

        with stub_kea(responses):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<button type="submit" class="btn btn-primary">')
        self.assertNotContains(
            response,
            '<button type="submit" class="btn btn-primary" disabled aria-disabled="true">',
        )

    def test_add_form_warns_when_configuration_persistence_is_disabled(self):
        self.server.persist_config = False
        self.server.save(update_fields=("persist_config",))
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])

        with stub_kea(responses):
            response = self.client.get(
                reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk]),
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configuration persistence is disabled.")

    def test_create_uses_the_typed_operation_and_emits_one_typed_signal(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
            "hostname": "host.example.invalid",
        }
        responses.update(
            {
                "subnet4-get": {"result": 3},
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(raw),
            }
        )
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        def failing_receiver(sender, **kwargs):
            raise RuntimeError("optional signal receiver failed")

        from netbox_kea.signals import reservation_created

        reservation_created.connect(receiver)
        reservation_created.connect(failing_receiver)
        try:
            with stub_kea(responses) as kea:
                response = self.client.post(
                    reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk]),
                    {
                        "subnet_cidr": "198.18.0.0/24",
                        "ip_address": "198.18.0.20",
                        "identifier_type": "hw-address",
                        "identifier": "AA-BB-CC-DD-EE-FF",
                        "hostname": "host.example.invalid",
                    },
                )
        finally:
            reservation_created.disconnect(receiver)
            reservation_created.disconnect(failing_receiver)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("reservation-add"), 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["server"], self.server)
        self.assertIs(received[0]["request"], response.wsgi_request)
        self.assertEqual(received[0]["dhcp_version"], 4)
        self.assertIsNone(received[0]["before"])
        self.assertEqual(received[0]["after"].identity.value, "aa:bb:cc:dd:ee:ff")
        from extras.models import JournalEntry

        journal = JournalEntry.objects.get(assigned_object_id=self.server.pk)
        self.assertIn("Reservation created", journal.comments)
        self.assertIn("aa:bb:cc:dd:ee:ff", journal.comments)

    def test_create_logs_overlap_probe_failure_as_a_warning(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
        }
        responses.update({"reservation-add": {"result": 0}, "reservation-get": _res_get(raw)})

        with (
            patch(
                "netbox_kea.views.reservation_mutations._warn_reservation_pool_overlap",
                autospec=True,
                side_effect=ValueError("malformed pool"),
            ),
            self.assertLogs("netbox_kea.views.reservation_mutations", level="WARNING") as logs,
            stub_kea(responses),
        ):
            response = self.client.post(
                reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk]),
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "198.18.0.20",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("overlap" in message.lower() for message in logs.output))

    def test_create_reports_failed_persistence_after_confirmed_application(self):
        from django.contrib.messages import get_messages
        from extras.models import JournalEntry

        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
        }
        responses.update(
            {
                "subnet4-get": {"result": 3},
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(raw),
                "config-test": {"result": 0},
                "config-write": {"result": 1, "text": "write failed"},
            }
        )
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        from netbox_kea.signals import reservation_created

        reservation_created.connect(receiver)
        try:
            with stub_kea(responses) as kea:
                response = self.client.post(
                    reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk]),
                    {
                        "subnet_cidr": "198.18.0.0/24",
                        "ip_address": "198.18.0.20",
                        "identifier_type": "hw-address",
                        "identifier": "aa:bb:cc:dd:ee:ff",
                    },
                )
        finally:
            reservation_created.disconnect(receiver)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("reservation-add"), 1)
        self.assertEqual(kea.commands().count("config-write"), 1)
        self.assertIn(
            "Kea applied the change, but could not persist it to disk.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )
        self.assertEqual(len(received), 1)
        self.assertIsNone(received[0]["before"])
        self.assertIsNotNone(received[0]["after"])
        self.assertTrue(JournalEntry.objects.filter(assigned_object_id=self.server.pk).exists())

    def test_create_skips_immediate_ipam_sync_without_ipam_write_permission(self):
        from django.contrib.auth import get_user_model
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress
        from users.models import ObjectPermission

        limited = get_user_model().objects.create_user(username="reservation_no_ipam", password="x")
        permission = ObjectPermission.objects.create(
            name="change-server-reservation-no-ipam",
            actions=["view", "change"],
        )
        permission.object_types.add(ContentType.objects.get_for_model(type(self.server)))
        permission.users.add(limited)
        self.client.force_login(limited)
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
        }
        responses.update(
            {
                "subnet4-get": {"result": 3},
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(raw),
            }
        )

        with stub_kea(responses) as kea:
            response = self.client.post(
                reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk]),
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "198.18.0.20",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "sync_to_netbox": "on",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("reservation-add"), 1)
        self.assertFalse(IPAddress.objects.filter(address="198.18.0.20/24").exists())

    def test_create_redirects_and_warns_when_ipam_validation_fails(self):
        from django.contrib.messages import get_messages
        from django.core.exceptions import ValidationError

        def fail_sync(_reservation, *, cleanup, force):
            raise ValidationError("IPAM validation failed")

        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
        }
        responses.update(
            {
                "subnet4-get": {"result": 3},
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(raw),
            }
        )

        with (
            patch("netbox_kea.views.reservation_mutations.sync_reservation_to_netbox", new=fail_sync),
            stub_kea(responses) as kea,
        ):
            response = self.client.post(
                reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk]),
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "198.18.0.20",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "sync_to_netbox": "on",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("reservation-add"), 1)
        self.assertIn(
            "The Reservation changed, but NetBox IPAM synchronization failed.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_edit_rejects_a_stale_managed_fingerprint_without_an_update(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        original = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
            "hostname": "old.example.invalid",
        }
        changed = {**original, "hostname": "changed.example.invalid"}
        url = reverse(
            "plugins:netbox_kea:server_reservation4_edit",
            args=[self.server.pk, 20],
        )
        query = _identity_query()

        with stub_kea({**responses, "reservation-get": _res_get(original)}):
            form_page = self.client.get(f"{url}?{query}")
        fingerprint = form_page.context["form"].initial["managed_fingerprint"]

        with stub_kea({**responses, "reservation-get": _res_get(changed)}) as kea:
            response = self.client.post(
                f"{url}?{query}",
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "198.18.0.21",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "hostname": "new.example.invalid",
                    "managed_fingerprint": fingerprint,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "changed after the edit form was opened")
        self.assertNotIn("reservation-update", kea.commands())

    def test_canonical_identity_route_rejects_missing_unsupported_and_malformed_identity(self):
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        queries = (
            "",
            "identifier_type=remote-id&identifier=relay",
            "identifier_type=hw-address&identifier=not-a-mac",
        )
        with stub_kea({}) as kea:
            for query in queries:
                with self.subTest(query=query):
                    response = self.client.get(f"{url}?{query}")
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(kea.commands(), [])

    def test_edit_returns_not_found_for_an_unknown_subnet_or_identity(self):
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        query = _identity_query()
        no_subnet = _mutation_responses(4, 21, "198.18.1.0/24", ["hw-address"])
        with stub_kea(no_subnet):
            response = self.client.get(f"{url}?{query}")
        self.assertEqual(response.status_code, 404)

        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        responses["reservation-get"] = {"result": 3}
        with stub_kea(responses):
            response = self.client.get(f"{url}?{query}")
        self.assertEqual(response.status_code, 404)

    def test_create_ipv6_preserves_all_addresses_prefixes_and_options(self):
        responses = _mutation_responses(6, 10, "2001:db8::/64", ["duid"])
        raw = {
            "subnet-id": 10,
            "duid": "00:01:02:03",
            "ip-addresses": ["2001:db8::20", "2001:db8::21"],
            "prefixes": ["2001:db8:100::/56"],
            "hostname": "host6.example.invalid",
            "option-data": [{"name": "dns-servers", "data": "2001:db8::53", "always-send": True}],
        }
        responses.update(
            {
                "subnet6-get": {"result": 3},
                "reservation-add": {"result": 0},
                "reservation-get": _res_get(raw),
            }
        )

        with stub_kea(responses) as kea:
            response = self.client.post(
                reverse("plugins:netbox_kea:server_reservation6_add", args=[self.server.pk]),
                {
                    "subnet_cidr": "2001:db8::/64",
                    "ip_addresses": "2001:db8::20,2001:db8::21",
                    "prefixes": "2001:db8:100::/56",
                    "identifier_type": "duid",
                    "identifier": "00:01:02:03",
                    "hostname": "host6.example.invalid",
                    "options-TOTAL_FORMS": "1",
                    "options-INITIAL_FORMS": "0",
                    "options-MIN_NUM_FORMS": "0",
                    "options-MAX_NUM_FORMS": "1000",
                    "options-0-name": "dns-servers",
                    "options-0-data": "2001:db8::53",
                    "options-0-always_send": "on",
                },
            )

        self.assertEqual(response.status_code, 302)
        sent = kea.bodies("reservation-add")[0]["arguments"]["reservation"]
        self.assertEqual(sent, raw)

    def test_create_keeps_the_form_visible_when_kea_rejects_the_mutation(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        responses.update(
            {
                "subnet4-get": {"result": 3},
                "reservation-add": {"result": 1, "text": "duplicate reservation"},
            }
        )

        with stub_kea(responses):
            response = self.client.post(
                reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk]),
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "198.18.0.20",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kea reported an error")

    def test_edit_ipv6_updates_mutable_facts_and_emits_typed_signal(self):
        responses = _mutation_responses(6, 10, "2001:db8::/64", ["duid"])
        current = {
            "subnet-id": 10,
            "duid": "00:01:02:03",
            "ip-addresses": ["2001:db8::20"],
            "prefixes": ["2001:db8:100::/56"],
            "hostname": "old6.example.invalid",
        }
        intended = {
            **current,
            "ip-addresses": ["2001:db8::21", "2001:db8::22"],
            "prefixes": ["2001:db8:200::/56"],
            "hostname": "new6.example.invalid",
        }
        url = reverse("plugins:netbox_kea:server_reservation6_edit", args=[self.server.pk, 10])
        query = _identity_query("00:01:02:03", "duid")
        with stub_kea({**responses, "reservation-get": _res_get(current)}):
            form_page = self.client.get(f"{url}?{query}")
        fingerprint = form_page.context["form"].initial["managed_fingerprint"]
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        from netbox_kea.signals import reservation_updated

        reservation_updated.connect(receiver)
        try:
            with stub_kea(
                {
                    **responses,
                    "reservation-get": queued(_res_get(current), _res_get(current), _res_get(intended)),
                    "reservation-update": {"result": 0},
                }
            ):
                response = self.client.post(
                    f"{url}?{query}",
                    {
                        "subnet_cidr": "2001:db8::/64",
                        "ip_addresses": "2001:db8::21,2001:db8::22",
                        "prefixes": "2001:db8:200::/56",
                        "identifier_type": "duid",
                        "identifier": "00:01:02:03",
                        "hostname": "new6.example.invalid",
                        "managed_fingerprint": fingerprint,
                    },
                )
        finally:
            reservation_updated.disconnect(receiver)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["server"], self.server)
        self.assertIs(received[0]["request"], response.wsgi_request)
        self.assertEqual(received[0]["dhcp_version"], 6)
        self.assertIsNotNone(received[0]["before"])
        self.assertEqual([str(address) for address in received[0]["after"].addresses], intended["ip-addresses"])

    def test_edit_preserves_unexposed_dhcp_option_metadata(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        option = {
            "code": 224,
            "space": "vendor-example",
            "data": "opaque-value",
            "csv-format": False,
            "always-send": False,
            "never-send": True,
            "user-context": {"source": "external"},
        }
        current = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "old.example.invalid",
            "option-data": [option],
        }
        intended = {**current, "hostname": "new.example.invalid"}
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        query = _identity_query()
        with stub_kea({**responses, "reservation-get": _res_get(current)}):
            form_page = self.client.get(f"{url}?{query}")
        fingerprint = form_page.context["form"].initial["managed_fingerprint"]

        with stub_kea(
            {
                **responses,
                "reservation-get": queued(_res_get(current), _res_get(current), _res_get(intended)),
                "reservation-update": {"result": 0},
            }
        ) as kea:
            response = self.client.post(
                f"{url}?{query}",
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "hostname": "new.example.invalid",
                    "managed_fingerprint": fingerprint,
                    "options-TOTAL_FORMS": "2",
                    "options-INITIAL_FORMS": "1",
                    "options-MIN_NUM_FORMS": "0",
                    "options-MAX_NUM_FORMS": "1000",
                    "options-0-name": "224",
                    "options-0-data": "opaque-value",
                    "options-1-name": "",
                    "options-1-data": "",
                },
            )

        self.assertEqual(response.status_code, 302)
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(sent["option-data"], [option])

    def test_edit_preserves_each_options_metadata_when_rows_are_reordered(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        first = {
            "name": "vendor-one",
            "code": 224,
            "space": "vendor-example",
            "data": "first",
            "csv-format": False,
            "never-send": True,
        }
        second = {
            "name": "vendor-two",
            "code": 225,
            "space": "vendor-example",
            "data": "second",
            "csv-format": False,
            "always-send": True,
        }
        current = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "old.example.invalid",
            "option-data": [first, second],
        }
        intended = {**current, "hostname": "new.example.invalid", "option-data": [second, first]}
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        query = _identity_query()
        with stub_kea({**responses, "reservation-get": _res_get(current)}):
            form_page = self.client.get(f"{url}?{query}")
        fingerprint = form_page.context["form"].initial["managed_fingerprint"]

        with stub_kea(
            {
                **responses,
                "reservation-get": queued(_res_get(current), _res_get(current), _res_get(intended)),
                "reservation-update": {"result": 0},
            }
        ) as kea:
            response = self.client.post(
                f"{url}?{query}",
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "hostname": "new.example.invalid",
                    "managed_fingerprint": fingerprint,
                    "options-TOTAL_FORMS": "2",
                    "options-INITIAL_FORMS": "2",
                    "options-MIN_NUM_FORMS": "0",
                    "options-MAX_NUM_FORMS": "1000",
                    "options-0-name": "vendor-two",
                    "options-0-data": "second",
                    "options-0-always_send": "on",
                    "options-1-name": "vendor-one",
                    "options-1-data": "first",
                },
            )

        self.assertEqual(response.status_code, 302)
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(sent["option-data"], [second, first])

    def test_edit_replaces_option_metadata_when_its_name_changes(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        current_option = {
            "name": "vendor-old",
            "code": 224,
            "space": "vendor-example",
            "data": "old-value",
            "csv-format": False,
            "never-send": True,
        }
        replacement = {"name": "vendor-renamed", "data": "new-value"}
        current = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "option-data": [current_option],
        }
        intended = {**current, "option-data": [replacement]}
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        query = _identity_query()
        with stub_kea({**responses, "reservation-get": _res_get(current)}):
            form_page = self.client.get(f"{url}?{query}")
        fingerprint = form_page.context["form"].initial["managed_fingerprint"]

        with stub_kea(
            {
                **responses,
                "reservation-get": queued(_res_get(current), _res_get(current), _res_get(intended)),
                "reservation-update": {"result": 0},
            }
        ) as kea:
            response = self.client.post(
                f"{url}?{query}",
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "managed_fingerprint": fingerprint,
                    "options-TOTAL_FORMS": "1",
                    "options-INITIAL_FORMS": "1",
                    "options-MIN_NUM_FORMS": "0",
                    "options-MAX_NUM_FORMS": "1000",
                    "options-0-original_index": "0",
                    "options-0-name": "vendor-renamed",
                    "options-0-data": "new-value",
                },
            )

        self.assertEqual(response.status_code, 302)
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(sent["option-data"], [replacement])

    def test_edit_preserves_option_spaces_when_duplicate_names_are_reordered(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        first = {
            "name": "vendor-option",
            "code": 224,
            "space": "vendor-one",
            "data": "first",
            "csv-format": False,
            "never-send": True,
        }
        second = {
            "name": "vendor-option",
            "code": 224,
            "space": "vendor-two",
            "data": "second",
            "csv-format": False,
            "always-send": True,
        }
        current = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "hostname": "old.example.invalid",
            "option-data": [first, second],
        }
        intended = {**current, "hostname": "new.example.invalid", "option-data": [second, first]}
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        query = _identity_query()
        with stub_kea({**responses, "reservation-get": _res_get(current)}):
            form_page = self.client.get(f"{url}?{query}")
        fingerprint = form_page.context["form"].initial["managed_fingerprint"]
        self.assertContains(form_page, 'name="options-0-original_index"')
        self.assertContains(form_page, 'name="options-1-original_index"')

        with stub_kea(
            {
                **responses,
                "reservation-get": queued(_res_get(current), _res_get(current), _res_get(intended)),
                "reservation-update": {"result": 0},
            }
        ) as kea:
            response = self.client.post(
                f"{url}?{query}",
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "hostname": "new.example.invalid",
                    "managed_fingerprint": fingerprint,
                    "options-TOTAL_FORMS": "2",
                    "options-INITIAL_FORMS": "2",
                    "options-MIN_NUM_FORMS": "0",
                    "options-MAX_NUM_FORMS": "1000",
                    "options-0-original_index": "1",
                    "options-0-name": "vendor-option",
                    "options-0-data": "second",
                    "options-0-always_send": "on",
                    "options-1-original_index": "0",
                    "options-1-name": "vendor-option",
                    "options-1-data": "first",
                },
            )

        self.assertEqual(response.status_code, 302)
        sent = kea.bodies("reservation-update")[0]["arguments"]["reservation"]
        self.assertEqual(sent["option-data"], [second, first])

    def test_edit_rejects_a_tampered_signed_fingerprint(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        current = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
        }
        responses["reservation-get"] = _res_get(current)
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        query = _identity_query()

        with stub_kea(responses) as kea:
            response = self.client.post(
                f"{url}?{query}",
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "198.18.0.21",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "managed_fingerprint": "tampered",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid or expired")
        self.assertNotIn("reservation-update", kea.commands())

    def test_delete_confirmation_and_post_use_canonical_identity(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:ff",
            "ip-address": "198.18.0.20",
        }
        url = reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, 20])
        query = _identity_query()

        with stub_kea({**responses, "reservation-get": _res_get(raw)}):
            confirmation = self.client.get(f"{url}?{query}")
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "hw-address aa:bb:cc:dd:ee:ff")

        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        from netbox_kea.signals import reservation_deleted

        reservation_deleted.connect(receiver)
        try:
            with stub_kea(
                {
                    **responses,
                    "reservation-get": queued(_res_get(raw), _res_get(raw), {"result": 3}),
                    "reservation-del": {"result": 0},
                }
            ) as kea:
                response = self.client.post(f"{url}?{query}")
        finally:
            reservation_deleted.disconnect(receiver)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(kea.commands().count("reservation-del"), 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["server"], self.server)
        self.assertIs(received[0]["request"], response.wsgi_request)
        self.assertEqual(received[0]["dhcp_version"], 4)
        self.assertIsNotNone(received[0]["before"])
        self.assertIsNone(received[0]["after"])

    def test_delete_confirmation_redirects_when_kea_is_unreachable(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        responses["subnet4-list"] = requests.ConnectionError("Kea unavailable")
        url = reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, 20])
        query = _identity_query()

        with stub_kea(responses):
            response = self.client.get(f"{url}?{query}")

        self.assertRedirects(
            response,
            reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk]),
            fetch_redirect_response=False,
        )

    def test_delete_confirmation_returns_not_found_for_an_unknown_identity(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        responses["reservation-get"] = {"result": 3}
        url = reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, 20])
        query = _identity_query()

        with stub_kea(responses):
            response = self.client.get(f"{url}?{query}")

        self.assertEqual(response.status_code, 404)

    def test_delete_fails_closed_when_mutation_capabilities_are_incomplete(self):
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        responses["list-commands"] = {"result": 0, "arguments": ["reservation-get"]}
        url = reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, 20])
        query = _identity_query()

        with stub_kea(responses) as kea:
            response = self.client.post(f"{url}?{query}")

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("reservation-del", kea.commands())


class TestReservationDocumentImport(_ViewTestBase):
    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_import", args=[self.server.pk])

    def test_validation_reports_all_errors_before_any_live_request(self):
        document = """version: 1
reservations:
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: hw-address, value: invalid}
    addresses: [2001:db8::1]
    delegated_prefixes: []
    hostname: 42
    options: []
"""

        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"format": "yaml", "document": document})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reservations[0].identity.value")
        self.assertContains(response, "reservations[0].addresses[0]")
        self.assertContains(response, "reservations[0].hostname")
        self.assertEqual(kea.commands(), [])

    def test_validation_reports_wrong_family_with_other_document_errors(self):
        document = """version: 1
reservations:
  - family: 6
    scope: {type: in-subnet, subnet: {cidr: 2001:db8::/64}}
    identity: {type: duid, value: "00:01:02:03"}
    addresses: []
    delegated_prefixes: []
    hostname: ""
    options: []
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: hw-address, value: "aa:bb:cc:dd:ee:01"}
    addresses: []
    delegated_prefixes: []
    hostname: 42
    options: []
"""

        with stub_kea({}) as kea:
            response = self.client.post(self._url(), {"format": "yaml", "document": document})

        self.assertEqual(response.status_code, 200)
        diagnostics = response.context["result"]["diagnostics"]
        self.assertEqual(
            {(diagnostic.code, diagnostic.source_position) for diagnostic in diagnostics},
            {
                ("wrong-family", "reservations[0].family"),
                ("invalid-hostname", "reservations[1].hostname"),
            },
        )
        self.assertEqual(kea.commands(), [])

    def test_json_upload_uses_typed_creation_side_effects_without_ipam_sync(self):
        from extras.models import JournalEntry
        from ipam.models import IPAddress

        option = {
            "code": None,
            "name": "domain-name",
            "space": None,
            "data": "example.invalid",
            "csv_format": False,
            "always_send": True,
            "never_send": False,
        }
        document = {
            "version": 1,
            "reservations": [
                {
                    "family": 4,
                    "scope": {"type": "in-subnet", "subnet": {"cidr": "198.18.0.0/24"}},
                    "identity": {"type": "hw-address", "value": "aa:bb:cc:dd:ee:01"},
                    "addresses": ["198.18.0.20"],
                    "delegated_prefixes": [],
                    "hostname": "upload.example.invalid",
                    "options": [option],
                }
            ],
        }
        raw = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:01",
            "ip-address": "198.18.0.20",
            "hostname": "upload.example.invalid",
            "option-data": [
                {
                    "name": "domain-name",
                    "data": "example.invalid",
                    "csv-format": False,
                    "always-send": True,
                    "never-send": False,
                }
            ],
        }
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        responses.update({"reservation-add": {"result": 0}, "reservation-get": _res_get(raw)})
        upload = SimpleUploadedFile(
            "reservations.json",
            json.dumps(document).encode(),
            content_type="application/json",
        )

        with stub_kea(responses) as kea:
            response = self.client.post(self._url(), {"format": "json", "document_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["created"], 1)
        created = kea.bodies("reservation-add")[0]["arguments"]["reservation"]
        self.assertEqual(created, raw)
        self.assertEqual(
            JournalEntry.objects.get(assigned_object_id=self.server.pk).comments,
            "Reservation created: hw-address aa:bb:cc:dd:ee:01; 198.18.0.20",
        )
        self.assertFalse(IPAddress.objects.filter(address="198.18.0.20/24").exists())

    def test_import_stops_after_first_kea_failure_and_emits_only_confirmed_signal(self):
        document = """version: 1
reservations:
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: hw-address, value: "aa:bb:cc:dd:ee:01"}
    addresses: [198.18.0.20]
    delegated_prefixes: []
    hostname: first.example.invalid
    options: []
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: hw-address, value: "aa:bb:cc:dd:ee:02"}
    addresses: [198.18.0.21]
    delegated_prefixes: []
    hostname: second.example.invalid
    options: []
  - family: 4
    scope: {type: in-subnet, subnet: {cidr: 198.18.0.0/24}}
    identity: {type: hw-address, value: "aa:bb:cc:dd:ee:03"}
    addresses: [198.18.0.22]
    delegated_prefixes: []
    hostname: third.example.invalid
    options: []
"""
        first = {
            "subnet-id": 20,
            "hw-address": "aa:bb:cc:dd:ee:01",
            "ip-address": "198.18.0.20",
            "hostname": "first.example.invalid",
        }
        responses = _mutation_responses(4, 20, "198.18.0.0/24", ["hw-address"])
        responses.update(
            {
                "subnet4-get": {"result": 3},
                "reservation-add": queued({"result": 0}, {"result": 1, "text": "conflict"}),
                "reservation-get": _res_get(first),
            }
        )
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        from netbox_kea.signals import reservation_created

        reservation_created.connect(receiver)
        try:
            with stub_kea(responses) as kea:
                response = self.client.post(self._url(), {"format": "yaml", "document": document})
        finally:
            reservation_created.disconnect(receiver)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["created"], 1)
        self.assertEqual(response.context["result"]["failed"], 1)
        self.assertEqual(response.context["result"]["not_attempted"], 1)
        self.assertEqual(kea.commands().count("reservation-add"), 2)
        self.assertEqual(len(received), 1)


class TestMutationCapabilityGate(_ViewTestBase):
    """A direct POST must never reach Kea when no mutation capability is confirmed.

    Each handler must redirect before it validates a form or reads a mutation target.
    These tests also assert that no ``reservation-*`` mutation command is issued, so a
    replayed or hand-built POST cannot slip past the disabled save control.
    """

    #: ``list-commands`` without any ``reservation-*`` entry: host_cmds is not loaded.
    NO_MUTATION_COMMANDS = {"result": 0, "arguments": ["config-get", "subnet4-list"]}

    def _responses_without_mutation(self, subnet_id=20, cidr="198.18.0.0/24"):
        responses = _mutation_responses(4, subnet_id, cidr, ["hw-address"])
        responses["list-commands"] = self.NO_MUTATION_COMMANDS
        # The rejection redirects to the reservation list, which renders on follow.
        responses["reservation-get-page"] = _res_page([])
        responses["lease4-get-by-state"] = {"result": 0, "arguments": {"leases": []}}
        return responses

    def _add_payload(self):
        return {
            "subnet_cidr": "198.18.0.0/24",
            "ip_address": "198.18.0.20",
            "identifier_type": "hw-address",
            "identifier": "aa:bb:cc:dd:ee:ff",
            "hostname": "host.example.invalid",
        }

    def test_add_post_is_rejected_without_mutation_capabilities(self):
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])

        with stub_kea(self._responses_without_mutation()) as kea:
            response = self.client.post(url, self._add_payload(), follow=True)

        self.assertRedirects(response, return_url)
        self.assertNotIn("reservation-add", kea.commands())
        self.assertContains(response, "Reservation mutation capabilities are unavailable.")

    def test_add_post_is_rejected_when_the_capability_read_fails(self):
        url = reverse("plugins:netbox_kea:server_reservation4_add", args=[self.server.pk])
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        responses = self._responses_without_mutation()
        responses["list-commands"] = RuntimeError("capability read failed")

        with stub_kea(responses) as kea:
            response = self.client.post(url, self._add_payload(), follow=True)

        self.assertRedirects(response, return_url)
        self.assertNotIn("reservation-add", kea.commands())
        self.assertContains(response, "Reservation mutation capabilities are unavailable.")

    def test_edit_post_is_rejected_without_mutation_capabilities(self):
        url = reverse("plugins:netbox_kea:server_reservation4_edit", args=[self.server.pk, 20])
        return_url = reverse("plugins:netbox_kea:server_reservations4", args=[self.server.pk])
        query = _identity_query()
        responses = self._responses_without_mutation()

        with stub_kea(responses) as kea:
            response = self.client.post(
                f"{url}?{query}",
                {
                    "subnet_cidr": "198.18.0.0/24",
                    "ip_address": "198.18.0.20",
                    "identifier_type": "hw-address",
                    "identifier": "aa:bb:cc:dd:ee:ff",
                    "hostname": "new.example.invalid",
                    "managed_fingerprint": "stale",
                },
                follow=True,
            )

        self.assertRedirects(response, return_url)
        self.assertNotIn("reservation-get", kea.commands())
        self.assertNotIn("reservation-update", kea.commands())
        self.assertContains(response, "Reservation mutation capabilities are unavailable.")

    def test_delete_post_stays_rejected_without_mutation_capabilities(self):
        url = reverse("plugins:netbox_kea:server_reservation4_delete", args=[self.server.pk, 20])
        query = _identity_query()

        with stub_kea(self._responses_without_mutation()) as kea:
            response = self.client.post(f"{url}?{query}", follow=True)

        self.assertNotIn("reservation-del", kea.commands())
        self.assertContains(response, "Reservation mutation capabilities are unavailable.")

# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Guards for the Subnet Settings key table and the form-managed DHCP Options.

Each writer and the reader use one table, so a value written through a real writer reads back as the same typed
value. The writers are the Subnet add and edit views and the Shared Network edit view, with a real ``KeaClient``
and Kea stubbed at the transport. The reader is ``server_configuration``.
"""

import ast
import json
from dataclasses import fields
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from netbox_kea import server_configuration
from netbox_kea.dhcp_options import address_list, form_managed_options, form_option_fields
from netbox_kea.subnet_settings import SETTING_KEYS, SubnetSettings

from .kea_stub import SubnetDaemon, _catalogue_responses_for_subnets, stub_kea
from .test_views_shared_networks import _RunningConfiguration
from .utils import _PLUGINS_CONFIG, _page_data, _ViewTestBase

_PACKAGE = Path(__file__).resolve().parents[1]
_RECORDINGS = Path(__file__).with_name("kea_recordings")
# The settings fields with a shape of their own, which the table does not hold.
_SHAPED_FIELDS = {"relay_addresses", "client_classes", "require_client_classes"}
# Their own parse of Kea configuration (ADR 0003), and the tests.
_SCAN_EXEMPT = ("subnet_settings.py", "mappers/", "integrations/", "tests/")
_STANDARD_TABLES = {"KEA_DHCP4_STD_OPTIONS", "KEA_DHCP6_STD_OPTIONS"}

_CIDR = {4: "192.0.2.0/24", 6: "2001:db8:1::/64"}
# Each settings field that the Subnet edit form writes: the form field and a value.
_EDITED_SETTINGS = {
    "valid_lifetime": ("valid_lft", 3600),
    "min_valid_lifetime": ("min_valid_lft", 1800),
    "max_valid_lifetime": ("max_valid_lft", 7200),
    "renew_timer": ("renew_timer", 900),
    "rebind_timer": ("rebind_timer", 1800),
    "ddns_qualifying_suffix": ("ddns_qualifying_suffix", "office.example.org."),
}
_OPTIONS = {
    4: {"gateway": "192.0.2.1", "dns_servers": "192.0.2.53, 192.0.2.54", "ntp_servers": "192.0.2.123"},
    6: {"dns_servers": "2001:db8::53, 2001:db8::54", "ntp_servers": "2001:db8::123"},
}


def _settings_key_literals() -> list[str]:
    """Return each string literal that equals a settings table key, outside the modules that may hold one."""
    keys = {setting.key for setting in SETTING_KEYS.values()}
    found = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        rel = path.relative_to(_PACKAGE).as_posix()
        if rel.startswith(_SCAN_EXEMPT):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # interface-id is also a standard DHCPv6 option name.
        option_names = {
            id(node)
            for statement in tree.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id in _STANDARD_TABLES
            for node in ast.walk(statement)
        }
        found += [
            f"{rel}:{node.lineno}: {node.value}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value in keys and id(node) not in option_names
        ]
    return found


class TestSettingsTable(SimpleTestCase):
    def test_the_table_holds_each_scalar_field(self):
        self.assertEqual(set(SETTING_KEYS), {field.name for field in fields(SubnetSettings)} - _SHAPED_FIELDS)

    def test_no_settings_key_is_spelled_outside_the_table(self):
        self.assertEqual(_settings_key_literals(), [])


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestRecordedSettingsReadThroughTheTable(_ViewTestBase):
    """Each table key that a real Kea returned reads as the value that Kea returned."""

    def test_each_recorded_setting_keeps_its_value(self):
        seen = set()
        for family in (4, 6):
            config_get = json.loads((_RECORDINGS / f"dhcp{family}.json").read_text())["config-get"]
            daemon = config_get["arguments"][f"Dhcp{family}"]
            raw = [
                *daemon[f"subnet{family}"],
                *(subnet for network in daemon["shared-networks"] for subnet in network[f"subnet{family}"]),
            ]
            with stub_kea({"config-get": config_get}):
                snapshot = server_configuration.for_verification(self.server, family)
            settings = {subnet.declared_subnet_id: subnet.configuration.settings for subnet in snapshot.subnets}
            for entry in raw:
                for field, setting in SETTING_KEYS.items():
                    if setting.key in entry:
                        seen.add(field)
                        with self.subTest(family=family, subnet=entry["id"], field=field):
                            self.assertEqual(getattr(settings[entry["id"]], field), entry[setting.key])
        self.assertEqual(seen, set(SETTING_KEYS))


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestWrittenValuesReadBack(_ViewTestBase):
    """A value that a writer sends reads back through ``server_configuration`` as the same typed value."""

    def _read_back(self, family: int):
        (subnet,) = server_configuration.for_verification(self.server, family).subnets
        return subnet.configuration

    def _assert_options(self, options, family: int) -> None:
        shown = form_option_fields(options, family)
        self.assertEqual(set(shown), set(form_managed_options(family)))
        for field, text in _OPTIONS[family].items():
            self.assertEqual(address_list(shown[field]), address_list(text), field)

    def test_subnet_edit(self):
        for family in (4, 6):
            daemon = SubnetDaemon(family, [{"id": 42, "subnet": _CIDR[family], "pools": [], "option-data": []}])
            url = reverse(f"plugins:netbox_kea:server_subnet{family}_edit", args=[self.server.pk, 42])
            with self.subTest(family=family), stub_kea(daemon.responses()):
                page = _page_data(self.client.get(url))
                values = {form_field: str(value) for form_field, value in _EDITED_SETTINGS.values()}
                response = self.client.post(url, {**page, **values, **_OPTIONS[family]})
                self.assertEqual(response.status_code, 302)
                configuration = self._read_back(family)
                self.assertEqual(
                    {field: getattr(configuration.settings, field) for field in _EDITED_SETTINGS},
                    {field: value for field, (_form_field, value) in _EDITED_SETTINGS.items()},
                )
                self._assert_options(configuration.options, family)

    def test_subnet_add(self):
        for family in (4, 6):
            daemon = SubnetDaemon(family)
            data = {
                "subnet": _CIDR[family],
                "shared_networks_complete": "True",
                "ddns_qualifying_suffix": "office.example.org.",
                **_OPTIONS[family],
            }
            url = reverse(f"plugins:netbox_kea:server_subnet{family}_add", args=[self.server.pk])
            with self.subTest(family=family), stub_kea(daemon.responses()):
                self.assertEqual(self.client.post(url, data).status_code, 302)
                configuration = self._read_back(family)
                self.assertEqual(configuration.settings.ddns_qualifying_suffix, "office.example.org.")
                self._assert_options(configuration.options, family)

    def test_shared_network_edit(self):
        for family in (4, 6):
            network = {"name": "office", f"subnet{family}": []}
            daemon = _RunningConfiguration(
                _catalogue_responses_for_subnets(family, [], shared_networks=[network])["config-get"]
            )
            url = reverse(f"plugins:netbox_kea:server_shared_network{family}_edit", args=[self.server.pk, "office"])
            with self.subTest(family=family), stub_kea(daemon.responses()):
                page = _page_data(self.client.get(url))
                options = {field: text for field, text in _OPTIONS[family].items() if field != "gateway"}
                self.assertEqual(self.client.post(url, {**page, **options}).status_code, 302)
                (read,) = server_configuration.for_verification(self.server, family).shared_networks
                shown = form_option_fields(read.options, family)
                for field, text in options.items():
                    self.assertEqual(address_list(shown[field]), address_list(text), field)

# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Every Server page that reads a Snapshot shows the error headline of each kind it reads when Kea fails.

The sweep walks the URL resolver, so a new Server page fails until someone names its Snapshot kinds or exempts it.
"""

import re

import requests
from django.contrib import messages as django_messages
from django.contrib.messages import get_messages
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from netbox_kea.views.notices import HEADLINES, SnapshotKind

from .kea_stub import stub_kea
from .kea_wire_discipline import WIRE_COMMANDS
from .utils import _ViewTestBase

_NAMESPACE = "plugins:netbox_kea:"

#: Each Server GET route that shows no Snapshot Notice, with the reason.
_EXEMPT = {
    "combined_server_status_badge": "An htmx badge that reads version-get, not a Snapshot.",
    "ipaddress_kea_reservations": "Its pk names an IP address, and it reads no Kea data.",
    "reservation_check_ip": "An htmx fragment that reads NetBox IPAM, not Kea.",
    "server": "The NetBox object page reads no Kea data.",
    "server_changelog": "A NetBox page that reads no Kea data.",
    "server_delete": "A NetBox page that reads no Kea data.",
    "server_edit": "A NetBox page that reads no Kea data.",
    "server_journal": "A NetBox page that reads no Kea data.",
    "server_jobs": "The job list reads no Kea data.",
    "server_sync_status": "The Sync tab reads no Kea data.",
    "server_dhcp_plugin": "It reads config-get for the import drift without a Snapshot, and shows its own state.",
    "server_dhcp4_enable": "A confirmation page that reads no Kea data.",
    "server_dhcp4_disable": "A confirmation page that reads no Kea data.",
    "server_dhcp6_enable": "A confirmation page that reads no Kea data.",
    "server_dhcp6_disable": "A confirmation page that reads no Kea data.",
    "server_lease4_add": "A form that reads no Snapshot.",
    "server_lease6_add": "A form that reads no Snapshot.",
    "server_lease4_bulk_import": "An upload form that reads no Kea data.",
    "server_lease6_bulk_import": "An upload form that reads no Kea data.",
    "server_lease4_edit": "It reads one Lease exactly, not a Snapshot, and shows its own error.",
    "server_lease6_edit": "It reads one Lease exactly, not a Snapshot, and shows its own error.",
    "server_leases4_delete": "A GET redirects; the delete is a POST.",
    "server_leases6_delete": "A GET redirects; the delete is a POST.",
    "server_option_def4_add": "A form that reads no Kea data.",
    "server_option_def6_add": "A form that reads no Kea data.",
    "server_option_def4_delete": "A confirmation page that reads no Kea data.",
    "server_option_def6_delete": "A confirmation page that reads no Kea data.",
    "server_reservation4_bulk_import": "An upload form that reads no Kea data.",
    "server_reservation6_bulk_import": "An upload form that reads no Kea data.",
    "server_reservation4_delete": "It confirms the Subnet identity and reads one Reservation exactly; no Snapshot.",
    "server_reservation6_delete": "It confirms the Subnet identity and reads one Reservation exactly; no Snapshot.",
    "server_reservation4_published_name": "An htmx preview that shows no suffix when the Catalogue cannot.",
    "server_reservation6_published_name": "An htmx preview that shows no suffix when the Catalogue cannot.",
    "server_shared_network4_add": "A form that reads no Kea data.",
    "server_shared_network6_add": "A form that reads no Kea data.",
    "server_shared_network4_delete": "A confirmation page that reads no Kea data.",
    "server_shared_network6_delete": "A confirmation page that reads no Kea data.",
}

#: The Snapshot kinds whose error headline each page must show, by route name without its family.
_EXPECTED: dict[str, frozenset[SnapshotKind]] = {
    "server_status": frozenset({"configuration"}),
    "server_dhcp_options_edit": frozenset({"configuration"}),
    "server_option_def": frozenset({"configuration"}),
    "server_shared_networks": frozenset({"configuration"}),
    "server_shared_network_edit": frozenset({"configuration"}),
    "server_subnet_add": frozenset({"configuration"}),
    "server_subnet_delete": frozenset({"configuration"}),
    "server_subnet_wipe_leases": frozenset({"configuration"}),
    "server_subnet_edit": frozenset({"catalogue", "configuration"}),
    "server_subnets": frozenset({"catalogue"}),
    "server_subnet_options_edit": frozenset({"catalogue"}),
    "server_subnet_pool_add": frozenset({"catalogue"}),
    "server_subnet_pool_delete": frozenset({"catalogue"}),
    "server_leases": frozenset({"catalogue"}),
    "server_reservation_add": frozenset({"catalogue"}),
    "server_reservation_edit": frozenset({"catalogue"}),
    "server_reservations": frozenset({"reservation"}),
}
#: The htmx lease search shows the Catalogue Notice in its form and the Lease Notice below it.
_HTMX_EXPECTED: frozenset[SnapshotKind] = frozenset({"catalogue", "lease"})

#: The query that a page needs before it reads anything.
_QUERIES = {
    "server_reservation4_edit": {"identifier_type": "hw-address", "identifier": "aa:bb:cc:dd:ee:ff"},
    "server_reservation6_edit": {"identifier_type": "duid", "identifier": "00:01:02:03"},
}

#: The htmx search of each lease page, which reads a Lease Snapshot after the page itself.
_HTMX_SEARCHES = {
    "server_leases4": {"by": "hw", "q": "aa:bb:cc:dd:ee:ff"},
    "server_leases6": {"by": "duid", "q": "00:01:02:03:04:05"},
}

_ARGUMENTS = {
    4: {"subnet_id": 1, "pool": "198.18.0.10-198.18.0.20", "network_name": "net", "code": 222, "space": "dhcp4"},
    6: {"subnet_id": 1, "pool": "2001:db8::10-2001:db8::20", "network_name": "net", "code": 222, "space": "dhcp6"},
}
_FAMILY = re.compile(r"(?<=[a-z])([46])(?=_|$)")


def _server_routes(resolver: URLResolver | None = None, namespace: str = "", parameters: tuple = ()):
    """Yield the name and parameters of each plugin GET route whose pk names a Server."""
    for entry in (resolver or get_resolver()).url_patterns:
        found = (*parameters, *entry.pattern.regex.groupindex)
        if isinstance(entry, URLResolver):
            yield from _server_routes(entry, f"{namespace}{entry.namespace}:" if entry.namespace else namespace, found)
        elif isinstance(entry, URLPattern) and f"{namespace}{entry.name}".startswith(_NAMESPACE) and "pk" in found:
            view = getattr(entry.callback, "view_class", None) or getattr(entry.callback, "cls", None)
            if hasattr(view, "get"):
                yield f"{namespace}{entry.name}".removeprefix(_NAMESPACE), found


def _shown_error_kinds(response) -> set[SnapshotKind]:
    """Return each Snapshot kind whose headline the page shows at the error level, as a message or an inline alert."""
    shown = {str(m) for m in get_messages(response.wsgi_request) if m.level == django_messages.ERROR}
    page = response.content.decode() if response.status_code == 200 else ""
    return {
        kind
        for kind, text in HEADLINES.items()
        if text in shown
        or re.search(rf'class="alert alert-danger[^"]*"[^>]*>\s*(?:<(?:div|strong)>\s*)*{re.escape(text)}', page)
    }


class TestEveryServerPageShowsAFailedRead(_ViewTestBase):
    def test_every_server_page_shows_an_error_notice_when_kea_fails(self):
        routes = dict(_server_routes())
        self.assertFalse(set(_EXEMPT) - set(routes), "An exemption names a route that no longer exists.")
        stale = set(_EXPECTED) - {_FAMILY.sub("", name) for name in routes}
        self.assertFalse(stale, "An expected entry names a route that no longer exists.")
        failure = requests.ConnectionError("Kea is unreachable")
        checked = []
        with stub_kea(dict.fromkeys(WIRE_COMMANDS, failure)):
            for name, parameters in sorted(routes.items()):
                if name in _EXEMPT:
                    continue
                family = _FAMILY.search(name)
                arguments = {"pk": self.server.pk, **_ARGUMENTS[int(family.group(1)) if family else 4]}
                url = reverse(_NAMESPACE + name, kwargs={key: arguments[key] for key in parameters})
                expected = _EXPECTED.get(_FAMILY.sub("", name))
                self.assertIsNotNone(expected, f"Add the Snapshot kinds of {name} to _EXPECTED, or exempt it.")
                requests_ = [(url, _QUERIES.get(name, {}), {}, expected)]
                if name in _HTMX_SEARCHES:
                    requests_.append((url, _HTMX_SEARCHES[name], {"HTTP_HX_REQUEST": "true"}, _HTMX_EXPECTED))
                for path, query, headers, kinds in requests_:
                    with self.subTest(route=name, htmx=bool(headers)):
                        self._fresh_client()
                        response = self.client.get(path, query, **headers)
                        self.assertIn(response.status_code, (200, 302))
                        missing = sorted(kinds - _shown_error_kinds(response))
                        self.assertEqual(missing, [], f"{name} shows no error headline of {missing}")
                        checked.append(name)
        # The sweep must reach the pages that read each Snapshot kind.
        self.assertTrue({"server_subnets4", "server_status", "server_reservations4", "server_leases6"} <= set(checked))

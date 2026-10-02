# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The IPAM sync marker: parse, rewrite and the description filter agree on one format."""

from __future__ import annotations

import pytest
from django.test import TestCase
from ipam.models import IPAddress as NbIP
from ipam.models import IPRange, Prefix

from netbox_kea.ipam_marker import (
    DESCRIPTION_MAX_LENGTH,
    MARKER_KINDS,
    Marker,
    marked_description_q,
    parse_marker,
    render_marker,
    rewrite_marker,
    status_kind,
)

# (description, parsed marker, rewrite to "lease + reservation")
_CASES: list[tuple[str, Marker | None, str | None]] = [
    ("[kea-sync: lease]", Marker("lease", "", legacy=False), "[kea-sync: lease + reservation]"),
    (
        "[kea-sync: lease] printer on floor 2",
        Marker("lease", " printer on floor 2", legacy=False),
        "[kea-sync: lease + reservation] printer on floor 2",
    ),
    (
        "[kea-sync: lease + reservation]  two spaces ",
        Marker("lease + reservation", "  two spaces ", legacy=False),
        None,
    ),
    ("[kea-sync: delegated prefix]", Marker("delegated prefix", "", legacy=False), None),
    ("[kea-sync: lease]x", Marker("lease", "x", legacy=False), "[kea-sync: lease + reservation]x"),
    ("Synced from Kea DHCP lease", Marker("lease", "", legacy=True), "[kea-sync: lease + reservation]"),
    (
        "Synced from Kea DHCP lease my note",
        Marker("lease", " my note", legacy=True),
        "[kea-sync: lease + reservation] my note",
    ),
    (
        "Synced from Kea DHCP lease + reservation",
        Marker("lease + reservation", "", legacy=True),
        "[kea-sync: lease + reservation]",
    ),
    (
        "Synced from Kea DHCP lease + reservation rack 4",
        Marker("lease + reservation", " rack 4", legacy=True),
        "[kea-sync: lease + reservation] rack 4",
    ),
    ("Synced from Kea DHCP pool", Marker("pool", "", legacy=True), None),
    ("Synced from Kea DHCP (dhcp)", Marker(None, " (dhcp)", legacy=True), "[kea-sync: lease + reservation] (dhcp)"),
    # A legacy kind ends where a letter, a digit, "-" or "_" does not follow it.
    ("Synced from Kea DHCP lease, rack 4", Marker("lease", ", rack 4", legacy=True), None),
    ("Synced from Kea DHCP pool.", Marker("pool", ".", legacy=True), None),
    ("Synced from Kea DHCP leases", Marker(None, " leases", legacy=True), "[kea-sync: lease + reservation] leases"),
    ("Synced from Kea DHCP lease4", Marker(None, " lease4", legacy=True), None),
    ("Synced from Kea DHCP lease-v6", Marker(None, " lease-v6", legacy=True), None),
    ("Synced from Kea DHCP subnet_a", Marker(None, " subnet_a", legacy=True), None),
    ("Synced from Kea DHCP lease + reservations", Marker(None, " lease + reservations", legacy=True), None),
    ("Synced from Kea DHCPlease", Marker(None, "lease", legacy=True), None),
    ("", None, None),
    ("Printer on floor 2", None, None),
    ("[kea-sync: lease ]", None, None),
    ("[kea-sync:lease]", None, None),
    ("[kea-sync: leases]", None, None),
    ("[Kea-sync: lease]", None, None),
    ("rack 4 [kea-sync: lease]", None, None),
    (" [kea-sync: lease]", None, None),
    ("synced from Kea DHCP lease", None, None),
]


@pytest.mark.parametrize(("description", "marker", "rewritten"), _CASES)
def test_parse_and_rewrite(description: str, marker: Marker | None, rewritten: str | None):
    assert parse_marker(description) == marker
    if rewritten is not None:
        assert marker is not None
        assert rewrite_marker(marker, "lease + reservation") == rewritten
        assert parse_marker(rewritten) == Marker("lease + reservation", marker.rest, legacy=False)


@pytest.mark.parametrize("kind", MARKER_KINDS)
def test_a_rendered_marker_parses_back_to_its_kind(kind):
    assert parse_marker(render_marker(kind)) == Marker(kind, "", legacy=False)


def test_each_status_that_the_sync_writes_has_a_kind():
    assert {status: status_kind(status) for status in ("dhcp", "reserved", "active")} == {
        "dhcp": "lease",
        "reserved": "reservation",
        "active": "lease + reservation",
    }
    with pytest.raises(KeyError):
        status_kind("deprecated")


def test_a_rewrite_that_does_not_fit_returns_none():
    block = render_marker("lease + reservation")
    fitting = Marker("lease", "n" * (DESCRIPTION_MAX_LENGTH - len(block)), legacy=False)
    assert rewrite_marker(fitting, "lease + reservation") == block + fitting.rest
    assert rewrite_marker(Marker("lease", fitting.rest + "n", legacy=False), "lease + reservation") is None


def test_the_length_limit_is_the_netbox_description_limit():
    for model in (NbIP, Prefix, IPRange):
        assert model._meta.get_field("description").max_length == DESCRIPTION_MAX_LENGTH, model


class MarkedDescriptionFilterTest(TestCase):
    """The database filter matches exactly the descriptions that parse_marker accepts."""

    def test_the_filter_matches_the_parsed_descriptions(self):
        for number, (description, _marker, _rewritten) in enumerate(_CASES, start=1):
            NbIP.objects.create(address=f"10.0.0.{number}/24", description=description)

        matched = set(NbIP.objects.filter(marked_description_q()).values_list("description", flat=True))

        self.assertEqual(matched, {description for description, marker, _ in _CASES if marker is not None})

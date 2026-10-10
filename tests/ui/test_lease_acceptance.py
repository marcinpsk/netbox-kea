# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Lease acceptance against the real Kea 3.2.0 daemons: delegated prefixes, permissions, stale edits, exports."""

import csv
import re
from typing import Any

import pynetbox
import pytest
from playwright.sync_api import Download, Page, expect

from ..conftest import REQUEST_TIMEOUT
from .conftest import _DualEndpointKeaClient
from .test_ui import _reservation_del, _reservation_get, _version_ge_43, configure_table, search_lease

PD_ADDRESS = "2001:db8:8::"
PD_PREFIX = f"{PD_ADDRESS}/64"
PD_DUID = "01:02:03:04:05:06:07:a1"
PD_SUBNET_ID = 1
ADDRESS6 = "2001:db8:1::1"
ADDRESS4 = "192.0.2.1"
HW_ADDRESS4 = "08:08:08:08:08:08"
# Kea 3.2.0 accepts this expiration, and reads back a transaction time past the last datetime timestamp.
UNREPRESENTABLE_EXPIRE = 300_000_000_000
MALFORMED_ADDRESS4 = "192.0.2.77"
RELEASED_ADDRESS4 = "192.0.2.2"
ADDED_ADDRESS4 = "192.0.2.30"
IN_USE_ADDRESS4 = "192.0.2.31"

# An address Sync writes the IP address and the MAC address; no grant covers a Prefix.
_SYNC_USER_PERMISSIONS = [
    {"actions": ["view"], "object_types": ["netbox_kea.server"]},
    {"actions": ["view", "add", "change"], "object_types": ["ipam.ipaddress", "dcim.macaddress"]},
]


def _pd_get(kea: _DualEndpointKeaClient, check=(0,)) -> dict[str, Any] | None:
    reply = kea.command("lease6-get", 6, arguments={"ip-address": PD_ADDRESS, "type": "IA_PD"}, check=check)[0]
    return reply.get("arguments") if reply["result"] == 0 else None


def _lease_row(page: Page, label: str):
    return page.locator("table.object-list > tbody > tr").filter(
        has=page.locator("td", has_text=re.compile(f"^{re.escape(label)}$"))
    )


def _delete_prefixes(nb_api: pynetbox.api, prefix: str) -> None:
    for stale in nb_api.ipam.prefixes.filter(prefix=prefix):
        stale.delete()


def _delete_ip_addresses(nb_api: pynetbox.api, address: str) -> None:
    for stale in nb_api.ipam.ip_addresses.filter(address=address):
        stale.delete()


def _post_with_csrf(page: Page, url: str, form: dict[str, str]):
    csrf_token = page.evaluate("window.CSRF_TOKEN")
    assert isinstance(csrf_token, str) and csrf_token
    return page.context.request.post(
        url,
        form=form,
        headers={"X-CSRFToken": csrf_token, "Referer": page.url},
        timeout=REQUEST_TIMEOUT * 1000,
    )


@pytest.fixture
def pd_lease(kea: _DualEndpointKeaClient, clear_leases: None) -> dict[str, Any]:
    kea.command(
        "lease6-add",
        6,
        arguments={
            "ip-address": PD_ADDRESS,
            "type": "IA_PD",
            "prefix-len": 64,
            "subnet-id": PD_SUBNET_ID,
            "duid": PD_DUID,
            "iaid": 7,
            "valid-lft": 3600,
            "preferred-lft": 1800,
            "hostname": "pd-host",
        },
    )
    lease = _pd_get(kea)
    assert lease is not None
    return lease


@pytest.fixture
def address6_lease(kea: _DualEndpointKeaClient, clear_leases: None, nb_api: pynetbox.api) -> dict[str, Any]:
    _delete_ip_addresses(nb_api, ADDRESS6)
    kea.command(
        "lease6-add",
        6,
        arguments={
            "ip-address": ADDRESS6,
            "duid": "01:02:03:04:05:06:07:08",
            "iaid": 1,
            "valid-lft": 3600,
            "preferred-lft": 1800,
            "hostname": "address6-host",
        },
    )
    return kea.command("lease6-get", 6, arguments={"ip-address": ADDRESS6})[0]["arguments"]


def test_a_delegated_prefix_lease_shows_its_prefix_and_kind(page: Page, pd_lease: dict[str, Any]) -> None:
    def search() -> None:
        search_lease(page, 6, "IP Address", PD_ADDRESS)

    search()
    configure_table(page, "ip_address", "kind", "prefix_length", "hostname")
    if _version_ge_43(page):
        search()

    expect(page.locator("table.object-list > tbody > tr > td")).to_have_text(
        [re.compile(".*"), PD_PREFIX, "delegated-prefix", "64", "pd-host", re.compile(".*")]
    )


def test_a_delegated_prefix_edit_changes_its_hostname_in_kea(
    page: Page, kea: _DualEndpointKeaClient, pd_lease: dict[str, Any]
) -> None:
    search_lease(page, 6, "IP Address", PD_ADDRESS)
    row = _lease_row(page, PD_PREFIX)
    expect(row).to_have_count(1)
    row.locator("a.dropdown-toggle").click()
    row.get_by_role("link", name="Edit lease").click()

    expect(page.get_by_text("Kind: Delegated prefix.")).to_be_visible()
    expect(page.locator("#id_hostname")).to_have_value("pd-host")
    page.locator("#id_hostname").fill("pd-renamed")
    page.get_by_role("button", name="Save").click()
    expect(page.locator(".toast-body")).to_have_text(f"Lease {PD_PREFIX} updated.")

    stored = _pd_get(kea)
    assert stored is not None
    assert stored["hostname"] == "pd-renamed"
    assert stored["type"] == "IA_PD"
    assert stored["prefix-len"] == 64
    assert stored["duid"] == PD_DUID


def test_a_delegated_prefix_delete_removes_it_from_kea(
    page: Page, kea: _DualEndpointKeaClient, pd_lease: dict[str, Any]
) -> None:
    search_lease(page, 6, "IP Address", PD_ADDRESS)
    row = _lease_row(page, PD_PREFIX)
    expect(row).to_have_count(1)
    row.locator('input[name="pk"]').check()
    page.get_by_role("button", name="Delete Selected").click()
    # The confirmation names the kind of the selected allocation.
    expect(page.locator("#object-list td", has_text=re.compile("^Delegated prefix$"))).to_have_count(1)
    page.locator('button[name="_confirm"]').click()
    expect(page.locator(".toast-body")).to_have_text(re.compile(r"Deleted 1 DHCPv6 lease\(s\)"))

    assert _pd_get(kea, check=(3,)) is None


def test_a_delegated_prefix_reserve_creates_a_prefix_reservation(
    page: Page, kea: _DualEndpointKeaClient, pd_lease: dict[str, Any]
) -> None:
    _reservation_del(kea, 6, PD_SUBNET_ID, "duid", PD_DUID)
    try:
        search_lease(page, 6, "IP Address", PD_ADDRESS)
        row = _lease_row(page, PD_PREFIX)
        expect(row).to_have_count(1)
        row.get_by_role("link", name="+ Reserve").click()

        expect(page.locator("#id_prefixes")).to_have_value(PD_PREFIX)
        expect(page.locator("#id_ip_addresses")).to_have_value("")
        expect(page.locator("#id_identifier")).to_have_value(PD_DUID)
        page.get_by_role("button", name="Save").click()
        # The harness mounts the Kea configuration read-only, so config-write adds a persistence warning.
        expect(page.locator(".toast-body", has_text="Reservation created.")).to_have_count(1)

        stored = _reservation_get(kea, 6, PD_SUBNET_ID, "duid", PD_DUID)
        assert stored is not None
        assert stored["prefixes"] == [PD_PREFIX]
        assert not stored.get("ip-addresses")
        # The redirect returns to the search, and the row is now reserved.
        expect(_lease_row(page, PD_PREFIX).get_by_text("Reserved", exact=True)).to_be_visible()
    finally:
        _reservation_del(kea, 6, PD_SUBNET_ID, "duid", PD_DUID)


def test_a_delegated_prefix_sync_creates_a_netbox_prefix(
    page: Page, nb_api: pynetbox.api, pd_lease: dict[str, Any]
) -> None:
    _delete_prefixes(nb_api, PD_PREFIX)
    try:
        search_lease(page, 6, "IP Address", PD_ADDRESS)
        row = _lease_row(page, PD_PREFIX)
        expect(row).to_have_count(1)
        row.get_by_role("button", name="Sync", exact=True).click()
        expect(row.get_by_role("link", name=PD_PREFIX)).to_be_visible()

        prefixes = list(nb_api.ipam.prefixes.filter(prefix=PD_PREFIX))
        assert len(prefixes) == 1
        assert prefixes[0].vrf is None

        search_lease(page, 6, "IP Address", PD_ADDRESS)
        row = _lease_row(page, PD_PREFIX)
        expect(row.get_by_role("link", name="Synced")).to_be_visible()
        expect(row.get_by_role("button", name="Sync", exact=True)).to_have_count(0)
    finally:
        _delete_prefixes(nb_api, PD_PREFIX)


_PREFIX_SYNC_REFUSAL = "Manual Sync needs unconstrained ipam.add_prefix, ipam.change_prefix permissions"


@pytest.mark.parametrize(
    ("netbox_username", "netbox_password", "netbox_user_permissions"),
    [("lease-sync-user", "lease-sync-user12Characters", _SYNC_USER_PERMISSIONS)],
)
def test_sync_needs_the_permissions_of_the_netbox_model_it_writes(
    page: Page,
    nb_api: pynetbox.api,
    plugin_base: str,
    with_test_server,
    pd_lease: dict[str, Any],
    address6_lease: dict[str, Any],
) -> None:
    _delete_prefixes(nb_api, PD_PREFIX)
    try:
        search_lease(page, 6, "Subnet ID", str(PD_SUBNET_ID))
        address_row = _lease_row(page, ADDRESS6)
        prefix_row = _lease_row(page, PD_PREFIX)
        expect(address_row).to_have_count(1)
        expect(prefix_row).to_have_count(1)
        expect(address_row.get_by_role("button", name="Sync", exact=True, disabled=False)).to_have_count(1)
        expect(prefix_row.get_by_role("button", name="Sync", exact=True, disabled=False)).to_have_count(0)
        expect(prefix_row.get_by_role("button", name="Sync", exact=True, disabled=True)).to_have_count(1)
        expect(prefix_row.get_by_title(_PREFIX_SYNC_REFUSAL, exact=False)).to_have_count(1)

        response = _post_with_csrf(
            page, f"{plugin_base}/servers/{with_test_server.id}/leases6/sync/", {"ip_address": PD_PREFIX}
        )
        assert response.ok
        assert _PREFIX_SYNC_REFUSAL in response.text()
        assert not list(nb_api.ipam.prefixes.filter(prefix=PD_PREFIX))
    finally:
        _delete_prefixes(nb_api, PD_PREFIX)


@pytest.mark.parametrize(
    ("netbox_username", "netbox_password", "netbox_user_permissions", "actions"),
    [
        (
            "lease-view-user",
            "lease-view-user12Characters",
            [{"actions": ["view"], "object_types": ["netbox_kea.server"]}],
            0,
        ),
        # The control: the same assertions find every action for the admin.
        ("admin", "admin", [], 2),
    ],
    ids=("view-only", "admin"),
)
def test_lease_row_actions_follow_the_user_permissions(
    page: Page, nb_api: pynetbox.api, pd_lease: dict[str, Any], address6_lease: dict[str, Any], actions: int
) -> None:
    _delete_prefixes(nb_api, PD_PREFIX)
    search_lease(page, 6, "Subnet ID", str(PD_SUBNET_ID))
    rows = page.locator("table.object-list > tbody > tr")
    expect(rows).to_have_count(2)

    expect(page.locator('input[name="pk"]')).to_have_count(actions)
    expect(page.get_by_role("button", name="Delete Selected")).to_have_count(min(actions, 1))
    expect(rows.get_by_role("button", name="Sync", exact=True, disabled=False)).to_have_count(actions)
    # A refused user sees the Sync control disabled.
    expect(rows.get_by_role("button", name="Sync", exact=True, disabled=True)).to_have_count(2 - actions)
    # A closed dropdown hides its items from get_by_role, so count the DOM elements.
    expect(rows.locator("a.dropdown-item", has_text="Edit lease")).to_have_count(actions)
    expect(rows.locator("a.badge", has_text="+ Reserve")).to_have_count(actions)


@pytest.fixture
def lease4_with_extension(kea: _DualEndpointKeaClient, clear_leases: None) -> dict[str, Any]:
    kea.command(
        "lease4-add",
        4,
        arguments={
            "ip-address": ADDRESS4,
            "hw-address": HW_ADDRESS4,
            "hostname": "stale-edit",
            "valid-lft": 3600,
            "user-context": {"owner": {"team": "netops"}},
        },
    )
    return kea.command("lease4-get", 4, arguments={"ip-address": ADDRESS4})[0]["arguments"]


def _replace_lease4(kea: _DualEndpointKeaClient, lease: dict[str, Any], **changes: Any) -> None:
    """Write the lease again from outside the plugin, as another Kea client would."""
    body = {
        "ip-address": lease["ip-address"],
        "hw-address": lease["hw-address"],
        "hostname": lease["hostname"],
        "valid-lft": lease["valid-lft"],
        "expire": lease["cltt"] + lease["valid-lft"],
        "user-context": lease["user-context"],
    }
    kea.command("lease4-update", 4, arguments={**body, **changes})


def _open_lease4_edit(page: Page, plugin_base: str, server_id: int) -> None:
    page.goto(f"{plugin_base}/servers/{server_id}/leases4/{ADDRESS4}/edit/")
    expect(page.locator("#id_hostname")).to_have_value("stale-edit")


def test_an_edit_refuses_a_lease_whose_binding_changed_in_kea(
    page: Page,
    kea: _DualEndpointKeaClient,
    plugin_base: str,
    with_test_server,
    lease4_with_extension: dict[str, Any],
) -> None:
    _open_lease4_edit(page, plugin_base, with_test_server.id)
    _replace_lease4(kea, lease4_with_extension, **{"hw-address": "08:08:08:08:08:09"})

    page.locator("#id_hostname").fill("must-not-apply")
    page.get_by_role("button", name="Save").click()

    expect(page.locator(".toast-body")).to_have_text(
        f"Lease {ADDRESS4} was not changed: its client binding changed in Kea after the form was shown."
        " Reload the form and try again."
    )
    stored = kea.command("lease4-get", 4, arguments={"ip-address": ADDRESS4})[0]["arguments"]
    assert stored["hostname"] == "stale-edit"
    assert stored["hw-address"] == "08:08:08:08:08:09"


def test_an_edit_after_a_renewal_keeps_the_renewal_and_the_extension_values(
    page: Page,
    kea: _DualEndpointKeaClient,
    plugin_base: str,
    with_test_server,
    lease4_with_extension: dict[str, Any],
) -> None:
    _open_lease4_edit(page, plugin_base, with_test_server.id)
    renewed_cltt = lease4_with_extension["cltt"] + 600
    renewed_context = {"owner": {"team": "netops", "renewed-by": "external"}}
    _replace_lease4(
        kea,
        lease4_with_extension,
        expire=renewed_cltt + lease4_with_extension["valid-lft"],
        **{"user-context": renewed_context},
    )

    page.locator("#id_hostname").fill("renewed-edit")
    page.get_by_role("button", name="Save").click()

    expect(page.locator(".toast-body")).to_have_text(f"Lease {ADDRESS4} updated.")
    stored = kea.command("lease4-get", 4, arguments={"ip-address": ADDRESS4})[0]["arguments"]
    assert stored["hostname"] == "renewed-edit"
    assert stored["cltt"] == renewed_cltt
    assert stored["user-context"] == renewed_context
    assert stored["hw-address"] == HW_ADDRESS4


@pytest.fixture
def leases4_with_unreadable(kea: _DualEndpointKeaClient, clear_leases: None) -> None:
    kea.command("lease4-add", 4, arguments={"ip-address": ADDRESS4, "hw-address": HW_ADDRESS4, "hostname": "good"})
    kea.command(
        "lease4-add",
        4,
        arguments={
            "ip-address": MALFORMED_ADDRESS4,
            "hw-address": "08:00:00:00:00:77",
            "valid-lft": 3600,
            "expire": UNREPRESENTABLE_EXPIRE,
        },
    )


def test_a_complete_export_refuses_an_observation_with_an_unreadable_lease(
    page: Page, leases4_with_unreadable: None
) -> None:
    downloads: list[Download] = []

    # Playwright cannot register a bound builtin such as list.append as a handler.
    def record(download: Download) -> None:
        downloads.append(download)

    page.on("download", record)

    def search() -> None:
        search_lease(page, 4, "Subnet ID", "1")
        expect(page.locator("table.object-list > tbody > tr")).to_have_count(1)
        expect(_lease_row(page, ADDRESS4)).to_have_count(1)
        warning = page.locator(".alert-warning", has_text="Kea returned 1 lease record that could not be read.")
        expect(warning).to_contain_text("A lease field is outside the range that Kea permits.")

    search()
    page.get_by_role("button", name="Export").click()
    with page.expect_download() as download:
        page.get_by_role("link", name="Current View (limited coverage)").click()
    assert download.value.suggested_filename == "leases_limited_coverage.csv"
    with open(download.value.path()) as f:
        assert [row["IP Address"] for row in csv.DictReader(f)] == [ADDRESS4]

    for link in ("All Data (CSV)", "Export All Leases (CSV)"):
        search()
        page.get_by_role("button", name="Export").click()
        page.get_by_role("link", name=link).click()
        expect(page.locator(".toast-body")).to_contain_text(
            "Export refused: Kea returned 1 lease record(s) that could not be read"
            " (A lease field is outside the range that Kea permits.)."
        )
    assert len(downloads) == 1


def test_a_delete_refuses_a_lease_whose_binding_changed_in_kea(
    page: Page, kea: _DualEndpointKeaClient, lease4_with_extension: dict[str, Any]
) -> None:
    search_lease(page, 4, "IP Address", ADDRESS4)
    row = _lease_row(page, ADDRESS4)
    expect(row).to_have_count(1)
    row.locator('input[name="pk"]').check()
    page.get_by_role("button", name="Delete Selected").click()
    confirm = page.locator('button[name="_confirm"]')
    expect(confirm).to_be_visible()
    _replace_lease4(kea, lease4_with_extension, **{"hw-address": "08:08:08:08:08:09"})

    confirm.click()

    expect(page.locator(".toast-body", has_text=f"Lease {ADDRESS4} was not deleted")).to_have_text(
        f"Lease {ADDRESS4} was not deleted: its client binding changed in Kea after the list was shown."
        " Reload the list and try again."
    )
    stored = kea.command("lease4-get", 4, arguments={"ip-address": ADDRESS4})[0]["arguments"]
    assert stored["hw-address"] == "08:08:08:08:08:09"


@pytest.fixture
def assigned_and_released_leases4(kea: _DualEndpointKeaClient, clear_leases: None, nb_api: pynetbox.api) -> None:
    for address in (ADDRESS4, RELEASED_ADDRESS4):
        _delete_ip_addresses(nb_api, address)
    kea.command("lease4-add", 4, arguments={"ip-address": ADDRESS4, "hw-address": HW_ADDRESS4})
    kea.command(
        "lease4-add", 4, arguments={"ip-address": RELEASED_ADDRESS4, "hw-address": "08:00:00:00:00:02", "state": 3}
    )


def test_a_lease_that_is_not_current_is_never_synced(
    page: Page,
    nb_api: pynetbox.api,
    plugin_base: str,
    with_test_server,
    assigned_and_released_leases4: None,
) -> None:
    # The harness keeps the automatic sync off, so only the manual Sync could claim it.
    search_lease(page, 4, "Subnet ID", "1")
    assigned, released = _lease_row(page, ADDRESS4), _lease_row(page, RELEASED_ADDRESS4)
    expect(released.get_by_text("Released", exact=True)).to_be_visible()
    expect(assigned.get_by_role("button", name="Sync", exact=True)).to_have_count(1)
    expect(released.get_by_role("button", name="Sync", exact=True)).to_have_count(0)

    response = _post_with_csrf(
        page, f"{plugin_base}/servers/{with_test_server.id}/leases4/sync/", {"ip_address": RELEASED_ADDRESS4}
    )

    assert response.ok
    assert "The lease is not current in Kea, so it was not synchronized." in response.text()
    assert not list(nb_api.ipam.ip_addresses.filter(address=RELEASED_ADDRESS4))


def test_a_lease_added_in_the_form_is_created_in_kea(
    page: Page, kea: _DualEndpointKeaClient, plugin_base: str, with_test_server, clear_leases: None
) -> None:
    page.goto(f"{plugin_base}/servers/{with_test_server.id}/leases4/add/")
    page.locator("#id_ip_address").fill(ADDED_ADDRESS4)
    page.locator("#id_hw_address").fill("08:00:00:00:00:30")
    page.locator("#id_hostname").fill("form-added")
    page.get_by_role("button", name="Save").click()

    expect(page.locator(".toast-body", has_text="created")).to_have_text(f"Lease for {ADDED_ADDRESS4} created.")
    stored = kea.command("lease4-get", 4, arguments={"ip-address": ADDED_ADDRESS4})[0]["arguments"]
    assert (stored["hw-address"], stored["hostname"]) == ("08:00:00:00:00:30", "form-added")


def test_a_csv_import_creates_each_new_lease_and_reports_the_refused_one(
    page: Page, kea: _DualEndpointKeaClient, plugin_base: str, with_test_server, clear_leases: None
) -> None:
    kea.command("lease4-add", 4, arguments={"ip-address": IN_USE_ADDRESS4, "hw-address": "08:00:00:00:00:31"})
    content = (
        "ip-address,hw-address,hostname\n"
        f"{ADDED_ADDRESS4},08:00:00:00:00:30,csv-added\n"
        f"{IN_USE_ADDRESS4},08:00:00:00:00:99,csv-refused\n"
    )
    page.goto(f"{plugin_base}/servers/{with_test_server.id}/leases4/import/")
    page.locator("#id_csv_file").set_input_files(
        files=[{"name": "leases.csv", "mimeType": "text/csv", "buffer": content.encode()}]
    )
    page.get_by_role("button", name="Import").click()

    expect(page.locator(".display-6.text-success")).to_have_text("1")
    expect(page.locator(".display-6.text-danger")).to_have_text("1")
    expect(page.locator("tr.table-danger > td").first).to_have_text("3")
    expect(page.locator("tr.table-danger code")).to_have_text(IN_USE_ADDRESS4)
    added = kea.command("lease4-get", 4, arguments={"ip-address": ADDED_ADDRESS4})[0]["arguments"]
    assert added["hostname"] == "csv-added"
    kept = kea.command("lease4-get", 4, arguments={"ip-address": IN_USE_ADDRESS4})[0]["arguments"]
    assert kept["hw-address"] == "08:00:00:00:00:31"

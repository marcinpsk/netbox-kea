# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The reservation "Sync all" button with a netbox-branching branch selected, in a real browser.

It runs against the netbox-branching variant of the harness (docker-compose.branching.yml: NetBox
4.7, netbox-branching 1.2.1, DEBUG off). Without netbox-branching the module skips. The branching
CI job sets NETBOX_KEA_REQUIRE_BRANCHING=1, and then the module fails instead.

Kea is live here, so the proof that a refused click changed nothing is by state: the configuration
hash, reservations and leases of both Kea daemons, and the NetBox IP addresses, are the same before
the page loads and after the click. The merge helper's Tag and a Server delete are not in that state. That the refused request sends no Kea command at all, reads included, is proved in
netbox_kea/tests/test_branching.py (guard 1 and the selector table), which counts the commands.
"""

import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import pynetbox
import pytest
import requests
from playwright.sync_api import Locator, Page, expect

from ..conftest import REQUEST_TIMEOUT
from .test_workflows import _RESERVED_IP4, _RESERVED_MAC4, reserved_lease4  # noqa: F401

if TYPE_CHECKING:
    from .conftest import _DualEndpointKeaClient

_REQUIRE_BRANCHING = "NETBOX_KEA_REQUIRE_BRANCHING"
_BRANCH_WAIT_SECONDS = 300
_REFUSED = (
    "is active. Kea servers, sync settings and IPAM ownership come from main. "
    "DHCP Import Mappings follow their targets in a supported fresh branch. "
    "Live Kea and import changes require main. Switch to main to make this change."
)
_UNUSABLE = "The selected branch is not usable"
# NetBox renders the navbar twice (desktop and mobile), and only one copy is visible.
_BANNER_SELECTOR = ".kea-branch-banner"

#: What a user needs to open a Server's reservations and to see the Sync all button.
_SYNC_USER_PERMISSIONS = [
    {"actions": ["view"], "object_types": ["netbox_kea.server"]},
    {"actions": ["view", "add", "change"], "object_types": ["ipam.ipaddress"]},
]


class _Branches:
    """Create, merge and delete branches through netbox-branching's REST API, and wait for its jobs."""

    def __init__(self, http: requests.Session, netbox_url: str) -> None:
        self._http = http
        self._url = f"{netbox_url}/api/plugins/branching/branches/"
        self._tags = f"{netbox_url}/api/extras/tags/"
        self.created: list[int] = []
        self.merged_tags: list[int] = []

    def create(self) -> dict[str, Any]:
        response = self._http.post(self._url, json={"name": f"kea-ui-{uuid.uuid4().hex[:8]}"}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        branch = response.json()
        self.created.append(branch["id"])
        return self._wait(branch, "ready")

    def merge(self, branch: dict[str, Any]) -> None:
        # netbox-branching does not merge a branch without changes, so the branch gets one first.
        name = f"kea-ui-{uuid.uuid4().hex[:8]}"
        tag = self._http.post(
            self._tags,
            json={"name": name, "slug": name},
            headers={"X-NetBox-Branch": branch["schema_id"]},
            timeout=REQUEST_TIMEOUT,
        )
        tag.raise_for_status()
        self.merged_tags.append(tag.json()["id"])
        merge = self._http.post(f"{self._url}{branch['id']}/merge/", json={"commit": True}, timeout=REQUEST_TIMEOUT)
        merge.raise_for_status()
        self._wait(branch, "merged")

    def delete(self, branch: dict[str, Any]) -> None:
        self._http.delete(f"{self._url}{branch['id']}/", timeout=REQUEST_TIMEOUT).raise_for_status()

    def delete_created(self) -> None:
        for branch_id in self.created:
            if self._http.get(f"{self._url}{branch_id}/", timeout=REQUEST_TIMEOUT).status_code == 200:
                self._http.delete(f"{self._url}{branch_id}/", timeout=REQUEST_TIMEOUT).raise_for_status()
        for tag_id in self.merged_tags:
            if self._http.get(f"{self._tags}{tag_id}/", timeout=REQUEST_TIMEOUT).status_code == 200:
                self._http.delete(f"{self._tags}{tag_id}/", timeout=REQUEST_TIMEOUT).raise_for_status()

    def _wait(self, branch: dict[str, Any], status: str) -> dict[str, Any]:
        deadline = time.monotonic() + _BRANCH_WAIT_SECONDS
        while True:
            response = self._http.get(f"{self._url}{branch['id']}/", timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            current = response.json()
            if current["status"]["value"] == status:
                return current
            if time.monotonic() > deadline:
                raise AssertionError(f"branch {branch['name']} is {current['status']['value']}, not {status}")
            time.sleep(1)


@pytest.fixture(scope="module")
def branching_installed(netbox_url: str, nb_http: requests.Session) -> None:
    """Skip when the harness NetBox has no netbox-branching, or fail when NETBOX_KEA_REQUIRE_BRANCHING=1."""
    required = (os.environ.get(_REQUIRE_BRANCHING) or "").strip()
    if required not in ("", "1"):
        raise ValueError(f"{_REQUIRE_BRANCHING} must be 1 or unset, not {required!r}")
    url = f"{netbox_url}/api/plugins/branching/branches/"
    response = nb_http.get(url, timeout=REQUEST_TIMEOUT)
    if response.status_code == 404:
        if required:
            pytest.fail(f"{_REQUIRE_BRANCHING}=1, but the harness NetBox has no netbox-branching")
        pytest.skip("the harness NetBox has no netbox-branching: use docker-compose.branching.yml")
    response.raise_for_status()


@pytest.fixture
def branches(branching_installed: None, nb_http: requests.Session, netbox_url: str) -> Iterator[_Branches]:
    created = _Branches(nb_http, netbox_url)
    try:
        yield created
    finally:
        created.delete_created()


def _reservations(kea: "_DualEndpointKeaClient", family: int) -> list[dict[str, Any]]:
    """Return the reservations of every subnet and every host source, as Kea's page cursor walks them."""
    hosts: list[dict[str, Any]] = []
    cursor = {"source-index": 0, "from": 0}
    for _page in range(100):
        reply = kea.command("reservation-get-page", family, arguments={**cursor, "limit": 1000}, check=(0, 3))[0]
        if reply["result"] == 3:
            return hosts
        hosts.extend(reply["arguments"]["hosts"])
        cursor = reply["arguments"]["next"]
        if cursor == {"source-index": 0, "from": 0}:
            return hosts
    raise AssertionError(f"reservation-get-page of DHCPv{family} did not end after 100 pages")


def _state(
    kea: "_DualEndpointKeaClient", http: requests.Session, netbox_url: str, branch: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return what the Sync all button can change: live Kea, and NetBox IP addresses in main and in *branch*."""
    state: dict[str, Any] = {}
    for family in (4, 6):
        state[f"dhcp{family} hash"] = kea.command("config-get", family)[0]["arguments"]["hash"]
        hosts = _reservations(kea, family)
        leases = (kea.command(f"lease{family}-get-all", family, check=(0, 3))[0].get("arguments") or {}).get(
            "leases", []
        )
        state[f"dhcp{family} reservations"] = sorted(json.dumps(host, sort_keys=True) for host in hosts)
        state[f"dhcp{family} leases"] = sorted(json.dumps(lease, sort_keys=True) for lease in leases)
    schemas = {"main": {}} if branch is None else {"main": {}, "branch": {"X-NetBox-Branch": branch["schema_id"]}}
    for schema, headers in schemas.items():
        response = http.get(f"{netbox_url}/api/ipam/ip-addresses/?limit=0", headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        state[f"NetBox IP addresses in {schema}"] = sorted(
            (ip["id"], ip["address"], ip["status"]["value"]) for ip in response.json()["results"]
        )
    return state


def _activate(page: Page, plugin_base: str, branch: dict[str, Any]) -> None:
    """Select *branch* the way the branch selector does; netbox-branching then keeps it in a cookie."""
    page.goto(f"{plugin_base}/servers/?_branch={branch['schema_id']}")
    expect(page.locator(f"{_BANNER_SELECTOR}:visible")).to_be_visible()


def _activate_in_other_tab(page: Page, plugin_base: str, branch: dict[str, Any]) -> None:
    """Keep an enabled main page open while another tab selects a branch in the shared cookie."""
    other = page.context.new_page()
    try:
        _activate(other, plugin_base, branch)
    finally:
        other.close()


def _reservation_row(page: Page, plugin_base: str, server_id: int, query: str = "") -> Locator:
    """Open the Server's DHCPv4 reservations and return the row of the reservation that kea-dhcp4.conf holds."""
    page.goto(f"{plugin_base}/servers/{server_id}/reservations4/{query}")
    page.wait_for_load_state("networkidle")
    row = (
        page.locator("tr")
        .filter(has=page.get_by_text(_RESERVED_MAC4, exact=True))
        .filter(has=page.get_by_text(_RESERVED_IP4, exact=True))
    )
    expect(row).to_have_count(1)
    expect(row.get_by_role("button", name="Sync all")).to_be_visible()
    return row


def _click_sync_all(page: Page, row: Locator) -> int:
    """Click Sync all and return the status of its POST."""
    with page.expect_response(lambda response: "/sync/" in response.url and response.request.method == "POST") as post:
        row.get_by_role("button", name="Sync all").click()
    return post.value.status


def _toast(page: Page, text: str) -> Locator:
    return page.locator("#django-messages .toast", has_text=text)


@pytest.fixture
def no_synchronized_address(nb_http: requests.Session, netbox_url: str) -> None:
    """The Sync all button shows only while NetBox has no IP address for the reservation."""
    response = nb_http.get(f"{netbox_url}/api/ipam/ip-addresses/?address={_RESERVED_IP4}", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    assert response.json()["count"] == 0, f"NetBox holds {_RESERVED_IP4}; delete it, so Sync all shows again"


pytestmark = pytest.mark.usefixtures("branching_installed", "no_synchronized_address")


def _control(page: Page, label: str) -> Locator:
    words = r"\s+".join(re.escape(word) for word in label.split())
    control = (
        page.locator("a, button")
        .filter(has_text=re.compile(rf"^\s*{words}\s*$"))
        .or_(page.get_by_label(label, exact=True))
    )
    expect(control).to_have_count(1)
    return control


def _assert_disabled_explanation(page: Page, control: Locator) -> None:
    expect(control).to_have_attribute("aria-disabled", "true")
    expect(control).not_to_have_attribute("href", re.compile(r".+"))
    wrapper = control.locator("..")
    description = wrapper.get_attribute("aria-describedby")
    assert description, "The disabled control must name its explanation."
    tooltip = page.locator(f"#{description}")
    expect(tooltip).to_have_count(1)
    expect(tooltip).not_to_be_visible()
    wrapper.hover()
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text("Switch to main")
    assert tooltip.evaluate(
        """element => {
            const box = element.getBoundingClientRect();
            const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
            return box.x >= 0 && box.y >= 0 && box.right <= innerWidth && box.bottom <= innerHeight
                && (hit === element || element.contains(hit));
        }"""
    ), "The visible explanation must not be clipped by a table or dropdown."
    page.mouse.move(0, 0)
    wrapper.evaluate("element => element.blur()")
    expect(tooltip).not_to_be_visible()
    wrapper.focus()
    wrapper.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(wrapper).to_be_focused()
    expect(tooltip).to_be_visible()
    wrapper.press("Enter")
    control.evaluate("element => element.click()")
    page.wait_for_load_state("networkidle")


@pytest.mark.usefixtures("reserved_lease4")
@pytest.mark.parametrize(
    ("suffix", "label"),
    [
        ("", "Edit"),
        ("subnets4/", "Add Subnet"),
        ("shared_networks4/", "Edit test-shared-network-4"),
        (f"leases4/?by=ip&q={_RESERVED_IP4}", "Delete Selected"),
        ("reservations4/", "Add DHCPv4 Reservation"),
        ("dhcp4/options/", "Save Options"),
    ],
)
def test_mutation_controls_are_disabled_in_a_branch(
    page: Page,
    suffix: str,
    label: str,
    kea_server,
    kea_client,
    branches: _Branches,
    nb_http: requests.Session,
    netbox_url: str,
    plugin_base,
) -> None:
    branch = branches.create()
    url = f"{plugin_base}/servers/{kea_server.id}/{suffix}"
    response = page.goto(url)
    assert response is not None and response.status == 200
    page.wait_for_load_state("networkidle")
    control = _control(page, label)
    expect(control).not_to_have_attribute("aria-disabled", "true")
    expect(control).to_be_enabled()

    before = _state(kea_client, nb_http, netbox_url, branch)
    _activate(page, plugin_base, branch)
    page.goto(url)
    page.wait_for_load_state("networkidle")
    sent: list[str] = []
    page.on(
        "request",
        lambda request: (
            sent.append(request.url)
            if request.url.startswith(plugin_base) and request.method not in {"GET", "HEAD", "OPTIONS"}
            else None
        ),
    )

    _assert_disabled_explanation(page, _control(page, label))
    assert urlsplit(page.url).path == urlsplit(url).path
    assert parse_qs(urlsplit(page.url).query) == parse_qs(urlsplit(url).query)
    if suffix.startswith("leases4/"):
        search = page.locator("#lease-search-btn")
        expect(search).to_be_enabled()
        with page.expect_response(
            lambda response: "/leases4/" in response.url and response.request.headers.get("hx-request") == "true"
        ) as refresh:
            search.click()
        assert refresh.value.status == 200
        _assert_disabled_explanation(page, _control(page, label))
    if suffix == "dhcp4/options/":
        form = page.locator("form").filter(has=_control(page, label))
        field = form.locator('input[type="text"]').first
        expect(field).to_be_visible()
        field.press("Enter")
        page.wait_for_load_state("networkidle")
        expect(page).to_have_url(url)
        expect(page.get_by_role("link", name="Cancel", exact=True)).to_be_enabled()
    assert sent == [], f"A disabled control sent a mutation request: {sent}"
    assert _state(kea_client, nb_http, netbox_url, branch) == before


def test_sync_all_in_a_branch_is_refused_and_the_page_reloads_in_the_branch(
    page: Page, kea_server, kea_client, branches: _Branches, nb_http: requests.Session, netbox_url: str, plugin_base
) -> None:
    branch = branches.create()
    before = _state(kea_client, nb_http, netbox_url, branch)
    row = _reservation_row(page, plugin_base, kea_server.id)
    _activate_in_other_tab(page, plugin_base, branch)

    assert _click_sync_all(page, row) == 409

    expect(_toast(page, f"Branch {branch['name']} {_REFUSED}")).to_be_visible()
    expect(page).to_have_url(re.compile(rf"/servers/{kea_server.id}/reservations4/$"))
    expect(page.locator(f"{_BANNER_SELECTOR}:visible")).to_be_visible()
    assert _state(kea_client, nb_http, netbox_url, branch) == before


def _assert_on_main(page: Page, plugin_base: str) -> None:
    """The stale-selector refusal leaves the user on main's Server list, with the refusal shown."""
    expect(page).to_have_url(f"{plugin_base}/servers/?_branch=")
    expect(_toast(page, _UNUSABLE)).to_be_visible()
    expect(page.locator(_BANNER_SELECTOR)).to_have_count(0)


def test_sync_all_after_the_branch_merged_elsewhere_is_refused_and_goes_to_main(
    page: Page, kea_server, kea_client, branches: _Branches, nb_http: requests.Session, netbox_url: str, plugin_base
) -> None:
    branch = branches.create()
    before = _state(kea_client, nb_http, netbox_url)
    row = _reservation_row(page, plugin_base, kea_server.id)
    _activate_in_other_tab(page, plugin_base, branch)
    branches.merge(branch)

    assert _click_sync_all(page, row) == 409

    _assert_on_main(page, plugin_base)
    assert _state(kea_client, nb_http, netbox_url) == before


def test_sync_all_on_a_stale_page_with_a_deleted_branch_is_refused_and_goes_to_main(
    page: Page, kea_server, kea_client, branches: _Branches, nb_http: requests.Session, netbox_url: str, plugin_base
) -> None:
    branch = branches.create()
    before = _state(kea_client, nb_http, netbox_url)
    row = _reservation_row(page, plugin_base, kea_server.id)
    _activate_in_other_tab(page, plugin_base, branch)
    branches.delete(branch)

    assert _click_sync_all(page, row) == 409

    _assert_on_main(page, plugin_base)
    assert _state(kea_client, nb_http, netbox_url) == before


def test_sync_all_with_a_stale_cookie_after_the_server_was_deleted_in_main_goes_to_main(
    page: Page, kea_server, kea_client, branches: _Branches, nb_http: requests.Session, netbox_url: str, plugin_base
) -> None:
    branch = branches.create()
    before = _state(kea_client, nb_http, netbox_url)
    row = _reservation_row(page, plugin_base, kea_server.id)
    _activate_in_other_tab(page, plugin_base, branch)
    branches.merge(branch)
    kea_server.delete()

    assert _click_sync_all(page, row) == 409

    # The page's own Server is gone, so a 404 there would consume the message; the Server list cannot 404.
    _assert_on_main(page, plugin_base)
    assert _state(kea_client, nb_http, netbox_url) == before


@pytest.mark.parametrize(
    ("netbox_username", "netbox_password", "netbox_user_permissions"),
    [("kea-sync-user", "kea-sync-user12Characters", _SYNC_USER_PERMISSIONS)],
)
@pytest.mark.parametrize("recovery", ["with view_server", "without view_server", "anonymous"])
def test_stale_selector_redirect_shows_refusal(
    page: Page,
    recovery: str,
    netbox_username: str,
    nb_api: pynetbox.api,
    kea_server,
    kea_client,
    branches: _Branches,
    nb_http: requests.Session,
    netbox_url: str,
    plugin_base,
) -> None:
    branch = branches.create()
    before = _state(kea_client, nb_http, netbox_url)
    row = _reservation_row(page, plugin_base, kea_server.id)
    _activate_in_other_tab(page, plugin_base, branch)
    branches.merge(branch)
    if recovery == "without view_server":
        # Keep the permission object, so the login fixture can still delete it.
        (server_view,) = [
            permission
            for permission in nb_api.users.permissions.filter(name=netbox_username)
            if "netbox_kea.server" in permission.object_types
        ]
        server_view.object_types = ["dcim.site"]
        assert server_view.save()
    elif recovery == "anonymous":
        page.context.clear_cookies(name="sessionid")

    assert _click_sync_all(page, row) == 409

    if recovery == "anonymous":
        expect(page).to_have_url(re.compile(r"/login/\?next="))
    else:
        expect(page).to_have_url(f"{plugin_base}/servers/?_branch=")
    if recovery == "without view_server":
        expect(page).to_have_title(re.compile("Access Denied"))
    expect(_toast(page, _UNUSABLE)).to_be_visible()
    expect(page.locator(_BANNER_SELECTOR)).to_have_count(0)
    assert _state(kea_client, nb_http, netbox_url) == before

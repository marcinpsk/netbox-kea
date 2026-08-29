"""Browser workflow tests: navigation, badge enrichment, and CRUD lifecycles.

They share the ``tests/ui`` harness with ``test_ui.py`` and run against the compose
stack that ``tests/test_setup.sh`` starts. See AGENTS.md for how to start it.
"""

import ipaddress
import re
import subprocess
import sys
import warnings
from urllib.parse import urlencode

import pytest
import requests
from playwright.sync_api import Locator, Page, expect

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _check_no_django_error(page: Page) -> None:
    """Fail immediately if the page shows a Django error page."""
    title = page.title()
    assert "Server Error" not in title, f"Django 500 at {page.url}"
    assert "Page not found" not in title, f"Django 404 at {page.url}"


def _submit_and_wait_nav(page: Page, js: str) -> None:
    """Run a form-submitting JS snippet and wait for the resulting navigation.

    Replaces the deprecated/racy ``page.expect_navigation()``: capture the current
    URL, run the submit, then wait for the URL to change (these submits always
    redirect to a different page) and for the network to settle.
    """
    prev_url = page.url
    page.evaluate(js)
    page.wait_for_url(lambda u: u != prev_url)
    page.wait_for_load_state("networkidle")


def _assert_no_none_pk(page: Page) -> None:
    """Fail if the URL contains the tell-tale pk=None pattern (regression guard)."""
    assert not re.search(r"/None(?:[/?#]|$)", page.url), f"URL contains /None — pk=None bug triggered at {page.url}"


def _subnet_cidr_for_id(page: Page, subnet_id: int) -> str:
    """Return the subnet CIDR for *subnet_id*, read from the reservation add form's datalist.

    The reservation form takes a CIDR, but the edit/delete routes are still keyed by
    subnet id. Reading the mapping off the page keeps the test independent of whatever
    subnets the live Kea happens to be configured with.
    """
    cidr = page.evaluate(
        """(subnetId) => {
            const input = document.getElementById('id_subnet');
            const datalistId = input ? input.getAttribute('list') : '';
            const datalist = datalistId ? document.getElementById(datalistId) : null;
            if (!datalist) return '';
            const opts = [...datalist.querySelectorAll('option')];
            const match = opts.find((o) => o.textContent.trim() === `id ${subnetId}`);
            return match ? match.value : '';
        }""",
        subnet_id,
    )
    assert cidr, f"Subnet id {subnet_id} is not offered on the reservation add form"
    return cidr


def _assert_no_http_errors(errors: list) -> None:
    """Fail on any tracked 4xx or 5xx response.

    ``track_http_errors`` records every response at 400 and above. Asserting only on
    5xx let a 400 or 403 after a form submit pass as success.
    """
    assert not errors, f"4xx/5xx responses during test: {errors}"


def _tail_container_logs(service: str = "netbox", lines: int = 30) -> str:
    """Return the last N lines from one Compose service's stderr (Django log).

    Filtering on the service label rather than the container name: the stack also runs
    netbox-worker and netbox-housekeeping, whose names contain "netbox" too.
    """
    try:
        name = (
            subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.service={service}",
                    "--format",
                    "{{.Names}}",
                ],
                timeout=5,
            )
            .decode()
            .strip()
            .split("\n")[0]
        )
        return subprocess.check_output(
            ["docker", "logs", "--tail", str(lines), name],
            stderr=subprocess.STDOUT,
            timeout=5,
        ).decode()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# TestServerList — no live Kea needed
# ---------------------------------------------------------------------------


class TestServerList:
    """Server list page and bulk-action sanity checks."""

    def test_page_loads(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        track_http_errors: list,
    ) -> None:
        page.goto(f"{plugin_base}/servers/")
        _check_no_django_error(page)
        # Confirm the server list rendered: URL is correct and page has a table or add link
        assert "/plugins/kea/servers" in page.url
        expect(page.locator("a[href*='/servers/add/']").first).to_be_visible()
        _assert_no_http_errors(track_http_errors)

    @pytest.mark.parametrize(
        ("action", "label"),
        [("_edit", r"edit.?selected"), ("_delete", r"delete.?selected")],
    )
    def test_bulk_action_without_a_selection_reaches_no_broken_url(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        action: str,
        label: str,
        track_http_errors: list,
    ) -> None:
        """A bulk action with nothing checked once submitted an empty selection and
        landed on ``/servers/None``.

        NetBox 4.6 renders the button disabled, which makes the action unreachable.
        NetBox 4.3 leaves it enabled, so the guard has to be the outcome of the click,
        not the state of the control.
        """
        page.goto(f"{plugin_base}/servers/")
        _check_no_django_error(page)

        button = page.get_by_role("button", name=re.compile(label, re.I))
        expect(button).to_be_visible()
        if button.is_disabled():
            assert page.locator(f'button[name="{action}"]:not([disabled])').count() == 0
        else:
            button.click()
            page.wait_for_load_state("networkidle")
        _assert_no_none_pk(page)
        _check_no_django_error(page)
        assert not [error for error in track_http_errors if error[0] == 404], (
            f"Got 404 from the empty {action} selection: {track_http_errors}"
        )
        _assert_no_http_errors(track_http_errors)

    def test_import_button_loads_form_not_404(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        track_http_errors: list,
    ) -> None:
        """Regression: clicking 'Import' on the server list must load the import form, not 404.

        Before the fix, ``/plugins/kea/servers/import/`` had no URL pattern and
        returned 404 immediately.
        """
        page.goto(f"{plugin_base}/servers/")
        _check_no_django_error(page)

        # Verify the Import link is rendered on the page (not missing)
        import_link = page.locator('a[href*="/servers/import/"]')
        expect(import_link).to_be_visible()

        # Navigate directly — the Django Debug Toolbar overlay blocks pointer
        # events on this link (same issue as other action buttons in the suite).
        page.goto(f"{plugin_base}/servers/import/")
        page.wait_for_load_state("networkidle")

        _check_no_django_error(page)
        _assert_no_none_pk(page)
        assert "/servers/import" in page.url, f"Expected import URL, got {page.url}"

        http_404 = [e for e in track_http_errors if e[0] == 404]
        assert not http_404, f"Got 404 navigating to import page: {http_404}"


# ---------------------------------------------------------------------------
# TestAddServerForm — no live Kea needed for GET
# ---------------------------------------------------------------------------


class TestAddServerForm:
    """Add-server form render and basic validation."""

    def test_form_get_renders_required_fields(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        track_http_errors: list,
    ) -> None:
        page.goto(f"{plugin_base}/servers/add/")
        _check_no_django_error(page)

        expect(page.get_by_label("Name", exact=True)).to_be_visible()
        expect(page.get_by_label("CA / Server URL", exact=True)).to_be_visible()
        expect(page.locator("#id_ca_username")).to_be_visible()
        # Checkboxes use IDs reliably across NetBox versions
        expect(page.locator("#id_dhcp4")).to_be_visible()
        expect(page.locator("#id_dhcp6")).to_be_visible()
        expect(page.locator("#id_has_control_agent")).to_be_visible()
        _assert_no_http_errors(track_http_errors)

    def test_form_missing_name_rerenders_not_none_url(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        track_http_errors: list,
    ) -> None:
        """Submitting without a name must re-render the form, NOT redirect to /servers/None."""
        page.goto(f"{plugin_base}/servers/add/")
        page.get_by_label("CA / Server URL", exact=True).fill("http://not-real")
        page.locator('[name="_create"]').click()
        page.wait_for_load_state("networkidle")

        _assert_no_none_pk(page)
        _check_no_django_error(page)
        # Either stayed on /add/ or back on add form — never on /servers/None
        assert "servers/None" not in page.url


# ---------------------------------------------------------------------------
# Server tabs
# ---------------------------------------------------------------------------


class TestKeaServerTabs:
    """Every Server tab renders against the harness Kea daemons."""

    def test_server_detail_page_loads(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Detail page of a live Kea server loads without errors."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/")
        _check_no_django_error(page)
        expect(page.get_by_role("heading", name=kea_server.name)).to_be_visible()
        _assert_no_http_errors(track_http_errors)

    def test_server_status_tab(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/status/")
        _check_no_django_error(page)
        # Status tab should surface daemon version / uptime data from Kea
        expect(page.get_by_text(re.compile(r"version|uptime|pid", re.I)).first).to_be_visible()
        _assert_no_http_errors(track_http_errors)

        # The helper returns "" for every failure, so an empty result proves nothing.
        logs = _tail_container_logs()
        if logs:
            assert "ERROR" not in logs, f"Unexpected server errors after status tab:\n{logs}"

    def test_server_leases4_tab(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/leases4/")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

    def test_server_leases6_tab(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/leases6/")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

    def test_server_subnets4_tab(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/subnets4/")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

    def test_server_subnets6_tab(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/subnets6/")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

    def test_edit_server_via_form(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Editing a server via the edit form redirects to a real integer-pk URL."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/edit/")
        _check_no_django_error(page)

        name_field = page.get_by_label("Name", exact=True)
        original = name_field.input_value()
        name_field.fill(f"{original}-edited")
        # force=True bypasses Django Debug Toolbar overlay pointer-event interception
        page.locator('[name="_update"]').click(force=True)
        page.wait_for_load_state("networkidle")

        _assert_no_none_pk(page)
        _check_no_django_error(page)
        assert re.search(r"/servers/\d+", page.url), f"Expected redirect to integer-pk detail URL, got: {page.url}"
        _assert_no_http_errors(track_http_errors)

    def test_bulk_edit_selected_server_reaches_edit_page(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Selecting a server and clicking 'Edit Selected' reaches /servers/edit/."""
        page.goto(f"{plugin_base}/servers/")
        # Select the first row checkbox
        page.locator("table input[type=checkbox]").first.check()
        page.get_by_role("button", name=re.compile(r"edit.?selected", re.I)).click()
        page.wait_for_load_state("networkidle")

        _assert_no_none_pk(page)
        _check_no_django_error(page)
        assert "servers/edit" in page.url, f"Expected /servers/edit/ after bulk edit, got: {page.url}"
        _assert_no_http_errors(track_http_errors)

    def test_delete_server_via_confirmation_form(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        nb_http: requests.Session,
        netbox_url: str,
        track_http_errors: list,
    ) -> None:
        """Confirming server deletion removes it from NetBox."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/delete/")
        _check_no_django_error(page)

        # Submit via JS — djDebug intercepts pointer events.
        _submit_and_wait_nav(
            page,
            "var forms = document.querySelectorAll('form[method=\"post\"]'); forms[forms.length - 1].submit()",
        )

        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

        # Verify the API agrees the server is gone
        resp = nb_http.get(f"{netbox_url}/api/plugins/kea/servers/{server_id}/", timeout=5)
        assert resp.status_code == 404, f"Server {server_id} should be deleted but API returned {resp.status_code}"


# ---------------------------------------------------------------------------
# TestCombinedViews — navigation checks, no live Kea required
# ---------------------------------------------------------------------------


class TestCombinedViews:
    """Combined multi-server views load and show the expected structure."""

    COMBINED_TABS = [
        ("combined", "combined/"),
        ("combined_leases4", "combined/leases4/"),
        ("combined_leases6", "combined/leases6/"),
        ("combined_reservations4", "combined/reservations4/"),
        ("combined_reservations6", "combined/reservations6/"),
        ("combined_subnets4", "combined/subnets4/"),
        ("combined_subnets6", "combined/subnets6/"),
    ]

    @pytest.mark.parametrize(
        ("_tab_name", "path"),
        COMBINED_TABS,
        ids=[name for name, _path in COMBINED_TABS],
    )
    def test_combined_tab_loads(
        self,
        _tab_name: str,
        path: str,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        track_http_errors: list,
    ) -> None:
        page.goto(f"{plugin_base}/{path}")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

    def test_combined_dashboard_has_navigation_tabs(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        track_http_errors: list,
    ) -> None:
        """Combined dashboard must render tab links for all sub-views."""
        page.goto(f"{plugin_base}/combined/")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        for _name, path in self.COMBINED_TABS[1:]:  # skip dashboard itself
            link = page.locator(f'a[href*="{path}"]').first
            assert link.count(), f"Missing tab link for {path}"
            expect(link).to_be_visible()
        _assert_no_http_errors(track_http_errors)


# ---------------------------------------------------------------------------
# TestCombinedViewsWithKea — column/table structure with a real server
# ---------------------------------------------------------------------------


class TestCombinedViewsWithKea:
    """Combined view content checks that require a live Kea server fixture."""

    @pytest.mark.parametrize("header", ("Server", "Reserved", "NetBox IP"))
    def test_combined_leases4_column_present(
        self,
        header: str,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Combined leases4 must show each required column as its own case."""
        page.goto(f"{plugin_base}/combined/leases4/?q=192.0.2.1&by=ip")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        expect(page.locator("th", has_text=header).first).to_be_visible()
        _assert_no_http_errors(track_http_errors)

    def test_combined_reservations4_server_column_present(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Combined reservations4 table must include a 'Server' column header."""
        # Reservations view also renders table on page load (no search required)
        page.goto(f"{plugin_base}/combined/reservations4/")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        expect(page.locator("th", has_text="Server").first).to_be_visible()
        _assert_no_http_errors(track_http_errors)

    def test_combined_reservations4_lease_column_present(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Combined reservations4 table must include a 'Lease' column header."""
        page.goto(f"{plugin_base}/combined/reservations4/")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        expect(page.locator("th", has_text="Lease").first).to_be_visible()
        _assert_no_http_errors(track_http_errors)

    def test_per_server_leases4_reserved_column_present(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Per-server leases4 table must include 'Reserved' column header.

        Note: 'NetBox IP' is intentionally not checked here because django_tables2 persists
        per-user column visibility preferences; the admin user may have toggled it off.
        Use the combined view tests to verify NetBox IP column presence.
        """
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/leases4/?q=192.0.2.1&by=ip")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        expect(page.locator("th", has_text="Reserved").first).to_be_visible()
        _assert_no_http_errors(track_http_errors)

    def test_per_server_reservations4_lease_column_present(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Per-server reservations4 table must include 'Lease' and 'NetBox IP' columns."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/reservations4/")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        expect(page.locator("th", has_text="Lease").first).to_be_visible()
        expect(page.locator("th", has_text="NetBox IP").first).to_be_visible()
        _assert_no_http_errors(track_http_errors)


# ---------------------------------------------------------------------------
# TestBadgeEnrichment — verify badge rendering with live Kea data
# ---------------------------------------------------------------------------


class TestBadgeEnrichment:
    """Badge enrichment rendering against live Kea servers.

    These tests inspect live Reservation and lease data through the plugin UI.
    """

    def test_reserved_badge_appears_on_leases4_for_known_reservation(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """A lease with a matching reservation must show the 'Reserved' badge on the leases page."""
        server_id = kea_server.id
        # Use a search query to trigger table rendering; even with no results headers must be visible
        page.goto(f"{plugin_base}/servers/{server_id}/leases4/?q=192.0.2.1&by=ip")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

        # The 'Reserved' column header must always be present regardless of data
        expect(page.locator("th", has_text="Reserved").first).to_be_visible()

        # A plain span is the correct render for a global or non-editable reservation
        # (see netbox_kea/tables.py), so only the linked variant carries a contract.
        reserved_links = page.locator('a.badge:has-text("Reserved")')
        if reserved_links.count() > 0:
            # Spot-check first link href points to a reservation URL
            href = reserved_links.first.get_attribute("href")
            assert href and "reservations4" in href, f"Reserved badge link href does not point to reservations: {href}"

    def test_active_lease_badge_is_link_on_reservations4_page(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """'Active Lease' badges on the reservations4 page must be clickable links."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/reservations4/")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

        expect(page.locator("th", has_text="Lease").first).to_be_visible()

        # 'Active Lease' must be an <a>, never a plain <span>
        active_spans = page.locator('span.badge:has-text("Active Lease")')
        assert active_spans.count() == 0, (
            f"Found {active_spans.count()} non-link 'Active Lease' badge(s); they must all be <a> elements"
        )

        # 'No Lease' must be a plain <span>, never a link
        no_lease_links = page.locator('a.badge:has-text("No Lease")')
        assert no_lease_links.count() == 0, (
            f"Found {no_lease_links.count()} linked 'No Lease' badge(s); they must be plain spans"
        )

        # If Active Lease badges exist, their href must point to the lease search
        active_links = page.locator('a.badge:has-text("Active Lease")')
        if active_links.count() > 0:
            href = active_links.first.get_attribute("href")
            assert href and "leases4" in href, f"Active Lease link href does not point to leases4: {href}"
            assert "?q=" in href, f"Active Lease link missing ?q= query param: {href}"

    def test_active_lease_link_navigates_to_lease_search(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Clicking an 'Active Lease' badge navigates to the lease search page without errors."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/reservations4/")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)

        active_links = page.locator('a.badge:has-text("Active Lease")')
        if active_links.count() == 0:
            pytest.skip("No 'Active Lease' badges present; skipping click test")

        active_links.first.click()
        page.wait_for_load_state("networkidle")

        _check_no_django_error(page)
        assert "leases4" in page.url, f"Expected leases4 URL after click, got: {page.url}"
        _assert_no_http_errors(track_http_errors)

    def test_combined_reservations4_active_lease_badge_is_link(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Combined reservations4 'Active Lease' badges must also be <a> links."""
        page.goto(f"{plugin_base}/combined/reservations4/")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

        active_spans = page.locator('span.badge:has-text("Active Lease")')
        assert active_spans.count() == 0, (
            f"Found {active_spans.count()} non-link 'Active Lease' badge(s) in combined view"
        )

    def test_netbox_ip_synced_badge_or_sync_button_present_on_leases4(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Combined leases4 'NetBox IP' column header is always present after a search.

        The combined view uses GlobalLeaseTable4 which has no saved per-user column config,
        so 'NetBox IP' is reliably visible. If lease rows are present, we also verify the
        enrichment widgets (Synced badge or Sync button) appear in the NetBox IP column.
        """
        # Combined view correctly shows all default columns (no saved user preferences)
        page.goto(f"{plugin_base}/combined/leases4/?q=192.0.2.1&by=ip")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)

        # Column header must always be present after a query (even with 0 results)
        expect(page.locator("th", has_text="NetBox IP").first).to_be_visible()

        # If real lease rows exist, at least one NetBox IP widget must be present
        real_rows = [r for r in page.locator("tbody tr").all() if "No leases found" not in r.text_content()]
        if real_rows:
            synced_badges = page.locator('a.badge:has-text("Synced")')
            sync_buttons = page.locator('button.badge:has-text("Sync")')
            total = synced_badges.count() + sync_buttons.count()
            assert total > 0, f"Leases4 table has {len(real_rows)} rows but no 'Synced' or 'Sync' widgets"


# ---------------------------------------------------------------------------
# Lease search modes (live Kea)
# ---------------------------------------------------------------------------


def _dismiss_debug_toolbar(page: Page) -> None:
    """Remove the Django Debug Toolbar overlay so it doesn't intercept clicks."""
    page.evaluate("() => { const el = document.getElementById('djDebug'); if (el) el.remove(); }")


class TestLeaseSearchModes:
    """Verify each lease search mode renders the table correctly against live Kea."""

    def test_search_by_subnet_shows_table(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Searching leases4 by subnet_id renders headers without a Django error."""
        server_id = kea_server.id
        # Navigate directly with URL params — avoids Tom Select interaction
        page.goto(f"{plugin_base}/servers/{server_id}/leases4/?q=1&by=subnet_id")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        expect(page.locator("th", has_text="IP Address").first).to_be_visible()

    def test_search_by_ip_shows_result_or_empty(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Searching leases4 by IP produces a valid page (found or not-found, never a 500)."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/leases4/?q=192.0.2.1&by=ip")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        expect(page.locator("th", has_text="IP Address").first).to_be_visible()

    def test_search_by_hostname_renders_table(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Searching leases4 by hostname renders the table without errors."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/leases4/?q=test&by=hostname")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        expect(page.locator("th", has_text="IP Address").first).to_be_visible()

    def test_search_by_mac_renders_table(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Searching leases4 by hardware address renders the table without errors.

        Note: BY_HW_ADDRESS constant value is "hw" (not "hw_address").
        """
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/leases4/?q=aa:bb:cc:dd:ee:ff&by=hw")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        expect(page.locator("th", has_text="IP Address").first).to_be_visible()

    def test_csv_export_returns_csv_content(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
    ) -> None:
        """CSV export for leases4 returns text/csv content.

        Uses the browser session (via page.context.request) so Django's session
        auth is used — regular plugin views don't accept Token auth headers.
        """
        server_id = kea_server.id
        url = f"{plugin_base}/servers/{server_id}/leases4/?q=1&by=subnet_id&export=all"
        # page.context.request shares the browser's cookie jar (including the session
        # cookie set by netbox_login), so it's fully authenticated.
        response = page.context.request.get(url)
        assert response.ok, f"CSV export returned HTTP {response.status}"
        ct = response.headers.get("content-type", "")
        assert "text/csv" in ct or "text/plain" in ct, f"Expected CSV content-type, got: {ct}"


# ---------------------------------------------------------------------------
# Reservation CRUD via UI (live Kea)
# ---------------------------------------------------------------------------


class TestReservationCRUD:
    """End-to-end create → verify → edit → delete reservation cycle via the UI."""

    # Deterministic test data — unlikely to clash with real production entries
    _TEST_MAC = "e2:e2:e2:e2:e2:01"
    _TEST_HOSTNAME = "e2e-crud-test"
    _TEST_HOSTNAME_EDITED = "e2e-crud-edited"
    _SUBNET_ID = 1
    # Host offset inside the selected Subnet. High enough to sit outside the pools a
    # live Kea normally allocates from, so the reservation does not clash with a lease.
    _TEST_HOST_OFFSET = -34

    def _test_ip(self, cidr: str) -> str:
        """Return the reservation address to use inside *cidr*.

        The address must belong to the Subnet the live Kea exposes for ``_SUBNET_ID``, so
        derive it instead of pinning one range.
        """
        network = ipaddress.ip_network(cidr)
        assert network.num_addresses >= abs(self._TEST_HOST_OFFSET), (
            f"Subnet {cidr} is too small for a reservation at host offset {self._TEST_HOST_OFFSET}"
        )
        return str(network[self._TEST_HOST_OFFSET])

    def _reservation_add_url(self, plugin_base: str, server_id: int) -> str:
        return f"{plugin_base}/servers/{server_id}/reservations4/add/"

    def _reservation_edit_url(self, plugin_base: str, server_id: int, subnet_id: int, identifier: str) -> str:
        query = urlencode({"identifier_type": "hw-address", "identifier": identifier})
        return f"{plugin_base}/servers/{server_id}/reservations4/{subnet_id}/edit/?{query}"

    def _reservation_delete_url(self, plugin_base: str, server_id: int, subnet_id: int, identifier: str) -> str:
        query = urlencode({"identifier_type": "hw-address", "identifier": identifier})
        return f"{plugin_base}/servers/{server_id}/reservations4/{subnet_id}/delete/?{query}"

    def _reservation_list_url(self, plugin_base: str, server_id: int) -> str:
        return f"{plugin_base}/servers/{server_id}/reservations4/"

    def _reservation_row(self, page: Page, plugin_base: str, server_id: int) -> Locator:
        page.goto(self._reservation_list_url(plugin_base, server_id))
        page.wait_for_load_state("networkidle")
        return page.locator("tr", has_text=self._TEST_MAC)

    def _submit_form_by_field(self, page: Page, field_id: str) -> None:
        """Submit the form that contains *field_id*, waiting for the resulting navigation."""
        _submit_and_wait_nav(page, f"document.getElementById('{field_id}').closest('form').submit()")

    def _fill_reservation_form(
        self,
        page: Page,
        cidr: str,
        ip: str,
        mac: str,
        hostname: str = "",
        identifier_type: str = "hw-address",
    ) -> None:
        """Fill the Reservation4Form fields.

        The ``identifier_type`` ChoiceField is wrapped by Tom Select.
        We set it via JavaScript on the underlying ``<select>`` then dispatch
        a 'change' event so the Tom Select widget syncs its display.
        """
        page.locator("#id_subnet_cidr").fill(cidr)
        page.locator("#id_ip_address").fill(ip)
        # Tom Select wraps identifier_type — set via JS
        page.evaluate(
            f"""() => {{
                const sel = document.getElementById('id_identifier_type');
                if (sel) {{
                    sel.value = '{identifier_type}';
                    sel.dispatchEvent(new Event('change'));
                }}
            }}"""
        )
        page.locator("#id_identifier").fill(mac)
        if hostname:
            page.locator("#id_hostname").fill(hostname)

    def _ui_cleanup_reservation(self, page: Page, plugin_base: str, server_id: int, *, strict: bool) -> None:
        """Delete any Reservation row matching ``_TEST_MAC`` through the UI.

        Safe to call twice. Strict cleanup raises failures. Lenient cleanup warns so it
        does not replace an exception that is already propagating from the test body.
        """
        try:
            stale_row = self._reservation_row(page, plugin_base, server_id)
            if not stale_row.count():
                return
            # get_attribute returns the raw root-relative href and this suite sets no base_url.
            delete_url = stale_row.first.locator('a[href*="/delete/"]').evaluate("el => el.href")
            assert delete_url
            page.goto(delete_url)
            page.wait_for_load_state("networkidle")
            _submit_and_wait_nav(
                page,
                "document.querySelectorAll('form[method=\"post\"]')"
                "[ document.querySelectorAll('form[method=\"post\"]').length - 1 ].submit()",
            )
            if self._reservation_row(page, plugin_base, server_id).count():
                message = f"Test reservation {self._TEST_MAC} is still present after delete"
                if strict:
                    raise AssertionError(message)
                warnings.warn(message, stacklevel=2)
        except Exception as exc:
            if strict:
                raise
            warnings.warn(f"Could not remove test reservation {self._TEST_MAC}: {exc}", stacklevel=2)

    def test_full_crud_lifecycle(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Create → verify listed → edit hostname → verify edit → delete → verify gone."""
        server_id = kea_server.id

        # ---- 0. PRE-CLEAN — remove any leftover from a previous interrupted run ----
        # Match on the identifier: the address is only known once the add form is open.
        self._ui_cleanup_reservation(page, plugin_base, server_id, strict=True)

        try:
            # ---- 1. CREATE ----
            page.goto(self._reservation_add_url(plugin_base, server_id))
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)
            _dismiss_debug_toolbar(page)

            cidr = _subnet_cidr_for_id(page, self._SUBNET_ID)
            test_ip = self._test_ip(cidr)
            self._fill_reservation_form(
                page,
                cidr,
                test_ip,
                self._TEST_MAC,
                hostname=self._TEST_HOSTNAME,
            )
            self._submit_form_by_field(page, "id_subnet_cidr")
            _check_no_django_error(page)
            _assert_no_http_errors(track_http_errors)

            # ---- 2. VERIFY LISTED ----
            page.goto(self._reservation_list_url(plugin_base, server_id))
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)
            expect(page.get_by_text(test_ip)).to_be_visible()

            # ---- 3. EDIT ----
            page.goto(self._reservation_edit_url(plugin_base, server_id, self._SUBNET_ID, self._TEST_MAC))
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)
            _dismiss_debug_toolbar(page)

            page.locator("#id_hostname").fill(self._TEST_HOSTNAME_EDITED)
            self._submit_form_by_field(page, "id_ip_address")
            _check_no_django_error(page)
            _assert_no_http_errors(track_http_errors)

            # ---- 4. VERIFY EDIT — reload list and confirm hostname changed ----
            page.goto(self._reservation_list_url(plugin_base, server_id))
            page.wait_for_load_state("networkidle")
            expect(page.get_by_text(test_ip)).to_be_visible()
            expect(page.get_by_text(self._TEST_HOSTNAME_EDITED)).to_be_visible()

            # ---- 5. DELETE ----
            page.goto(self._reservation_delete_url(plugin_base, server_id, self._SUBNET_ID, self._TEST_MAC))
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)
            # The delete confirmation form has only one unique element — submit it directly
            _submit_and_wait_nav(
                page,
                "document.querySelectorAll('form[method=\"post\"]')"
                "[ document.querySelectorAll('form[method=\"post\"]').length - 1 ].submit()",
            )
            _check_no_django_error(page)
            _assert_no_http_errors(track_http_errors)

            # ---- 6. VERIFY GONE ----
            page.goto(self._reservation_list_url(plugin_base, server_id))
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)
            assert test_ip not in page.content(), f"Reservation {test_ip} still visible after delete"
        finally:
            # A reservation left in live Kea blocks the next run on the same identifier.
            self._ui_cleanup_reservation(page, plugin_base, server_id, strict=sys.exc_info()[0] is None)

    def test_add_reservation_form_loads(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Add-reservation form renders without error."""
        server_id = kea_server.id
        page.goto(self._reservation_add_url(plugin_base, server_id))
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        expect(page.locator("#id_ip_address")).to_be_visible()

    def test_edit_reservation_form_loads(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Edit-reservation form loads for an existing reservation without error.

        Skips if no reservations exist on the live server.
        """
        server_id = kea_server.id
        page.goto(self._reservation_list_url(plugin_base, server_id))
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _dismiss_debug_toolbar(page)

        # Collect edit hrefs directly from the DOM to avoid pointer-event interception
        hrefs = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('a[href*="reservations4"][href*="/edit/"]')
            ).map(a => a.href)"""
        )
        if not hrefs:
            pytest.skip("No existing reservations to edit on live server")

        page.goto(hrefs[0])
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        expect(page.locator("#id_ip_address")).to_be_visible()

    def test_delete_confirmation_page_loads(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Delete confirmation page for a known reservation loads without error.

        Skips if no reservations exist on the live server.
        """
        server_id = kea_server.id
        page.goto(self._reservation_list_url(plugin_base, server_id))
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _dismiss_debug_toolbar(page)

        # Collect delete hrefs directly from the DOM
        hrefs = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('a[href*="reservations4"][href*="/delete/"]')
            ).map(a => a.href)"""
        )
        if not hrefs:
            pytest.skip("No existing reservations to delete on live server")

        page.goto(hrefs[0])
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        # The delete form is always the last form[method=post] on the page
        # (NetBox adds bookmark/subscribe forms before the content form)
        expect(page.locator('form[method="post"]').last).to_be_visible()


class TestPoolManagement:
    """E2E tests for pool add/delete against a live Kea server."""

    @staticmethod
    def _subnets4_url(plugin_base: str, server_id: int) -> str:
        return f"{plugin_base}/servers/{server_id}/subnets4/"

    @staticmethod
    def _pool_add_url(plugin_base: str, server_id: int, subnet_id: int) -> str:
        return f"{plugin_base}/servers/{server_id}/subnets4/{subnet_id}/pools/add/"

    @staticmethod
    def _pool_delete_url(plugin_base: str, server_id: int, subnet_id: int, pool: str) -> str:
        return f"{plugin_base}/servers/{server_id}/subnets4/{subnet_id}/pools/{pool}/delete/"

    def _discover_first_subnet(self, page: Page):
        """Extract subnet CIDR and add-pool URL from the first applicable table row."""
        return page.evaluate(
            """() => {
                const rows = document.querySelectorAll('table tbody tr');
                for (const row of rows) {
                    const addLink = row.querySelector('a[href*="pools/add"]');
                    if (!addLink) continue;
                    let subnet = null;
                    for (const cell of row.querySelectorAll('td')) {
                        const text = cell.textContent.trim();
                        if (/^[\\d.]+\\/\\d+$/.test(text)) { subnet = text; break; }
                    }
                    if (!subnet) continue;
                    return { add_url: addLink.href, subnet: subnet };
                }
                return null;
            }"""
        )

    def test_pool_add_form_loads(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Pool add form renders without error."""
        server_id = kea_server.id
        page.goto(self._subnets4_url(plugin_base, server_id))
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _dismiss_debug_toolbar(page)

        hrefs = page.evaluate("() => Array.from(document.querySelectorAll('a[href*=\"pools/add\"]')).map(a => a.href)")
        if not hrefs:
            pytest.skip("No add-pool links found on subnets4 page")

        page.goto(hrefs[0])
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        expect(page.locator("#id_pool")).to_be_visible()

    def test_pool_delete_form_loads(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """Pool delete confirmation page renders for an existing pool.

        Skips if the live server has no pools configured.
        """
        server_id = kea_server.id
        page.goto(self._subnets4_url(plugin_base, server_id))
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _dismiss_debug_toolbar(page)

        hrefs = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('a[href*="pools/"][href*="/delete/"]')
            ).map(a => a.href)"""
        )
        if not hrefs:
            pytest.skip("No delete-pool links found — no pools on live server")

        page.goto(hrefs[0])
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        expect(page.locator('form[method="post"]').last).to_be_visible()

    def _kea4_cleanup_pool(self, kea_client, subnet_id: int, pool: str, *, strict: bool) -> None:
        """Remove *pool* from *subnet_id* through Kea directly (safe to call twice).

        Strict cleanup raises failures. Lenient cleanup warns so it does not replace
        an exception that is already propagating from the test body.
        """
        try:
            data = kea_client.command("subnet4-get", service=["dhcp4"], arguments={"id": subnet_id}, check=(0, 3))[0]
            for subnet in (data.get("arguments") or {}).get("subnet4") or []:
                if not any(entry.get("pool") == pool for entry in subnet.get("pools") or []):
                    continue
                kea_client.command(
                    "subnet4-delta-del",
                    service=["dhcp4"],
                    arguments={"subnet4": [{"id": subnet_id, "subnet": subnet["subnet"], "pools": [{"pool": pool}]}]},
                    check=(0, 3),
                )
        except Exception as exc:
            if strict:
                raise
            warnings.warn(f"Could not remove test pool {pool} from subnet {subnet_id}: {exc}", stacklevel=2)

    def test_pool_add_and_delete_cycle(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        kea_client,
        track_http_errors: list,
    ) -> None:
        """Full cycle: add a pool → verify present → delete it → verify gone."""
        server_id = kea_server.id
        page.goto(self._subnets4_url(plugin_base, server_id))
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _dismiss_debug_toolbar(page)

        row_data = self._discover_first_subnet(page)
        if not row_data or not row_data.get("subnet"):
            pytest.skip("No subnets with add-pool buttons found on live server")

        net = ipaddress.IPv4Network(row_data["subnet"], strict=False)
        host_count = net.num_addresses - 2
        if host_count < 15:
            pytest.skip(f"Subnet {row_data['subnet']} too small for test pool")

        start_addr = ipaddress.IPv4Address(int(net.broadcast_address) - 10)
        end_addr = ipaddress.IPv4Address(int(net.broadcast_address) - 6)
        test_pool = f"{start_addr}-{end_addr}"

        # Parse subnet_id from the add_url path
        add_url_path = row_data["add_url"].split("//", 1)[-1].split("/", 1)[-1]
        parts = ("/" + add_url_path).rstrip("/").split("/")
        subnet_id = int(parts[parts.index("subnets4") + 1])

        try:
            # ---- ADD ----
            page.goto(self._pool_add_url(plugin_base, server_id, subnet_id))
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)
            page.fill("#id_pool", test_pool)
            _submit_and_wait_nav(page, "document.getElementById('id_pool').closest('form').submit()")
            _check_no_django_error(page)
            _assert_no_http_errors(track_http_errors)

            # ---- VERIFY ADDED ----
            page.goto(self._subnets4_url(plugin_base, server_id))
            page.wait_for_load_state("networkidle")
            assert test_pool in page.content(), f"Test pool {test_pool} not visible in subnets table after add"

            # ---- DELETE ----
            page.goto(self._pool_delete_url(plugin_base, server_id, subnet_id, test_pool))
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)
            _submit_and_wait_nav(
                page,
                "document.querySelectorAll('form[method=\"post\"]')"
                "[ document.querySelectorAll('form[method=\"post\"]').length - 1 ].submit()",
            )
            _check_no_django_error(page)
            _assert_no_http_errors(track_http_errors)

            # ---- VERIFY DELETED ----
            page.goto(self._subnets4_url(plugin_base, server_id))
            page.wait_for_load_state("networkidle")
            assert test_pool not in page.content(), f"Test pool {test_pool} still visible after delete"
        finally:
            # Kea rejects a duplicate range, so a pool left behind fails every later run.
            self._kea4_cleanup_pool(kea_client, subnet_id, test_pool, strict=sys.exc_info()[0] is None)


class TestSubnetManagement:
    """E2E tests for subnet add/delete against a live Kea server."""

    def _kea4_cleanup_subnet(self, kea_client, cidr: str, *, strict: bool) -> None:
        """Remove every Kea subnet whose CIDR matches *cidr* (direct API call).

        Strict cleanup raises failures. Lenient cleanup warns so it does not replace
        an exception that is already propagating from the test body.
        """
        try:
            data = kea_client.command("subnet4-list", service=["dhcp4"], check=(0, 3))[0]
            for s in (data.get("arguments") or {}).get("subnets", []):
                if s.get("subnet") == cidr:
                    kea_client.command("subnet4-del", service=["dhcp4"], arguments={"id": s["id"]}, check=(0, 3))
        except Exception as exc:
            if strict:
                raise
            warnings.warn(f"Could not remove test subnet {cidr}: {exc}", stacklevel=2)

    def test_subnet_add_form_loads(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
    ) -> None:
        """The add-subnet form renders without error."""
        server_id = kea_server.id
        page.goto(f"{plugin_base}/servers/{server_id}/subnets4/add/")
        page.wait_for_load_state("networkidle")
        _check_no_django_error(page)
        _assert_no_http_errors(track_http_errors)
        assert page.locator("#id_subnet").is_visible(), "Subnet CIDR input not found"

    def test_subnet_add_and_delete_cycle(
        self,
        page: Page,
        netbox_login: None,
        plugin_base: str,
        kea_server,
        track_http_errors: list,
        kea_client,
    ) -> None:
        """Full add→verify→delete cycle for a DHCPv4 subnet."""
        server_id = kea_server.id
        test_subnet = "10.254.253.0/24"
        test_pool = "10.254.253.10-10.254.253.20"

        # ---- PRE-CLEANUP: remove any leftover test subnets via direct Kea API ----
        self._kea4_cleanup_subnet(kea_client, test_subnet, strict=True)

        try:
            # ---- ADD ----
            page.goto(f"{plugin_base}/servers/{server_id}/subnets4/add/")
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)

            page.fill("#id_subnet", test_subnet)
            page.fill("#id_pools", test_pool)
            page.fill("#id_gateway", "10.254.253.1")

            _submit_and_wait_nav(page, "document.getElementById('id_subnet').closest('form').submit()")
            _check_no_django_error(page)
            _assert_no_http_errors(track_http_errors)

            # ---- VERIFY VISIBLE ----
            page.goto(f"{plugin_base}/servers/{server_id}/subnets4/")
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("table tbody tr", timeout=10000)
            rows_after_add = page.locator("table tbody tr").filter(has_text=test_subnet).all()
            assert rows_after_add, f"Test subnet {test_subnet} not visible in subnets table after add"

            # Find the new subnet's ID from the delete link in the matching row
            all_added_ids = []
            for row in rows_after_add:
                for link in row.locator("a[href*='subnets4/'][href*='/delete/']").all():
                    href = link.get_attribute("href") or ""
                    m = re.search(r"/subnets4/(\d+)/delete/$", href)
                    if m:
                        all_added_ids.append(int(m.group(1)))
            assert all_added_ids, f"Could not find any subnet delete links for {test_subnet}"
            new_subnet_id = max(all_added_ids)  # highest ID = the one we just added

            # ---- DELETE via UI ----
            page.goto(f"{plugin_base}/servers/{server_id}/subnets4/{new_subnet_id}/delete/")
            page.wait_for_load_state("networkidle")
            _check_no_django_error(page)
            assert test_subnet in page.content(), "Subnet CIDR not shown on delete confirmation page"

            _submit_and_wait_nav(page, "document.querySelector('.card-body form[method=\"post\"]').submit()")
            _check_no_django_error(page)
            _assert_no_http_errors(track_http_errors)

            # ---- VERIFY DELETED via direct Kea API ----
            data = kea_client.command("subnet4-list", service=["dhcp4"], check=(0, 3))[0]
            remaining_ids = {s["id"] for s in (data.get("arguments") or {}).get("subnets", [])}
            assert new_subnet_id not in remaining_ids, (
                f"Subnet ID {new_subnet_id} ({test_subnet}) still present in Kea after UI delete"
            )

        finally:
            # Unconditional: the helper matches by CIDR under check=(0, 3), so it is a no-op
            # once the UI delete succeeded, and no flag can be left stale by a failed wait.
            self._kea4_cleanup_subnet(kea_client, test_subnet, strict=sys.exc_info()[0] is None)

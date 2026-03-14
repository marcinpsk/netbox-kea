"""Fixtures for E2E Playwright tests of the NetBox Kea plugin.

Environment variables (all optional, have defaults):
    NETBOX_URL          NetBox base URL (default: http://localhost:8000)
    NETBOX_USERNAME     Admin username   (default: admin)
    NETBOX_PASSWORD     Admin password   (default: admin)
    KEA_V4_URL          Live Kea DHCPv4 URL (default: https://kea-v4-api.cnad.dev)
    KEA_V6_URL          Live Kea DHCPv6 URL (default: https://kea-v6-api.cnad.dev)
    KEA_API_USERNAME    Kea API username    (default: admin)
    KEA_API_PASSWORD    Kea API password    (required for live-Kea tests)
"""

import os
import re

import pytest
import requests
from playwright.sync_api import Page

NETBOX_URL = os.environ.get("NETBOX_URL", "http://127.0.0.1:8000")
NETBOX_USERNAME = os.environ.get("NETBOX_USERNAME", "admin")
NETBOX_PASSWORD = os.environ.get("NETBOX_PASSWORD", "admin")

KEA_V4_URL = os.environ.get("KEA_V4_URL", "https://kea-v4-api.cnad.dev")
KEA_V6_URL = os.environ.get("KEA_V6_URL", "https://kea-v6-api.cnad.dev")
KEA_API_USERNAME = os.environ.get("KEA_API_USERNAME", "admin")
KEA_API_PASSWORD = os.environ.get("KEA_API_PASSWORD", "")


# ---------------------------------------------------------------------------
# Session-scoped infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def netbox_url() -> str:
    return NETBOX_URL


@pytest.fixture(scope="session")
def plugin_base(netbox_url: str) -> str:
    return f"{netbox_url}/plugins/kea"


@pytest.fixture(scope="session")
def netbox_token(netbox_url: str) -> str:
    """Provision a short-lived API token for the admin user."""
    resp = requests.post(
        f"{netbox_url}/api/users/tokens/provision/",
        json={"username": NETBOX_USERNAME, "password": NETBOX_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("version") == 2:
        return f"nbt_{data['key']}.{data['token']}"
    return data["key"]


@pytest.fixture(scope="session")
def api_session(netbox_url: str, netbox_token: str) -> requests.Session:
    """Authenticated requests session for direct API calls."""
    s = requests.Session()
    auth_prefix = "Bearer" if netbox_token.startswith("nbt_") else "Token"
    s.headers.update(
        {
            "Authorization": f"{auth_prefix} {netbox_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    return s


@pytest.fixture(scope="session")
def live_kea_configured() -> None:
    """Skip the test (and everything that depends on it) when KEA_API_PASSWORD is absent."""
    if not KEA_API_PASSWORD:
        pytest.skip("KEA_API_PASSWORD not set; skipping live Kea tests")


# ---------------------------------------------------------------------------
# Per-test browser fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    """Add --no-sandbox so Chromium runs inside Docker without privilege issues."""
    return {
        **browser_type_launch_args,
        "args": ["--no-sandbox", "--disable-setuid-sandbox"],
    }


@pytest.fixture
def netbox_login(page: Page, netbox_url: str) -> None:
    """Log into NetBox via the browser login form."""
    page.goto(f"{netbox_url}/login/")
    page.get_by_label("Username").fill(NETBOX_USERNAME)
    page.get_by_label("Password").fill(NETBOX_PASSWORD)
    page.get_by_role("button", name=re.compile(r"sign.?in|log.?in", re.I)).click()
    page.wait_for_url(f"{netbox_url}/")


@pytest.fixture
def track_http_errors(page: Page) -> list[tuple[int, str]]:
    """Accumulate 4xx/5xx responses that occur during the test.

    Must be requested *before* any navigation so the listener is registered in time.
    """
    errors: list[tuple[int, str]] = []

    def _on_response(response):  # noqa: ANN001
        if response.status >= 400:
            errors.append((response.status, response.url))

    page.on("response", _on_response)
    return errors


# ---------------------------------------------------------------------------
# Per-test server fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def live_kea_server(
    api_session: requests.Session, netbox_url: str, live_kea_configured: None
) -> dict:
    """Create a Server pointing at the live Kea daemons; delete it after the test.

    Requires KEA_API_PASSWORD to be set (enforced by live_kea_configured).
    If a leftover server with the same name exists (e.g. from an interrupted run),
    it is deleted first so we always start with a fresh entry.
    """
    _SERVER_NAME = "e2e-live-kea"
    _SERVER_PAYLOAD = {
        "name": _SERVER_NAME,
        "server_url": KEA_V4_URL,
        "dhcp4_url": KEA_V4_URL,
        "dhcp6_url": KEA_V6_URL,
        "username": KEA_API_USERNAME,
        "password": KEA_API_PASSWORD,
        "has_control_agent": False,
        "dhcp4": True,
        "dhcp6": True,
        "ssl_verify": True,
    }

    # Clean up any leftover server from a previous interrupted run
    existing = api_session.get(
        f"{netbox_url}/api/plugins/kea/servers/",
        params={"name": _SERVER_NAME},
        timeout=10,
    )
    if existing.ok:
        for srv in existing.json().get("results", []):
            if srv["name"] == _SERVER_NAME:
                api_session.delete(
                    f"{netbox_url}/api/plugins/kea/servers/{srv['id']}/",
                    timeout=10,
                )

    resp = api_session.post(
        f"{netbox_url}/api/plugins/kea/servers/",
        json=_SERVER_PAYLOAD,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        pytest.fail(f"Could not create live Kea server via API: {resp.status_code} {resp.text}")
    server = resp.json()
    yield server
    # Teardown: ignore 404 (test may have deleted the server itself)
    api_session.delete(
        f"{netbox_url}/api/plugins/kea/servers/{server['id']}/",
        timeout=10,
    )

"""Browser-harness fixtures shared by every Playwright suite in ``tests/ui``.

Both UI modules drive one harness: the compose stack's NetBox, its two direct Kea
daemons, and one Server object joining them. Keeping the harness here is what lets
``pytest tests/`` run every browser test in one pass.
"""

import os
from typing import Any

import pynetbox
import pytest
import requests
from playwright.sync_api import Page

# This is linked from netbox_kea to avoid import errors
from ..kea import KeaClient


@pytest.fixture
def requests_session(nb_api: pynetbox.api) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Token {nb_api.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    return s


@pytest.fixture(autouse=True)
def clear_leases(kea_client: KeaClient) -> None:
    kea_client.command("lease4-wipe", service=["dhcp4"], check=(0, 3))
    kea_client.command("lease6-wipe", service=["dhcp6"], check=(0, 3))


@pytest.fixture(autouse=True)
def reset_user_preferences(requests_session: requests.Session, nb_api: pynetbox.api) -> None:
    r = requests_session.get(url=f"{nb_api.base_url}/users/config/")
    r.raise_for_status()
    tables_config = r.json().get("tables", {})

    # pynetbox doesn't support this endpoint
    requests_session.patch(
        url=f"{nb_api.base_url}/users/config/",
        json={"tables": {k: {} for k in tables_config}},
    ).raise_for_status()

    # restore pagination
    requests_session.patch(
        url=f"{nb_api.base_url}/users/config/",
        json={"pagination": {"placement": "bottom"}},
    ).raise_for_status()


@pytest.fixture
def kea_server(nb_api: pynetbox.api, kea_url: str, kea_dhcp6_url: str):
    """Create the one Server every browser test drives, and delete it afterwards.

    Kea 3.0 has no Control Agent, so each daemon gets its own URL.
    """
    server = nb_api.plugins.kea.servers.create(
        name="test", ca_url=kea_url, dhcp6_url=kea_dhcp6_url, has_control_agent=False
    )
    try:
        yield server
    finally:
        # The delete-lifecycle tests remove it through the UI, so it may already be gone.
        if nb_api.plugins.kea.servers.get(server.id) is not None:
            server.delete()


@pytest.fixture
def with_test_server(kea_server, page: Page, netbox_login: None, plugin_base: str):
    """Open the Server detail page, the starting point for the table-driven tests."""
    page.goto(f"{plugin_base}/servers/{kea_server.id}/")
    return kea_server


@pytest.fixture
def with_test_server_only6(nb_api: pynetbox.api, kea_dhcp6_url: str, page: Page, netbox_login: None, plugin_base: str):
    server = nb_api.plugins.kea.servers.create(
        name="only6", ca_url=kea_dhcp6_url, dhcp4=False, dhcp6=True, has_control_agent=False
    )
    try:
        page.goto(f"{plugin_base}/servers/{server.id}/")
        yield
    finally:
        server.delete()


@pytest.fixture
def with_test_server_only4(nb_api: pynetbox.api, kea_url: str, page: Page, netbox_login: None, plugin_base: str):
    server = nb_api.plugins.kea.servers.create(
        name="only4", ca_url=kea_url, dhcp4=True, dhcp6=False, has_control_agent=False
    )
    try:
        page.goto(f"{plugin_base}/servers/{server.id}/")
        yield
    finally:
        server.delete()


class _DualEndpointKeaClient:
    """Test-side client for Kea 3.0 (no Control Agent): routes each service to its own daemon socket."""

    def __init__(self, dhcp4: KeaClient, dhcp6: KeaClient) -> None:
        self._clients = {"dhcp4": dhcp4, "dhcp6": dhcp6}

    def command(self, command, service=None, arguments=None, check=(0,)):
        svc = (service or ["dhcp4"])[0]
        return self._clients[svc].command(command, service=[svc], arguments=arguments, check=check)


@pytest.fixture
def kea_client() -> _DualEndpointKeaClient:
    # Kea 3.0: two daemons, each on its own host-exposed HTTP control socket. These are
    # the host side of the same daemons KEA_DHCP4_URL/KEA_DHCP6_URL name for NetBox, so
    # a run that moves one must move the other or the two drive different daemons.
    return _DualEndpointKeaClient(
        KeaClient(os.environ.get("KEA_DHCP4_CONTROL_URL", "").strip() or "http://127.0.0.1:8001"),
        KeaClient(os.environ.get("KEA_DHCP6_CONTROL_URL", "").strip() or "http://127.0.0.1:8003"),
    )


@pytest.fixture
def kea(with_test_server, kea_client: _DualEndpointKeaClient) -> _DualEndpointKeaClient:
    return kea_client


@pytest.fixture
def plugin_base(netbox_url: str) -> str:
    return f"{netbox_url}/plugins/kea"


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    """Add --no-sandbox so Chromium runs inside a container without privilege issues."""
    return {
        **browser_type_launch_args,
        "args": ["--no-sandbox", "--disable-setuid-sandbox"],
    }


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


@pytest.fixture(scope="function")
def netbox_user_permissions() -> list[dict[str, list[Any]]]:
    return [{"actions": [], "object_types": []}]


@pytest.fixture(scope="function", autouse=True)
def netbox_login(
    page: Page,
    netbox_url: str,
    netbox_username: str,
    netbox_password: str,
    netbox_user_permissions: list[dict[str, list[Any]]],
    nb_api: pynetbox.api,
):
    to_delete = []
    if netbox_username != "admin":
        nb_api.users.users.filter(username=netbox_username).delete()
        nb_api.users.permissions.all(0).delete()
        user = nb_api.users.users.create(username=netbox_username, password=netbox_password)
        to_delete.append(user)
        for permission in netbox_user_permissions:
            p = nb_api.users.permissions.create(
                name=netbox_username,
                actions=permission["actions"],
                object_types=permission["object_types"],
                users=[user.id],
            )
            to_delete.append(p)

    page.goto(f"{netbox_url}/login/")
    page.get_by_label("Username").fill(netbox_username)
    page.get_by_label("Password").fill(netbox_password)
    page.get_by_role("button", name="Sign In").click()

    yield

    for obj in to_delete:
        assert obj.delete()

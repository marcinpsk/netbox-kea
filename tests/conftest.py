import os
import warnings
from collections.abc import Iterator
from functools import partial
from urllib.parse import urlsplit

import pynetbox
import pytest
import requests


def _delete_created_servers(api: pynetbox.api, created_server_ids: set[int]) -> None:
    """Delete every session-owned Server, and report failures without stopping."""
    for server_id in sorted(created_server_ids):
        try:  # noqa: PERF203 - each deletion needs independent best-effort cleanup
            server = api.plugins.kea.servers.get(server_id)
            if server is not None and server.delete() is not True:
                raise RuntimeError("the NetBox API did not confirm deletion")
        except Exception as exc:  # noqa: BLE001,PERF203 - cleanup must continue after each failure
            warnings.warn(f"Could not delete test Server {server_id}: {exc}", RuntimeWarning, stacklevel=2)


def _record_created_server_ids(
    response: requests.Response,
    *args,
    created_server_ids: set[int],
    netbox_url: str,
    **kwargs,
) -> requests.Response:
    """Record exact Server IDs from successful Pynetbox create responses."""
    request = response.request
    server_collection_path = urlsplit(f"{netbox_url}/api/plugins/kea/servers").path.rstrip("/")
    if (
        request is None
        or request.method != "POST"
        or urlsplit(response.url).path.rstrip("/") != server_collection_path
        or not 200 <= response.status_code < 300
    ):
        return response

    if "application/json" not in response.headers.get("Content-Type", "").lower():
        return response
    try:
        payload = response.json()
    except ValueError:
        return response
    records = payload if isinstance(payload, list) else [payload]
    created_server_ids.update(
        identifier
        for record in records
        if isinstance(record, dict)
        and isinstance((identifier := record.get("id")), int)
        and not isinstance(identifier, bool)
    )
    return response


@pytest.fixture(scope="session")
def netbox_url() -> str:
    # Overridable so the harness can be published on another port when 8000 is taken.
    # NETBOX_PORT is what Compose publishes on, so fall back to it before assuming 8000.
    # A blank value means "unset", and a trailing slash would double up the / in every
    # f-string that appends a path.
    port = os.environ.get("NETBOX_PORT", "").strip() or "8000"
    url = os.environ.get("NETBOX_URL", "").strip() or f"http://localhost:{port}"
    return url.rstrip("/")


@pytest.fixture(scope="session")
def netbox_token(netbox_url: str) -> str:
    resp = requests.post(
        f"{netbox_url}/api/users/tokens/provision/",
        json={"username": "admin", "password": "admin"},
    )
    resp.raise_for_status()

    data = resp.json()
    if data.get("version") == 2:
        return f"nbt_{data['key']}.{data['token']}"
    return data["key"]


@pytest.fixture(scope="session")
def netbox_username() -> str:
    return "admin"


@pytest.fixture(scope="session")
def netbox_password() -> str:
    return "admin"


@pytest.fixture(scope="session")
def kea_url() -> str:
    # Kea 3.0: no Control Agent — the DHCPv4 daemon's own HTTP control socket.
    # Used as ca_url / DHCPv4 endpoint; pair with kea_dhcp6_url for the v6 daemon.
    return os.environ.get("KEA_DHCP4_URL", "").strip() or "http://kea-dhcp4:8000"


@pytest.fixture(scope="session")
def kea_dhcp6_url() -> str:
    return os.environ.get("KEA_DHCP6_URL", "").strip() or "http://kea-dhcp6:8000"


@pytest.fixture(scope="session")
def kea_server_kwargs(kea_url: str, kea_dhcp6_url: str) -> dict:
    """Server-create kwargs for the Kea 3.0 dual-daemon harness (no Control Agent):
    ca_url/DHCPv4 -> kea-dhcp4, DHCPv6 -> kea-dhcp6, and has_control_agent disabled."""
    return {"ca_url": kea_url, "dhcp6_url": kea_dhcp6_url, "has_control_agent": False}


@pytest.fixture(scope="session")
def nb_http(netbox_token: str) -> requests.Session:
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


@pytest.fixture(scope="session", autouse=True)
def nb_api(netbox_url: str, netbox_token: str) -> Iterator[pynetbox.api]:
    """Return a client and delete only Servers that this test session created."""
    api = pynetbox.api(netbox_url, token=netbox_token)
    created_server_ids: set[int] = set()
    api.http_session.hooks["response"].append(
        partial(_record_created_server_ids, created_server_ids=created_server_ids, netbox_url=netbox_url)
    )

    yield api

    _delete_created_servers(api, created_server_ids)


@pytest.fixture
def kea_basic_url() -> str:
    return "http://nginx"


@pytest.fixture
def kea_basic_username() -> str:
    return "kea"


@pytest.fixture
def kea_basic_password() -> str:
    return "kea"


@pytest.fixture
def kea_https_url() -> str:
    return "https://nginx"


@pytest.fixture
def kea_cert_url() -> str:
    return "https://nginx:444"


@pytest.fixture
def kea_client_cert() -> str:
    return "/certs/netbox.crt"


@pytest.fixture
def kea_client_key() -> str:
    return "/certs/netbox.key"


@pytest.fixture
def kea_ca() -> str:
    return "/certs/nginx.crt"

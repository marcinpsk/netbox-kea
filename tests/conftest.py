import pynetbox
import pytest
import requests


@pytest.fixture(scope="session")
def netbox_url() -> str:
    return "http://localhost:8000"


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
    return "http://kea-dhcp4:8000"


@pytest.fixture(scope="session")
def kea_dhcp6_url() -> str:
    return "http://kea-dhcp6:8000"


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
def nb_api(netbox_url: str, netbox_token: str) -> pynetbox.api:
    api = pynetbox.api(netbox_url, token=netbox_token)
    api.plugins.kea.servers.delete(api.plugins.kea.servers.all())

    return api


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

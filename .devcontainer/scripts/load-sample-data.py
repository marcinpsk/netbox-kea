"""Load sample Kea DHCP server entries into NetBox for development.

Run from the NetBox shell:
    python manage.py shell < /workspaces/netbox-kea/.devcontainer/scripts/load-sample-data.py

Reads credentials from environment variables (set them in .env and source it first):
    KEA_V4_URL, KEA_V4_USERNAME, KEA_V4_PASSWORD
    KEA_V6_URL, KEA_V6_USERNAME, KEA_V6_PASSWORD
"""

import os

from netbox_kea.models import Server

_V4_URL = os.environ.get("KEA_V4_URL", "https://kea-v4-api.cnad.dev")
_V6_URL = os.environ.get("KEA_V6_URL", "https://kea-v6-api.cnad.dev")
_V4_USER = os.environ.get("KEA_V4_USERNAME", "admin")
_V6_USER = os.environ.get("KEA_V6_USERNAME", "admin")
_V4_PASS = os.environ.get("KEA_V4_PASSWORD", "")
_V6_PASS = os.environ.get("KEA_V6_PASSWORD", "")

_SERVERS = [
    dict(
        name="CNAD Kea DHCPv4",
        server_url=_V4_URL,
        username=_V4_USER,
        password=_V4_PASS,
        dhcp4=True,
        dhcp6=False,
        ssl_verify=True,
    ),
    dict(
        name="CNAD Kea DHCPv6",
        server_url=_V6_URL,
        username=_V6_USER,
        password=_V6_PASS,
        dhcp4=False,
        dhcp6=True,
        ssl_verify=True,
    ),
]

for _s in _SERVERS:
    _obj, _created = Server.objects.update_or_create(name=_s["name"], defaults=_s)
    if _created:
        print(f"Created: {_obj.name}")
    else:
        print(f"Updated: {_obj.name}")

# netbox-kea-ng

[![PyPI](https://img.shields.io/pypi/v/netbox-kea-ng)](https://pypi.org/project/netbox-kea-ng/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/netbox-kea-ng)](https://pypi.org/project/netbox-kea-ng/)
[![CI](https://img.shields.io/github/actions/workflow/status/marcinpsk/netbox-kea/ci.yml?branch=main&label=tests)](https://github.com/marcinpsk/netbox-kea/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/marcinpsk/netbox-kea/branch/main/graph/badge.svg)](https://codecov.io/gh/marcinpsk/netbox-kea)
[![CodeQL](https://github.com/marcinpsk/netbox-kea/actions/workflows/codeql.yml/badge.svg)](https://github.com/marcinpsk/netbox-kea/actions/workflows/codeql.yml)
[![REUSE](https://api.reuse.software/badge/github.com/marcinpsk/netbox-kea)](https://api.reuse.software/info/github.com/marcinpsk/netbox-kea)
[![License](https://img.shields.io/github/license/marcinpsk/netbox-kea)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/netbox-kea-ng)](https://pypi.org/project/netbox-kea-ng/)
[![NetBox](https://img.shields.io/badge/NetBox-%E2%89%A54.0.0-blue)](https://github.com/netbox-community/netbox)

> **Fork notice:** This is `netbox-kea-ng`, an independently maintained fork of
> [netbox-kea](https://github.com/devon-mar/netbox-kea) by
> [Devon Mar](https://github.com/devon-mar).
> Published to PyPI as **`netbox-kea-ng`** and maintained at this repository.
> Upstream changes are merged where applicable.

A full-featured NetBox plugin for [Kea DHCP](https://www.isc.org/kea/) server management.
Exposes Kea daemon status, live lease search/delete, reservation CRUD, subnet and pool management,
NetBox IPAM synchronisation, and periodic background sync — all directly inside the NetBox UI.

---

## Features

### Core (inherited from upstream)

- Connect to one or more Kea Control Agent endpoints — with optional separate DHCPv4 / DHCPv6 URLs per server
- View Kea daemon status (Control Agent + DHCPv4 + DHCPv6)
- Search, view, export, and delete DHCPv4 and DHCPv6 leases
- View DHCP subnets, shared networks, and pools from Kea's running configuration
- Navigate from a lease directly to the matching NetBox device/VM
- REST API and GraphQL support for managing `Server` objects

### New in this fork

#### Host Reservations (`host_cmds` hook)

- Full CRUD for DHCPv4 and DHCPv6 reservations via Kea's `host_cmds` hook
- Create reservations with fixed IP, hardware address, DUID, client-id, circuit-id, flex-id, or remote-id
- Edit and delete existing reservations; option-data per reservation
- Active lease status badge per reservation (requires `lease_cmds` hook)
- Gracefully degrades when `host_cmds` is not loaded

#### DHCP Options Management

- Edit per-subnet option-data (`subnet4/6-update`) and global server options
- Add, edit, and delete custom option definitions (`option_def_add/update/del`)
- Supports `always-send`, custom codes, types, and spaces

#### Pool & Shared Network Management

- List and inspect pools within subnets
- View shared networks with their member subnets
- Warning when a new reservation overlaps an existing pool range

#### NetBox IPAM Synchronisation

- **Manual sync** from lease or reservation tables: create/update a NetBox `IPAddress` with a single click
- Status mapping: `active` for leases, `reserved` for reservations
- Sets `dns_name` from Kea hostname → works automatically with [netbox-dns](https://github.com/peteeckel/netbox-plugin-dns) IPAMDNSsync
- Stale IP cleanup: configurable `remove` / `deprecate` / `none` for IPs no longer present in Kea

#### Periodic Background Sync (Background Worker)

- Automatic periodic sync of all Kea leases and reservations to NetBox IPAM
- Runs via NetBox's built-in `rqworker` infrastructure — no external scheduler required
- Configurable interval (default 5 minutes)
- Per-server error isolation: one failing server does not block others
- Summary logging per sync run

#### Combined Multi-Server Views

- Single view across all servers for leases and reservations
- Concurrent fetching (thread pool) from all servers

---

## Requirements

| Requirement | Notes |
|---|---|
| NetBox | 4.0, 4.1, 4.2, 4.3, 4.4 or 4.5 |
| Kea Control Agent | Required for all features |
| `lease_cmds` hook | Required for lease search and active lease badges |
| `host_cmds` hook | Required for reservation CRUD (open source since Kea 2.7.7) |

> **Kea version:** Tested with Kea v2.4+ using `memfile` or `mysql`/`pgsql` lease backends.

---

## Installation

1. Add `netbox-kea-ng` to `local_requirements.txt`:

    ```
    netbox-kea-ng
    ```

2. Enable the plugin in `configuration.py`:

    ```python
    PLUGINS = ["netbox_kea"]
    ```

3. Run database migrations:

    ```bash
    ./manage.py migrate
    ```

4. Restart the NetBox service.

### Background Sync (optional)

To enable periodic Kea→IPAM sync, ensure the NetBox background worker is running:

```bash
./manage.py rqworker
```

The sync job registers automatically when the worker starts. Configure the interval and behaviour in `configuration.py`:

```python
PLUGINS_CONFIG = {
    "netbox_kea": {
        # Sync interval in minutes (default: 5)
        "sync_interval_minutes": 5,
        # Sync active leases to NetBox (status=active)
        "sync_leases_enabled": True,
        # Sync reservations to NetBox (status=reserved)
        "sync_reservations_enabled": True,
        # Max leases fetched per server per run (0 = unlimited)
        "sync_max_leases_per_server": 50000,
    }
}
```

The background sync job appears in **System → Background Jobs** in the NetBox UI.

---

## Configuration Reference

All settings go under `PLUGINS_CONFIG["netbox_kea"]`:

| Setting | Default | Description |
|---|---|---|
| `kea_timeout` | `30` | HTTP timeout (seconds) for Kea API calls |
| `stale_ip_cleanup` | `"remove"` | What to do with IPs no longer in Kea: `"remove"`, `"deprecate"`, or `"none"` |
| `sync_interval_minutes` | `5` | Background sync interval (minutes) |
| `sync_leases_enabled` | `True` | Include active leases in background sync |
| `sync_reservations_enabled` | `True` | Include reservations in background sync |
| `sync_max_leases_per_server` | `50000` | Cap on leases fetched per server per sync run |

---

## Dual-URL Server Support

A single `Server` object can point to separate DHCPv4 and DHCPv6 Kea processes:

```
Server
  ├── dhcp4_url = https://kea-v4.example.com:8000
  └── dhcp6_url = https://kea-v6.example.com:8000
```

All views automatically route to the correct endpoint based on the IP version being queried.

---

## Custom Links

Add custom links to NetBox models to jump straight from a device or prefix to its DHCP leases.

Replace `<Kea Server ID>` with your server's object ID (visible top-right on the server detail page as `netbox_kea.server:<ID>`).

### DHCP leases for a prefix

**Content types:** `IPAM > Prefix`

```
https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases{{ object.prefix.version }}/?q={{ object.prefix }}&by=subnet
```

### DHCP leases for a device/VM interface (by MAC)

**Content types:** `DCIM > Interface`, `Virtualization > Interface`

DHCPv4:
```
https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases4/?q={{ object.mac_address }}&by=hw
```

DHCPv6:
```
https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases6/?q={{ object.mac_address }}&by=hw
```

### DHCP leases for a device/VM (by hostname)

**Content types:** `DCIM > Device`, `Virtualization > Virtual Machine`

```
https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases4/?q={{ object.name|lower }}&by=hostname
```

You can also use a custom field: replace `{{ object.name|lower }}` with `{{ object.cf.<your_field>|lower }}`.

---

## DNS Integration

When `dns_name` is set on a NetBox `IPAddress` (which this plugin does automatically from Kea hostnames), [netbox-plugin-dns](https://github.com/peteeckel/netbox-plugin-dns) with IPAMDNSsync enabled will automatically create A/AAAA/PTR records — provided matching DNS views and zones exist.

No additional configuration is required in this plugin.

---

## Screenshots

![Screenshot of DHCP leases](images/leases.png)

---

## Development

See [CHANGELOG](CHANGELOG.md) for release history.

```bash
# Install dev dependencies
uv sync

# Lint
uv run ruff check netbox_kea/
uv run ruff format --check netbox_kea/

# Auto-format
uv run ruff format netbox_kea/

# REUSE compliance check
uv run reuse lint

# Build wheel (required before integration tests)
uv build

# Run unit tests (no Docker)
uv run pytest

# Run integration tests (requires Docker)
./tests/test_setup.sh
uv run pytest tests/ --tracing=retain-on-failure -v

# Install pre-commit hooks
uv run pre-commit install
```

---

## License

[Apache-2.0](LICENSE) — original code by [Devon Mar](https://github.com/devon-mar), fork maintained by [Marcin Zieba](https://github.com/marcinpsk).

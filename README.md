# netbox-kea-ng

[![PyPI](https://img.shields.io/pypi/v/netbox-kea-ng)](https://pypi.org/project/netbox-kea-ng/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/netbox-kea-ng)](https://pypi.org/project/netbox-kea-ng/)
[![CI](https://img.shields.io/github/actions/workflow/status/marcinpsk/netbox-kea/ci.yml?branch=main&label=tests)](https://github.com/marcinpsk/netbox-kea/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/marcinpsk/netbox-kea/branch/main/graph/badge.svg)](https://codecov.io/gh/marcinpsk/netbox-kea)
[![CodeQL](https://github.com/marcinpsk/netbox-kea/actions/workflows/codeql.yml/badge.svg)](https://github.com/marcinpsk/netbox-kea/actions/workflows/codeql.yml)
[![REUSE](https://api.reuse.software/badge/github.com/marcinpsk/netbox-kea)](https://api.reuse.software/info/github.com/marcinpsk/netbox-kea)
[![License](https://img.shields.io/github/license/marcinpsk/netbox-kea)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/netbox-kea-ng)](https://pypi.org/project/netbox-kea-ng/)
[![NetBox](https://img.shields.io/badge/NetBox-%E2%89%A54.3.0-blue)](https://github.com/netbox-community/netbox)

> **Fork notice:** This is `netbox-kea-ng`, an independently maintained fork of
> [netbox-kea](https://github.com/devon-mar/netbox-kea) by
> [Devon Mar](https://github.com/devon-mar).
> It is published to PyPI as **`netbox-kea-ng`** and tracked in this repository.
> Upstream changes are periodically merged where applicable.

NetBox plugin for the [Kea DHCP](https://www.isc.org/kea/) server. Manage your DHCP infrastructure directly from NetBox — view daemon status, search and manage leases, manage host reservations, configure subnets/pools/options, and keep your NetBox IPAM synchronized with live Kea data via a background job.

## Features

### Core (from upstream)

- View Kea daemon status (Control Agent + DHCPv4/DHCPv6 daemons)
- Full DHCPv4 and DHCPv6 support
- Search, view, delete and export DHCP leases
- Search for NetBox devices/VMs directly from DHCP leases
- View DHCP subnets from Kea configuration
- REST API and GraphQL support for Server objects

### Additions in this fork

**Host Reservations**
- Full CRUD for DHCPv4 and DHCPv6 reservations via [`host_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#host-cmds) hook
- Identifier types: hw-address (v4), DUID (v6), client-id, flex-id, circuit-id, remote-id
- Per-reservation DHCP options
- Journal entries on add/edit/delete

**Subnet Management**
- Add, edit and delete subnets (requires [`subnet_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#subnet-cmds) or `config-set`)
- Pool management (add/delete pools per subnet)
- Shared network management (add/edit/delete)
- Per-subnet and global DHCP option editing

**IPAM Sync**
- Sync active leases → NetBox `IPAddress` (status `active`)
- Sync reservations → NetBox `IPAddress` (status `reserved`)
- Sync button on individual leases and reservations
- Bulk sync for entire lease tables
- Pending-change detection: badge on leases where a reservation exists at a different IP
- MAC address sync → NetBox `MACAddress`
- Sets `dns_name` on IPAddress for automatic DNS sync via [netbox-dns](https://github.com/peteeckel/netbox-plugin-dns) IPAMDNSsync

**Periodic Background Sync** *(requires `rqworker`)*
- Automatic Kea→NetBox IPAM sync on a configurable interval (default 5 minutes)
- Syncs all leases and reservations from all configured servers
- Visible in NetBox **System → Background Jobs**

**DHCP Control**
- Enable/disable DHCPv4 and DHCPv6 daemons from the NetBox UI

**Dual-URL Server**
- Optional separate URLs for DHCPv4 and DHCPv6 Control Agents
- Supports environments where v4 and v6 are served by separate Kea processes

**Global / Cross-Server Views**
- Combined dashboard, lease, reservation, subnet and shared-network views across all servers

**Lease Add / Edit / Bulk Import**
- Add and edit individual leases
- Bulk import leases from CSV

---

## Requirements

- NetBox 4.3, 4.4 or 4.5
- [Kea Control Agent](https://kea.readthedocs.io/en/latest/arm/agent.html)
- [`lease_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#lease-cmds-lease-commands-for-easier-lease-management) hook library (for lease search and management)
- [`host_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#host-cmds) hook library (optional, for reservation management)
- [`subnet_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#subnet-cmds) hook library (optional, for subnet add/edit/delete)

The plugin degrades gracefully when optional hooks are absent — tabs for unavailable features are hidden automatically.

---

## Compatibility

| netbox-kea-ng | NetBox | Kea |
|---|---|---|
| 1.x | 4.3 – 4.5 | 2.4+ |

Tested with Kea v2.4.1 using the `memfile` lease database. Other versions and databases should also work.

---

## Installation

### 1. Install the package

Add `netbox-kea-ng` to your `local_requirements.txt` (or install with pip):

```bash
pip install netbox-kea-ng
```

### 2. Enable the plugin

In `configuration.py`:

```python
PLUGINS = ["netbox_kea"]
```

Optionally configure plugin settings (see [Configuration](#configuration)):

```python
PLUGINS_CONFIG = {
    "netbox_kea": {
        "kea_timeout": 30,
        "sync_interval_minutes": 5,
        "sync_leases_enabled": True,
        "sync_reservations_enabled": True,
        "sync_max_leases_per_server": 50000,
        "stale_ip_cleanup": "remove",
    }
}
```

### 3. Run migrations

```bash
./manage.py migrate
```

### 4. Start the background worker (required for periodic sync)

The periodic IPAM sync job runs via NetBox's built-in `rqworker`. If you're not already running it:

```bash
./manage.py rqworker
```

The `Kea IPAM Sync` job will appear under **System → Background Jobs** and runs on the configured interval.

---

## Configuration

All settings are under `PLUGINS_CONFIG["netbox_kea"]`:

| Setting | Default | Description |
|---|---|---|
| `kea_timeout` | `30` | HTTP request timeout in seconds for Kea API calls |
| `stale_ip_cleanup` | `"remove"` | What to do with stale IPs after sync: `"remove"` (delete), `"deprecate"` (set status=deprecated), `"none"` (skip) |
| `sync_interval_minutes` | `5` | How often the background sync job runs (minutes). Also editable via NetBox admin → Jobs |
| `sync_leases_enabled` | `True` | Sync active DHCP leases to NetBox IPAM |
| `sync_reservations_enabled` | `True` | Sync Kea reservations to NetBox IPAM |
| `sync_max_leases_per_server` | `50000` | Hard cap on leases fetched per server per sync run. Set to `0` for no limit |

---

## Server Configuration

### Single-URL (standard)

Configure one `Server` URL that points to the Kea Control Agent:

| Field | Description |
|---|---|
| `CA / Server URL` (`ca_url`) | URL of the Kea Control Agent (e.g. `https://kea.example.com:8000`) |
| `DHCPv4` | Enable DHCPv4 lease/reservation/subnet management |
| `DHCPv6` | Enable DHCPv6 lease/reservation/subnet management |
| `CA Username` (`ca_username`) / `CA Password` (`ca_password`) | HTTP Basic Auth credentials (if required) |
| `CA File Path` | Path to a custom CA certificate file for TLS verification |
| `SSL Verification` | Enable/disable TLS certificate verification (enabled by default) |

### Dual-URL (separate v4/v6 processes)

When DHCPv4 and DHCPv6 are served by separate Kea processes (each with its own Control Agent):

| Field | Description |
|---|---|
| `DHCPv4 URL` | URL of the Control Agent for the DHCPv4 daemon |
| `DHCPv6 URL` | URL of the Control Agent for the DHCPv6 daemon |

The main `CA URL` (`ca_url`) is required and acts as a fallback for any protocol without a dedicated URL.
By default, both `DHCPv4 URL` and `DHCPv6 URL` use CA-level credentials; see **Per-protocol credentials** below for optional overrides.

---

### Per-protocol credentials

When connecting directly to DHCP daemons (bypassing the Control Agent), you can configure
separate credentials per protocol:

| Field | Description |
|-------|-------------|
| `dhcp4_username` | Username for the DHCPv4 daemon (overrides `ca_username` for DHCPv4) |
| `dhcp4_password` | Password for the DHCPv4 daemon (overrides `ca_password` for DHCPv4) |
| `dhcp6_username` | Username for the DHCPv6 daemon (overrides `ca_username` for DHCPv6) |
| `dhcp6_password` | Password for the DHCPv6 daemon (overrides `ca_password` for DHCPv6) |

If per-protocol credentials are not set, the CA-level credentials (`ca_username`/`ca_password`)
are used as the default for all connections.

---

## Background IPAM Sync

The `Kea IPAM Sync` job runs automatically when `rqworker` is active:

1. Iterates all configured `Server` objects
2. For each server: fetches all active leases (v4 + v6) and all reservations
3. Creates or updates NetBox `IPAddress` objects:
   - Leases → `status=active`, `dns_name` set from Kea hostname
   - Reservations → `status=reserved`, `dns_name` set from Kea hostname
4. Cleans up stale IPs (configurable via `stale_ip_cleanup`)
5. One server failing does not block others
6. Summary logged per server and in total

View job history, next scheduled time and logs under **System → Background Jobs → Kea IPAM Sync**.

The sync interval can be changed live via the NetBox admin without restarting the worker — edit the `interval` field on the job object.

---

## DNS Integration

When [netbox-dns](https://github.com/peteeckel/netbox-plugin-dns) with IPAMDNSsync is installed:

1. The IPAM sync sets `dns_name` on `IPAddress` objects from the Kea hostname
2. IPAMDNSsync picks up `dns_name` changes via Django signals
3. A/AAAA/PTR records are created automatically (provided matching DNS views + zones exist)

No additional configuration is required — the integration is automatic when both plugins are present.

---

## Custom Links

Add custom links to NetBox models to navigate directly to Kea lease searches.

Replace `<Kea Server ID>` with your server's object ID (visible in the top-right corner of the server detail page as `netbox_kea.server:<ID>`).

### Show DHCP leases for a prefix

**Content type**: `IPAM > Prefix`

**URL**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases{{ object.prefix.version }}/?q={{ object.prefix }}&by=subnet`

### Show DHCP leases for a device/VM interface (by MAC)

**Content types**: `DCIM > Interface`, `Virtualization > Interface`

**DHCPv4 URL**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases4/?q={{ object.mac_address }}&by=hw`

**DHCPv6 URL**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases6/?q={{ object.mac_address }}&by=hw`

### Show DHCP leases for a device/VM (by hostname)

**Content types**: `DCIM > Device`, `Virtualization > Virtual Machine`

**DHCPv4 URL**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases4/?q={{ object.name|lower }}&by=hostname`

**DHCPv6 URL**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases6/?q={{ object.name|lower }}&by=hostname`

You can substitute `{{ object.name|lower }}` with a custom field: `{{ object.cf.<your_field>|lower }}`.

---

## Development

```bash
# Install dev dependencies
uv sync

# Lint
uv run ruff check netbox_kea/
uv run ruff format --check netbox_kea/

# REUSE compliance check
uv run reuse lint

# Format
uv run ruff format netbox_kea/

# Install pre-commit hooks
uv run pre-commit install

# Build wheel (required before integration tests)
uv build

# Run unit tests (no Docker required)
uv run pytest netbox_kea/tests/ --reuse-db -q

# Run integration tests (requires Docker — see tests/test_setup.sh)
./tests/test_setup.sh
uv run pytest tests/ --tracing=retain-on-failure -v --cov=netbox_kea --cov-report=xml
```

See [CHANGELOG](CHANGELOG.md) for version history.

---

## License

[Apache-2.0](LICENSE) — original code by [Devon Mar](https://github.com/devon-mar), fork maintained by [Marcin Zieba](https://github.com/marcinpsk).

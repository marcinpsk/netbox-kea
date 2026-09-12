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

- View Kea daemon status (DHCPv4/DHCPv6 daemons — and the Control Agent on Kea < 3.0)
- Full DHCPv4 and DHCPv6 support
- Search, view, delete and export DHCP leases
- Search for NetBox devices/VMs directly from DHCP leases
- View DHCP subnets from Kea configuration
- REST API and GraphQL support for Server objects

### Additions in this fork

**Host Reservations**
- Full CRUD for DHCPv4 and DHCPv6 reservations via [`host_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#hooks-host-cmds) and [`subnet_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#hooks-subnet-cmds) hooks
- Identifier types: hw-address (v4), DUID (v6), client-id, flex-id, circuit-id, remote-id
- Reservations that reserve no address — an identifier-only host (hostname, options or
  client classes only) and a DHCPv6 host that only delegates prefixes. Both are listed,
  created, edited and deleted by client identifier instead of by IP, and the IPAM sync
  reports them as *skipped*: there is no address to record in NetBox. Their **Lease**
  column matches the client identifier against the leases of the reservation's own
  subnet, there being no reserved address to match on
- Per-reservation DHCP options
- Journal entries on add/edit/delete
- Validated YAML or JSON bulk transfer. Export a Complete Snapshot from one server
  or the combined view, then import it from the matching DHCPv4 or DHCPv6 Reservations page

**Subnet Management**
- Add, edit and delete subnets (requires [`subnet_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#hooks-subnet-cmds) or `config-set`)
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
- Optional separate URLs for the DHCPv4 and DHCPv6 endpoints
- Supports Kea 3.0+ (each daemon exposes its own HTTP control socket) and split v4/v6 deployments

**Global / Cross-Server Views**
- Combined dashboard, lease, reservation, subnet and shared-network views across all servers

**Lease Add / Edit / Bulk Import**
- Add and edit individual leases
- Bulk import leases from CSV

---

## Requirements

- NetBox 4.3 – 4.7
- Kea 3.0+ (recommended) — the plugin connects directly to each daemon's built-in HTTP control socket (`kea-dhcp4` / `kea-dhcp6`). The [Kea Control Agent](https://kea.readthedocs.io/en/latest/arm/agent.html) was deprecated in Kea 2.7 and removed in 3.0; on Kea < 3.0, point the server URL at the Control Agent instead.
- [`lease_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#hooks-lease-cmds) hook library (for lease search and management)
- [`stat_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#hooks-stat-cmds) hook library (for guarded Subnet lease searches unless `lease_query_max_unpaged_leases` is `0`)
- Kea 3.1.5+ for guarded state-filtered Subnet lease searches. On Kea 3.0 through 3.1.4, setting
  `lease_query_max_unpaged_leases` to `0` permits an unbounded compatibility query that NetBox filters locally.
- [`host_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#hooks-host-cmds) hook library (optional, for reservation management — also requires `subnet_cmds` to resolve a reservation's subnet from its CIDR)
- [`subnet_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#hooks-subnet-cmds) hook library (optional, for subnet add/edit/delete, reservation management, and the subnet suggestions on the lease search and reservation forms)

The plugin degrades gracefully when optional hooks are absent — tabs for unavailable features are hidden automatically. Two pages offer the server's configured subnets as suggestions and read them through `subnet_cmds`; without that hook each says so in a banner rather than silently offering nothing:

- **Lease search** keeps working: the Search field offers no subnet suggestions, so type an exact configured subnet CIDR or ID.
- **Add reservation** cannot save, because resolving the entered CIDR to a Kea subnet ID needs `subnet_cmds`. Load the hook first.

---

## Compatibility

| netbox-kea-ng | NetBox | Kea |
|---|---|---|
| 1.x | 4.3 – 4.7 | 3.0+ recommended (2.4+ via Control Agent) |

On Kea 3.0+ the plugin talks directly to each DHCP daemon's HTTP control socket; on Kea < 3.0 it connects through the (now-deprecated) Control Agent. CI tests against **Kea 3.2.0** using the `memfile` lease database.

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
        "lease_query_max_unpaged_leases": 1000,
        "sync_interval_minutes": 5,
        "sync_leases_enabled": True,
        "sync_reservations_enabled": True,
        "sync_prefixes_enabled": True,
        "sync_ip_ranges_enabled": True,
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
| `lease_query_max_unpaged_leases` | `1000` | Reject an unpaged Subnet lease query when its Kea statistics count exceeds this limit. Set to `0` to disable this safety check |
| `stale_ip_cleanup` | `"remove"` | What to do with stale IPs after sync: `"remove"` (delete), `"deprecate"` (set status=deprecated), `"none"` (skip) |
| `sync_interval_minutes` | `5` | How often the background sync job runs (minutes). Also editable via NetBox admin → Jobs |
| `sync_leases_enabled` | `True` | Sync active DHCP leases to NetBox IPAM |
| `sync_reservations_enabled` | `True` | Sync Kea reservations to NetBox IPAM |
| `sync_prefixes_enabled` | `True` | Sync Kea subnets to NetBox IPAM as IP Prefixes |
| `sync_ip_ranges_enabled` | `True` | Sync Kea pools to NetBox IPAM as IP Ranges |
| `sync_max_leases_per_server` | `50000` | Hard cap on leases fetched per server per sync run. Set to `0` for no limit |

Subnet lease searches use `stat-lease4-get` or `stat-lease6-get` before an
unpaged lease command. Kea statistics can reject a query that is already too
large. They cannot prove that an unqualified query is below the limit because
stored expired states are not included in all statistics. Select Active or
Declined to narrow a large query. Other states require an exact IP address or
client identifier search. The guard fails closed when the `stat_cmds` hook is
not available. Set the limit to `0` only when you accept unbounded responses.

---

## Server Configuration

### Single-URL (Control Agent, or a single-protocol daemon)

Point one `Server` URL at a Kea endpoint that serves every enabled protocol — a Control Agent (Kea < 3.0, which fronts both DHCPv4 and DHCPv6), or a single DHCP daemon's HTTP control socket (Kea 3.0+) when the server runs only DHCPv4 *or* only DHCPv6. A dual-stack Kea 3.0+ deployment needs one URL per daemon — see **Dual-URL** below.

| Field | Description |
|---|---|
| `CA / Server URL` (`ca_url`) | URL of the Kea HTTP endpoint — a DHCP daemon control socket (Kea 3.0+) or the Control Agent (Kea < 3.0), e.g. `https://kea.example.com:8000` |
| `DHCPv4` | Enable DHCPv4 lease/reservation/subnet management |
| `DHCPv6` | Enable DHCPv6 lease/reservation/subnet management |
| `CA Username` (`ca_username`) / `CA Password` (`ca_password`) | HTTP Basic Auth credentials (if required) |
| `CA File Path` | Path to a custom CA certificate file for TLS verification |
| `SSL Verification` | Enable/disable TLS certificate verification (enabled by default) |

### Dual-URL (separate v4/v6 processes)

When DHCPv4 and DHCPv6 have separate endpoints — the norm on Kea 3.0+, where each daemon exposes its own HTTP control socket:

| Field | Description |
|---|---|
| `DHCPv4 URL` | URL of the DHCPv4 daemon's HTTP control socket (or its Control Agent on Kea < 3.0) |
| `DHCPv6 URL` | URL of the DHCPv6 daemon's HTTP control socket (or its Control Agent on Kea < 3.0) |

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

### Per-server IPAM sync settings

Each server has optional overrides for the IPAM sync job:

| Field | Default | Description |
|---|---|---|
| `IPAM Sync Enabled` (`sync_enabled`) | `True` | Include this server in the periodic sync job |
| `Sync Leases` (`sync_leases_enabled`) | `True` | Sync active DHCP leases as NetBox IP Addresses |
| `Sync Reservations` (`sync_reservations_enabled`) | `True` | Sync DHCP reservations as NetBox IP Addresses |
| `Sync Prefixes` (`sync_prefixes_enabled`) | `True` | Sync Kea subnets as NetBox IP Prefixes |
| `Sync IP Ranges` (`sync_ip_ranges_enabled`) | `True` | Sync Kea pools as NetBox IP Ranges |
| `Sync VRF` (`sync_vrf`) | None (global routing table) | VRF to assign when syncing Prefixes and IP Ranges. There is no global fallback — leave blank to use the global routing table (no VRF) |
| `Persist configuration` (`persist_config`) | `True` | Automatically save Kea config after each change via `config-write`. Disable when Kea config is managed externally (e.g. Ansible) |

These fields override the global `PLUGINS_CONFIG` values for that specific server.

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

Each server's summary reports `created`, `updated`, `errors`, `prefix_errors`, `conflicts` and `skipped`:

- **skipped** — rows the sync deliberately did not write, chiefly reservations that
  reserve no address. They are not errors and do not fail the job.
- **errors** — rows that failed to sync. Any error fails the job; the per-row reason is
  logged at warning level with the server, IP version, subnet id and identifier *type*
  (never the identifier value, which can carry operator data).
- **conflicts** — addresses already held in NetBox by something other than this Kea
  server, deduplicated per server across the lease and reservation phases. Up to 20 of
  them are named in the summary and the log, so the addresses to look at are visible
  without trawling debug output.

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
uv run ruff check .
uv run ruff format --check .

# REUSE compliance check
uv run reuse lint

# Format
uv run ruff format .

# Install pre-commit hooks
uv run pre-commit install

# Build wheel (required before integration tests)
uv build

# Run unit tests; both variables are required (see AGENTS.md for picking values)
TEST_DB_NAME=test_netbox_kea_local TEST_REDIS_HOST=localhost \
  uv run --native-tls pytest --reuse-db -n auto --maxschedchunk=1 -q

# Run integration tests (requires Docker — see tests/test_setup.sh)
./tests/test_setup.sh
uv run --native-tls pytest -p no:django tests/ --tracing=retain-on-failure -v --cov=netbox_kea --cov-report=xml
```

See [CHANGELOG](CHANGELOG.md) for version history.

---

## License

[Apache-2.0](LICENSE) — original code by [Devon Mar](https://github.com/devon-mar), fork maintained by [Marcin Zieba](https://github.com/marcinpsk).

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
> It is published to PyPI as **`netbox-kea-ng`** and tracked in this repository.
> Upstream changes are periodically merged where applicable.

NetBox plugin for the [Kea DHCP](https://www.isc.org/kea/) server.
View Kea status, leases and subnets directly in NetBox — and navigate back and forth between NetBox devices/VMs and DHCP leases.

## Features

- Uses the Kea management API
- View Kea daemon statuses.
- Supports Kea's DHCPv4 and DHCPv6 servers.
- View, delete, export and search for DHCP leases.
- Search for NetBox devices/VMs directly from DHCP leases.
- View DHCP subnets from Kea's configuration.
- REST API and GraphQL support for managing Server objects.

![Screenshot of DHCP leases](images/leases.png)

## Limitations

- Due to limitations in the Kea management API, pagination is only supported when searching for leases by subnet.
  Additionally, you can only go forwards, not backwards.

- Searching for leases by subnet ID does not support pagination. This may be an expensive operation depending on the subnet size.

- Kea doesn't provide a way to get a list of subnets without an additional hook library.
  Thus, this plugin lists subnets using the `config-get` command. This means that the entire config will be fetched just to get the configured subnets!
  This may be an expensive operation.

## Requirements

- NetBox 4.0, 4.1, 4.2, 4.3, 4.4 or 4.5
- [Kea Control Agent](https://kea.readthedocs.io/en/latest/arm/agent.html)
- [`lease_cmds`](https://kea.readthedocs.io/en/latest/arm/hooks.html#lease-cmds-lease-commands-for-easier-lease-management) hook library

## Compatibility

- This plugin is tested with Kea v2.4.1 with the `memfile` lease database.
  Other versions and lease databases may also work.

## Installation

1. Add `netbox-kea-ng` to `local_requirements.txt`.

2. Enable the plugin in `configuration.py`:

    ```python
    PLUGINS = ["netbox_kea"]
    ```

3. Run `./manage.py migrate`

## Custom Links

You can add custom links to NetBox models to easily search for leases.

Make sure to replace `<Kea Server ID>` in the link URL with the object ID of your Kea server. To find a server's ID, open the page for the server
and look at the top right corner for `netbox_kea.server:<Server ID Here>`.

### Show DHCP leases for a prefix

**Content types**: `IPAM > Prefix`

**Link URL**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases{{ object.prefix.version }}/?q={{ object.prefix }}&by=subnet`

### Show DHCP leases for a device/VM interface (by MAC):

**Content types**: `DCIM > Interface`, `Virtualization > Interface`

**Link URL (DHCPv4)**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases4/?q={{ object.mac_address }}&by=hw`

**Link URL (DHCPv6)**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases6/?q={{ object.mac_address }}&by=hw`

### Show DHCP leases for a device/VM (by name):

**Content types**: `DCIM > Device`, `Virtualization > Virtual Machine`

**Link URL (DHCPv4)**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases4/?q={{ object.name|lower }}&by=hostname`

**Link URL (DHCPv6)**: `https://netbox.example.com/plugins/kea/servers/<Kea Server ID>/leases6/?q={{ object.name|lower }}&by=hostname`

You may also use a custom field by replacing `{{ object.name|lower }}` with `{{ object.cf.<your custom field>|lower }}`.

## Development

See [CONTRIBUTING](CONTRIBUTING.md) and [CHANGELOG](CHANGELOG.md).

```bash
# Install dev dependencies
uv sync

# Lint
uv run ruff check netbox_kea/
uv run ruff format --check netbox_kea/

# REUSE compliance check
uv run reuse lint

# Install pre-commit hooks
uv run pre-commit install

# Build
uv build
```

## License

[Apache-2.0](LICENSE) — original code by [Devon Mar](https://github.com/devon-mar), fork maintained by [Marcin Zieba](https://github.com/marcinpsk).

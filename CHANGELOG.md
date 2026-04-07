# Changelog

All notable changes to netbox-kea-ng will be documented in this file.

This project follows [Conventional Commits](https://www.conventionalcommits.org/) and uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Per-protocol credentials: `dhcp4_username`/`dhcp4_password` and `dhcp6_username`/`dhcp6_password` fields on `Server` allow separate credentials for each DHCP daemon, overriding the CA-level credentials when connecting directly

### Changed

- **Breaking API change**: renamed `Server` fields: `server_url` → `ca_url`, `username` → `ca_username`, `password` → `ca_password` (DB migration 0009 preserves data; REST API field names change)
- `Server.get_client(version=)` now resolves credentials per-protocol: prefers `dhcp4_username`/`dhcp4_password` for v4, `dhcp6_username`/`dhcp6_password` for v6 when set; falls back to `ca_username`/`ca_password`
- `ServerForm` reorganised into named fieldsets: General, Control Agent / Default Connection, DHCPv4, DHCPv6, IPAM Sync
- Forked from [netbox-kea](https://github.com/devon-mar/netbox-kea) by Devon Mar
- Renamed package to `netbox-kea-ng` for independent PyPI publishing
- Migrated tooling to uv + hatchling
- Added REUSE/SPDX compliance
- Added pre-commit hooks (ruff, conventional commits, REUSE)
- Added CodeQL security scanning
- Added Codecov coverage reporting infrastructure
- Added `.editorconfig`
- Updated `dependabot.yml` to weekly cadence with grouping

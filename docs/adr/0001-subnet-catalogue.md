# ADR 0001: Use a Subnet Catalogue

Date: 2026-08-13

Status: Accepted. Amended 2026-09-18 by ADR 0003.

## Context

The repository reads Kea Subnet facts through several paths. Some paths use `subnet4-list` or `subnet6-list`. Other paths parse `config-get`. Views, mutation handlers, synchronization, and the optional DHCP plugin adapter repeat source selection, response validation, Shared Network flattening, and lookup rules.

Kea exposes different authorities for different facts. The subnet command hooks provide live identity facts. `config-get` provides full configuration facts and effective inherited values. A caller that uses one source for every purpose can accept stale identity, lose configuration, or treat incomplete data as authoritative.

Kea Shared Networks do not map to the optional DHCP plugin's current `SharedNetwork` model. The upstream model requires an aggregate Prefix. Kea does not require one.

## Decision

Create a deep `subnet_catalogue` module with a small purpose-named interface:

- `display(server, family)` returns a Complete or Incomplete Catalogue Snapshot for read-only presentation.
- `mutation(server, family)` opens a live mutation scope for exact lookup and creation preparation.
- `for_synchronization(server, family)` returns only a Complete Catalogue Snapshot.

The module owns source selection, validation, normalization, reconciliation, diagnostics, caching, and cache invalidation. Production callers do not parse raw Kea Subnet payloads outside private adapters.

### Source authority

- `subnet4-list` and `subnet6-list` are the Subnet Identity authority.
- `config-get` is the full configuration authority.
  **Amended 2026-09-18 (ADR 0003):** the Server Configuration module owns that read and its parse.
  The Subnet Catalogue consumes a Server Configuration Snapshot. It no longer parses `config-get` itself.
- The identity and configuration observations must agree on canonical CIDR and Kea subnet ID before the module creates a Verified Subnet.
- A source disagreement causes one fresh retry of both observations. The implementation uses the Kea configuration hash when available. Persistent disagreement against stable configuration makes the Catalogue unavailable.

### Snapshot safety

- A Complete Catalogue Snapshot contains verified identity and complete required configuration facts.
- An Identity-Only Catalogue Snapshot can support display, choices, and exact verified lookup. It cannot support synchronization.
- A Configuration-Only Catalogue Snapshot can support display. It cannot authorize identity-sensitive mutation or synchronization.
- Malformed or colliding identities quarantine every participant. Valid unrelated Subnets remain available in an Incomplete Catalogue Snapshot.
- An invalid nested Pool, option, or Shared Network fact does not discard an otherwise valid Subnet. The module omits the invalid fact and marks the nested collection and snapshot incomplete.
- Confirmed absence requires a complete identity observation. Otherwise, exact lookup is blocked instead of reporting absence.
- No caller receives a false empty snapshot when both sources fail or persistent disagreement invalidates the observation.

### Typed facts

The module exposes immutable typed facts for Subnet Identity, Verified Subnet, Configured Subnet, Pool, local Subnet options, effective Subnet Settings, Shared Network membership, diagnostics, and Catalogue Snapshots.

Pool input can use an explicit range or CIDR. The module exposes normalized inclusive endpoints. It does not expose source spelling.

Subnet Settings expose effective values that the repository consumes. They do not claim where Kea declared each value. Unknown configuration remains private.

### Cache and mutation rules

- Interactive display can use a short cache.
  **Amended 2026-09-18 (ADR 0003):** the cache admits an Incomplete Catalogue Snapshot.
  Freshness and completeness are separate properties. The cache generation already guarantees that no
  served snapshot predates the last configuration change. `for_synchronization` still refuses an
  Incomplete Catalogue Snapshot.
- Expired cache data is never served after a fresh read fails.
- Mutation lookup is live and uncached.
- A synchronization run pins one live Complete Catalogue Snapshot.
- A mutation scope invalidates the interactive cache on entry and in `finally` on exit.
- A known pre-change failure can preserve the cache. Any possible change, partial persistence, or ambiguous result invalidates it.
- Existing Subnet mutation can proceed with one exact Verified Subnet.
- New Subnet creation requires a complete live identity observation.
- Automatic Kea subnet ID allocation uses the highest existing ID plus one. It retries once after a concurrent collision and fails when the valid range is exhausted.

Read-modify-write adapters refetch the raw target immediately before mutation. They preserve unknown Kea fields but never expose the raw object through the catalogue interface.

### Optional DHCP plugin integration

The optional integration remains lazy and does not reduce core NetBox compatibility.

- CI tests the optional interface with `netbox-plugin-dhcp==0.1.10` and NetBox 4.6.4.
- Older DHCP plugin releases are unsupported. The adapter does not add compatibility paths for them.
- Kea Shared Network members remain attached directly to the upstream `DHCPServer`.
- NetBox Kea retains Shared Network identity, membership, selectors, settings, and options.
- The adapter uses upstream `DHCPServerInterface` only for real direct-interface facts that it can resolve.
- The adapter does not fabricate aggregate Prefixes or infer Shared Network identity from equal interface assignments.
- A possible upstream DHCP Link model is future work. The current implementation does not predict its interface.

### Migration and verification

The replacement is completed in one change. Production reads of Subnet identity, membership, Pools, options, prefix facts, and settings move to the catalogue. Obsolete helpers and duplicate parsing paths are deleted.

**Amended 2026-09-18 (ADR 0003):** this clause was not completed. The module was built, but seven production
modules continued to parse `config-get` directly, because the Catalogue models only Subnet-shaped facts and
those callers need server-global DHCP Options, Shared Network entities, and option definitions. ADR 0003
records the split that makes the remaining migration possible and schedules it.

Tests cross the catalogue's public interface with a real Server and real `KeaClient`. They stub only `requests.Session.post`. Representative Django view, mutation, synchronization, and optional DHCP plugin tests exercise the real ORM and forms.

## Consequences

The catalogue has a narrow interface and a substantial hidden implementation. Callers gain one source of truth for Subnet facts and cannot select unsafe freshness or completeness policies.

The migration touches many callers. It requires coordinated changes to views, forms, jobs, synchronization, tables, and the optional DHCP plugin adapter.

Incomplete data remains useful for safe display. Type distinctions and purpose-named operations prevent it from authorizing unsafe mutation or reconciliation.

The optional DHCP plugin integration remains lossy for Kea Shared Network grouping. The UI and import result must state that limitation.

## Rejected alternatives

- Use only `config-get`: rejected because configuration facts are not the chosen live Subnet Identity authority.
- Use only subnet command hooks: rejected because they do not provide complete Subnet configuration.
- Let each caller select a source and freshness policy: rejected because it repeats safety rules and permits drift.
- Expose one generic query interface with mode flags: rejected because purpose-named operations provide a smaller and safer interface.
- Treat incomplete configuration as verified identity: rejected because display data must not authorize mutation or synchronization.
- Fabricate an aggregate Prefix for a Kea Shared Network: rejected because it records a false IPAM fact.
- Predict a future upstream DHCP Link model: rejected because the optional integration is sufficient without speculative compatibility code.

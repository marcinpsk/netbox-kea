---
status: accepted
date: 2026-09-18
---

# Separate the Server Configuration authority from the Subnet Catalogue

## Context

ADR 0001 gave the Subnet Catalogue both the Subnet Identity authority and the configuration authority. The
Catalogue models Subnet-shaped facts only. Its `SharedNetworkMembership` type carries one field, a name. It does
not model server-global DHCP Options, Shared Network entities, Shared Network DHCP Options, or option
definitions.

Seven production modules therefore still parse `config-get` directly. Twelve call sites sit outside `kea.py` and
`subnet_catalogue.py`: `jobs.py`, `views/subnets.py` (three), `views/shared_networks.py` (two),
`views/options.py` (two), `views/server.py`, `views/combined.py`, and `views/dhcp_plugin_sync.py`. Four
spellings of the response-envelope guard exist, with three different failure modes for one condition.

The per-server Subnet tab and the combined Subnet view render the same rows from two implementations. One is a
raw parse and one is the Catalogue. They can disagree.

Some Catalogue facts are also unreachable for some Servers. `find_by_id`, `find_by_cidr` and `subnet_choices`
search Verified Subnets only. On a Server without the `subnet_cmds` hook every Subnet is a Configured Subnet, so
those accessors return nothing where the raw parse works.

## Decision

Create a `server_configuration` module. It is the `config-get` authority for one Server and address family. It is
read only.

The Subnet Catalogue keeps the Subnet Identity authority, reconciliation, the Verified and Configured
distinction, Subnet mutation, and its own snapshot cache. It stops parsing `config-get`. It consumes a Server
Configuration Snapshot.

### Interface

Two purpose-named reads. There is no freshness argument. ADR 0001 rejected mode flags and that rejection stands.

```text
display(server, family)           -> ServerConfigurationSnapshot
for_verification(server, family)  -> ServerConfigurationSnapshot
invalidate(server, family)        -> None
```

- `display` serves interactive presentation from the cache.
- `for_verification` is live and uncached. The Subnet Catalogue uses it for identity reconciliation, for
  `for_synchronization`, and inside a mutation scope.
- `invalidate` rotates the cache generation. Both modules key their cache entries on that generation, so one
  rotation clears both. `Server.get_client` wires `on_config_change` to it.

### Typed facts

```text
ServerConfigurationSnapshot
    server_id: int
    family: int
    observed_at: datetime                      # timezone aware
    subnets: tuple[DeclaredSubnet, ...]
    shared_networks: tuple[SharedNetwork, ...]
    global_options: tuple[DHCPOption, ...]
    option_definitions: tuple[OptionDefinition, ...]
    diagnostics: tuple[Diagnostic, ...]
    configuration_hash: str | None
    available: bool
    complete: bool

DeclaredSubnet
    declared_cidr: str                         # canonical form of the Kea `subnet` value
    declared_subnet_id: int | None
    configuration: SubnetConfiguration
    shared_network_name: str | None

SharedNetwork
    name: str
    description: str | None
    interface: str | None
    relay_addresses: tuple[IPAddressValue, ...]
    options: tuple[DHCPOption, ...]
    member_cidrs: tuple[str, ...]

OptionDefinition
    code: int
    name: str
    space: str
    type: str
    array: bool
    encapsulate: str | None
    record_types: tuple[str, ...]
```

`SubnetConfiguration`, `Pool`, `SubnetSettings` and `Diagnostic` move from `subnet_catalogue` to
`server_configuration`, because they describe parsed configuration. The Subnet Catalogue re-exports them, so its
callers keep one import. `DHCPOption` stays in `dhcp_options`. Both modules use it.

A `DeclaredSubnet` states what Kea declared. It does not assert Subnet Identity. Only the Subnet Catalogue
creates a Verified Subnet, and only after a declared fact agrees with a `subnet4-list` or `subnet6-list`
observation.

A Shared Network with no member Subnets is representable. `SharedNetworkMembership` cannot express one, which is
why `views/subnets.py` reads `config-get` for Shared Network choices today.

### Snapshot safety

- `available` is false when the read failed. The Snapshot then carries diagnostics and no facts.
- `complete` is false when any fact failed to parse. Valid facts survive. One invalid Pool, DHCP Option or
  Shared Network entry does not discard the rest.
- `display` never raises. A caller reads `available`, `complete` and `diagnostics` to decide what to show.
- The Subnet Catalogue folds the Snapshot diagnostics into its own and derives `configuration_complete` from
  `complete`.

### Cache rules

- `server_configuration` caches its Snapshot. The Subnet Catalogue caches its reconciled Catalogue Snapshot.
  Both use the same generation.
- Both caches admit an incomplete result. See the ADR 0001 amendment for why.
- `for_verification` bypasses both caches.

### Presentation

A view shows an error when a Snapshot is unavailable. It shows a warning that lists the diagnostics when a
Snapshot is incomplete but usable. `Diagnostic` gains no severity field. The `available`, `complete`,
`identity_complete`, `configuration_complete` and `consistent` flags already carry that distinction.

### Scope

The module is read only. Shared Network, DHCP Option and option definition writes stay on the existing `kea.py`
read-modify-write path. A mutation interface is future work. This ADR does not predict its shape.

`mappers/kea_to_dhcp.py` keeps its own parse. It needs 47 settings keys at three scopes, client class
definitions, and per-Pool DHCP Options. The scope here is what this plugin's own user interface manages.
Widening it to the union of both demands would make the module as wide as `config-get`, which is the first
rejected alternative below.

### Migration

The order is fixed. The foundation lands first and changes no caller.

1. The module, the Catalogue rewiring, `identity` on Configured Subnet, accessors that search both collections,
   incomplete caching, and the generation move.
2. `views/subnets.py`, with the shared row builder moved to `views/_base.py`.
3. `views/shared_networks.py` and the combined Shared Network view.
4. `views/options.py` and `views/server.py`.
5. The `jobs.py` Subnet phases.
6. `utilities.fetch_subnet_choices` retired.
7. A check that fails on Kea wire-format string literals outside the owning modules.

Step 1 is verified by the existing `subnet_catalogue` tests staying green. Every step crosses public interfaces
with a real Server and a real `KeaClient`, and stubs only `requests.Session.post`.

## Consequences

One module parses `config-get`. The Subnet Catalogue gets smaller and keeps one job.

Shared Networks, server-global DHCP Options and option definitions become typed facts. Views stop hand-parsing
them.

A Server without the `subnet_cmds` hook keeps working, because the Catalogue accessors also search Configured
Subnets.

A Server whose configuration holds one invalid entry keeps its cache. Today it loses the cache, and every render
re-reads Kea.

The import cycle between `models` and the domain modules moves to `server_configuration`. This ADR does not fix
it.

Two modules now share one `config-get` read per cache generation instead of issuing two.

## Rejected alternatives

- Widen the Subnet Catalogue to the whole `Dhcp4` and `Dhcp6` configuration: rejected because its name and its
  CONTEXT.md definition describe Subnets. The concept would stop being true.
- A shared private parser that both modules import: rejected because it is neither a public interface nor a
  domain concept, so it has no owner and no name.
- Let the Server Configuration module wrap the Subnet Catalogue: rejected because it parses one payload twice
  and keeps two independent caches.
- One read operation with a freshness argument: rejected for the reason ADR 0001 records. Purpose-named
  operations give a smaller and safer interface.
- A severity field on `Diagnostic`: rejected because the completeness flags already separate unusable from
  degraded, and every existing diagnostic site would need a judgement.
- Model everything `mappers/kea_to_dhcp.py` needs: rejected because it is the first rejected alternative under
  another name.

# Kea Shared Networks and NetBox DHCP

Research date: 2026-08-13.

## Result

The optional integration targets sys4's [`netbox-plugin-dhcp`](https://github.com/sys4/netbox-plugin-dhcp), whose Django app is `netbox_dhcp`. The latest release without a pre-release suffix is [0.1.10](https://pypi.org/project/netbox-plugin-dhcp/0.1.10/), published on 2026-08-04. Upstream still classifies the project as Beta. Release 0.1.10 requires Python 3.12 or later and supports NetBox 4.5.x and 4.6.x, as stated by its immutable [package metadata](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/pyproject.toml#L1-L17) and [compatibility matrix](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/COMPATIBILITY.md#L1-L8).

The plugin has a `SharedNetwork` model, but its meaning is not equal to a Kea Shared Network. The plugin requires one aggregate NetBox Prefix and requires every member Subnet prefix to be inside that Prefix ([plugin models](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/subnet.py#L134-L209)). Kea uses a Shared Network to state that multiple logical subnets use one physical link or one relay selection domain. Its documented examples include non-contiguous member prefixes ([Kea DHCPv4 Shared Networks](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp4-srv.html#shared-networks-in-dhcpv4)). Importing that Kea group into the current plugin model would therefore require a fabricated aggregate Prefix or would fail validation.

The Subnet Catalogue should keep Shared Network identity and membership in scope. It should not fabricate a DHCP-plugin `SharedNetwork`. Full Shared Network settings should remain behind the existing Kea configuration adapter until the upstream model can represent a prefixless, family-specific subnet selection group.

## Current project decision

An upstream model change is outside the current implementation scope. The optional integration will use `DHCPServerInterface` where a Kea direct-interface selector resolves to a real NetBox device or virtual-machine Interface. NetBox Kea remains the authority for Kea Shared Network identity, membership, selectors, settings, and options.

The adapter will continue to attach member Subnets directly to the DHCP plugin's `DHCPServer`. It will report Shared Network import as deferred. It will not fabricate an aggregate Prefix, infer a DHCP Link from equal server-interface assignments, or copy effective group settings into member Subnets as local overrides.

The integration remains lazy. The core plugin does not require `netbox-plugin-dhcp`. CI will test the optional interface against an exact 0.1.10 pin on NetBox 4.6.4. The adapter does not provide compatibility branches for older DHCP plugin releases. A future upstream DHCP Link proposal can use the research below, but no current code will predict its interface.

## Upstream plugin status

The tagged API exposes `DHCPCluster`, `DHCPServer`, `DHCPServerInterface`, `SharedNetwork`, `Subnet`, `Pool`, `PDPool`, `HostReservation`, `ClientClass`, `OptionDefinition`, and `Option` resources ([tagged API routes](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/api/urls.py)). The main hierarchy is:

```text
DHCPCluster
  -> DHCPServer
       -> SharedNetwork -> Subnet -> Pool / PDPool / HostReservation
       -> Subnet         -> Pool / PDPool / HostReservation
```

A Subnet must have exactly one parent, either a DHCP Server or a Shared Network. Its `subnet_id` is globally unique. A Shared Network has one DHCP Server, one required NetBox Prefix, settings, client classes, and options ([Subnet model](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/subnet.py#L44-L207), [Shared Network model](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/shared_network.py#L42-L150)). The Shared Network family is derived from its required Prefix. The base model mixin also makes each model's name globally unique ([model mixins](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/mixins.py#L21-L42)).

`DHCPCluster` groups DHCP Server records and has only a name, description, and status. It does not group Subnets or define subnet selection ([DHCP Cluster model](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/dhcp_cluster.py)). Client classes can restrict access to Shared Networks, Subnets, and Pools, but they do not replace the relationship that identifies subnets on one link ([Shared Network model](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/shared_network.py#L42-L150), [Subnet model](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/subnet.py#L44-L188)).

No recent release added a second Kea-like grouping model. The [0.1.8 to 0.1.10 comparison](https://github.com/sys4/netbox-plugin-dhcp/compare/0.1.8...0.1.10) shows no semantic change to the Shared Network, Subnet, or DHCP Cluster models. Release 0.1.10 fixed forms and serializers, and retained the same model constraints.

### Why DHCPServerInterface is not the missing group

`DHCPServerInterface` represents one interface of the DHCP Server host. Each row belongs to one `DHCPServer` and refers to exactly one NetBox device Interface or virtual-machine Interface ([0.1.10 model](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/dhcp_server.py#L38-L123)). The plugin creates and removes these wrapper rows as interfaces are assigned to or removed from a DHCP Server ([signals](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/signals/dhcp_server.py)).

Subnet and Shared Network inherit a `server_interfaces` relationship from `NetworkModelMixin`. The same implementation stores relay addresses and DHCPv6 interface ID separately on those network objects ([network model fields](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/mixins.py#L350-L375)). This makes `DHCPServerInterface` one useful selector for directly connected traffic. It is not the client attachment or relay-selected link itself.

It lacks the facts required for a universal DHCP Link:

- It has no address family, group name, routing-domain identity, or stable vendor source key.
- It does not own Subnet membership or express that several Subnets form one selection domain.
- It cannot represent a remote client link selected by relay address or link address when the DHCP Server has no interface on that link.
- It does not own group settings, options, client restrictions, inheritance, or allocation policy.
- One server interface can carry or reach more than one client link, so equal interface assignment is not sufficient group identity.

Keep `DHCPServerInterface` and reuse it as an optional direct-interface selector on `DHCPLink`. A DHCP Link also needs structured relay selectors for remote traffic. Neither selector should replace the DHCP Link identity or membership relationship.

## Kea Shared Network semantics

Kea defines a Shared Network for multiple logical IP subnets on the same physical link. A client attached to it can receive a lease from pools in any member Subnet. Kea first tries one Subnet, then can try another member when the first cannot allocate a lease. It can also choose another member because of a client hint or reservation ([DHCPv4 Shared Networks](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp4-srv.html#shared-networks-in-dhcpv4), [DHCPv6 Shared Networks](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp6-srv.html#shared-networks-in-dhcpv6)). When subnet selection finds one member, Kea selects the whole Shared Network ([DHCPv4 subnet selection](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp4-srv.html#how-the-dhcpv4-server-selects-a-subnet-for-the-client)).

The group is also a configuration scope. Settings and options declared there apply to all members unless a Subnet overrides them. This includes values such as lease lifetimes and options ([DHCPv4 inheritance](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp4-srv.html#shared-networks-in-dhcpv4), [DHCPv6 inheritance](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp6-srv.html#shared-networks-in-dhcpv6)).

Direct traffic must reach all members through the same interface. Relayed traffic should use the same relay address for all members. Kea recommends placing interface or relay selectors at Shared Network scope so members inherit them ([DHCPv4 local and relayed traffic](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp4-srv.html#local-and-relayed-traffic-in-shared-networks), [DHCPv6 local and relayed traffic](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp6-srv.html#local-and-relayed-traffic-in-shared-networks)).

Each Shared Network name must be unique within the daemon configuration. Kea uses the name for logs and internal identity ([DHCPv4 Shared Networks](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp4-srv.html#shared-networks-in-dhcpv4), [DHCPv6 Shared Networks](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp6-srv.html#shared-networks-in-dhcpv6)). DHCPv4 and DHCPv6 are separate configuration scopes, so an importer must include the Server and address family in its key.

## Concept mapping

| Kea fact | NetBox DHCP 0.1.10 | Fit |
|---|---|---|
| Server and address family | `DHCPServer`; family is inferred from Prefixes | Partial. One `DHCPServer` spans both families. |
| Shared Network name | Globally unique model `name` | Partial. Kea identity is scoped to Server and family. |
| Member Subnets | `Subnet.shared_network` foreign key | Good relationship shape. |
| Same link or relay selection domain | Required `SharedNetwork.prefix`; child containment validation | Not equivalent. A common link does not imply one aggregate address prefix. |
| Direct interface selector | `server_interfaces` relationship and `interface_id` string from `NetworkModelMixin` | Partial. A raw daemon interface name cannot always resolve to a NetBox Interface. |
| Relay address list | `relay` character field from `NetworkModelMixin` | Partial. Kea uses a structured list of addresses. |
| Shared settings and options | Settings mixins and an `Option` generic relation | Structurally close. The effective inheritance interface is not explicit. |
| Allocation fallback across member Subnets | No separate operational field | The parent relationship implies a group, but the plugin does not model or execute Kea allocation logic. |
| DHCP server redundancy | `DHCPCluster` | Different concept. It groups servers, not Subnets. |

The plugin's required Prefix makes its Shared Network closer to an address aggregate that contains child Subnets. Kea's object is a link and selection group. Kea's official DHCPv4 example groups unrelated address blocks on one interface, which demonstrates why Prefix containment is not a valid general rule ([Kea DHCPv4 example](https://kea.readthedocs.io/en/kea-3.2.0/arm/dhcp4-srv.html#shared-networks-in-dhcpv4), [plugin containment rule](https://github.com/sys4/netbox-plugin-dhcp/blob/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models/subnet.py#L188-L207)).

Flattening a Kea Shared Network loses its name, membership, allocation fallback domain, shared selectors, and the origin of inherited settings and options. Coercing it into the current plugin model has a different risk: the importer must create a broad aggregate Prefix that can misstate IPAM ownership and routing. Global name uniqueness can also force a renamed object when two Servers or two families use the same valid Kea name.

## This repository and newer-plugin testing

This project declares only `requests` and `netaddr` as runtime dependencies. It does not declare or pin `netbox-plugin-dhcp` in its project or development dependencies ([project metadata](../../pyproject.toml)). The unit-test job installs NetBox 4.6.4, enables only `netbox_kea`, and does not install `netbox-plugin-dhcp` ([unit-test workflow](../../.github/workflows/ci.yml)). The Docker compatibility matrix includes NetBox 4.3, which is below the DHCP plugin's 4.5 floor ([compatibility workflow](../../.github/workflows/ci.yml)).

The adapter loads plugin models through Django's app registry. It expects the exact current ORM names and relationships for DHCP Server, Subnet, Pool, Host Reservation, Option Definition, Client Class, and Option ([adapter](../../netbox_kea/integrations/dhcp_plugin.py)). It uses a local `KeaDhcpLink` because Kea Subnet IDs are scoped to Server and family while the plugin makes `Subnet.subnet_id` global ([link model](../../netbox_kea/models.py)). It explicitly flattens Shared Network members onto `DHCPServer` because the upstream model requires a Prefix ([adapter scope](../../netbox_kea/integrations/dhcp_plugin.py#L3-L41)).

The real-model import tests skip when `netbox_dhcp` is not installed ([integration tests](../../netbox_kea/tests/test_views_dhcp_plugin.py)). Therefore, there is no existing dependency pin to update. A newer plugin can be tested by adding an exact test-only pin and a dedicated CI leg that:

1. Uses Python 3.12 or later and NetBox 4.6.4.
2. Installs `netbox-plugin-dhcp==0.1.10`.
3. Enables both `netbox_kea` and `netbox_dhcp`.
4. Runs migrations against an isolated test database.
5. Runs the real-model DHCP-plugin tests and the normal unit suite.

This must remain a separate optional leg. An unconditional dependency would remove this project's Python 3.10 and 3.11 support and would conflict with its NetBox 4.3 compatibility leg. The upstream plugin is Beta, so the CI dependency should use an exact version. Installing the plugin in the baseline unit job can also change global NetBox query behavior, so it should not silently replace the query-count environment.

The integration should stay lazy and target only `netbox-plugin-dhcp==0.1.10`. It should not add compatibility branches for older DHCP-plugin releases. The normal NetBox 4.3 leg must continue without `netbox_dhcp`, so this optional integration does not reduce the core plugin's NetBox support window.

Concrete compatibility risks are the plugin's private ORM shape, globally unique names and Subnet IDs, the exact option-definition constraints, nullable inheritance fields, and model validation during `save()`. The tagged 0.1.10 models still match the adapter's main assumptions, but only the skipped real-model tests can verify this claim end to end ([tagged models](https://github.com/sys4/netbox-plugin-dhcp/tree/3248f32e604ac324da973638deb6fc9701781353/netbox_dhcp/models), [local real-model tests](../../netbox_kea/tests/test_views_dhcp_plugin.py)).

## Cross-vendor DHCP link model

[RFC 7969 defines a shared subnet](https://www.rfc-editor.org/rfc/rfc7969.html#section-2) as two or more same-family Subnets on one link. It also maps the same network fact to the Unix term “shared subnet,” the Windows term “multinet,” and the Microsoft DHCP term “Superscope.” [RFC 3527](https://www.rfc-editor.org/rfc/rfc3527.html#section-3) states that a DHCPv4 link selector can identify one Subnet while all related Subnets on the same link remain allocation candidates. These definitions support **DHCP Link** as the vendor-neutral domain term. They do not define an address aggregate.

| Implementation | Vendor object and identity | Membership and selection scope | Configuration inheritance | Aggregate Prefix | Allocation behavior |
|---|---|---|---|---|---|
| Microsoft DHCP | A persistent IPv4 **Superscope** is hosted by one DHCP Server and is uniquely identified by `SuperScopeName` ([identity](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dhcpm/035c335d-267c-4a6a-94dc-842e9dfddf0e)). | It groups multiple logical networks on one physical segment or on a remote multinet behind a relay ([overview](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dhcpm/4b3dafe4-70e5-4085-969e-4bb402d9c68b)). The member scopes do not need to form one contiguous prefix. | No. Option precedence is Reservation, selected Scope, then Server. There is no Superscope option scope ([option selection](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dhcpm/c005b785-bb20-45ca-a077-260ca2b0989e), [option scope types](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dhcpm/34075f91-50f1-4216-b61f-44f0f5ab3679)). | Not required. Identity is a name and ID, and membership refers to ordinary Scope records. | The Server selects the normal Scope first. If it is exhausted, the Server can allocate from another member with the same Superscope ID ([allocation rule](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dhcpm/c005b785-bb20-45ca-a077-260ca2b0989e)). |
| ISC DHCP | A named **`shared-network`** configuration block is the identity ([ISC DHCP 4.4 configuration reference](https://kb.isc.org/docs/isc-dhcp-44-manual-pages-dhcpdconf)). | It contains the independent Subnet declarations that use one physical network. Relay link selection identifies this shared selection scope ([relay option reference](https://kb.isc.org/docs/isc-dhcp-44-manual-pages-dhcp-options)). | Yes. Shared-network parameters apply to members unless a more specific declaration overrides them ([configuration reference](https://kb.isc.org/docs/isc-dhcp-44-manual-pages-dhcpdconf)). | Not required. The block has a name and member Subnet declarations, not a covering prefix. | The Server collects dynamic addresses from the member Subnets into a common pool. Pool permit rules can still restrict a client ([configuration reference](https://kb.isc.org/docs/isc-dhcp-44-manual-pages-dhcpdconf)). |
| Cisco Prime Network Registrar | DHCPv4 has an inferred **Network**. Same-network Scopes join implicitly. An unrelated secondary Scope joins through its `primary-subnet`. DHCPv6 has an explicit named **Link** containing Prefixes ([DHCPv4 Network model](https://www.cisco.com/c/en/us/td/docs/net_mgmt/prime/network_registrar/11-0/dhcp/guide/DHCP_Guide/DHCP_Guide_chapter_00.html), [DHCPv6 Link model](https://www.cisco.com/c/en/us/td/docs/net_mgmt/prime/network_registrar/11-0/dhcp/guide/DHCP_Guide/DHCP_Guide_chapter_0100.html)). | The receiving interface or `giaddr` selects the IPv4 Network. A secondary Scope can have an address and mask unrelated to the primary Scope. A DHCPv6 Link groups different Prefixes on one link ([DHCPv4 model](https://www.cisco.com/c/en/us/td/docs/net_mgmt/prime/network_registrar/11-0/dhcp/guide/DHCP_Guide/DHCP_Guide_chapter_00.html)). | IPv4 Networks do not own a policy layer. Reusable policies apply to Scopes. DHCPv6 Links add a policy layer between Prefix and system policy ([policy hierarchy](https://www.cisco.com/c/en/us/td/docs/net_mgmt/prime/network_registrar/11-4/dhcp/guide/DHCP_Guide/DHCP_Guide_chapter_0110.html), [DHCPv6 Link model](https://www.cisco.com/c/en/us/td/docs/net_mgmt/prime/network_registrar/11-0/dhcp/guide/DHCP_Guide/DHCP_Guide_chapter_0100.html)). | Not required. `primary-subnet` is a selection anchor, not a covering prefix. A DHCPv6 Link is also separate from its Prefix members. | DHCPv4 allocates from eligible Scopes by round robin or configured allocation priority ([CNR 11.4 DHCP guide](https://www.cisco.com/c/en/us/td/docs/net_mgmt/prime/network_registrar/11-4/dhcp/guide/DHCP_Guide.pdf)). |
| Infoblox NIOS | A family-specific **Shared Network** is a first-class object in a Network View. The Network View is the routing-domain scope ([NIOS object glossary](https://docs.infoblox.com/space/nios90/918683662), [supported DHCP object types](https://docs.infoblox.com/space/NCR8/23068762/Table%2B1.5%2BSupported%2BDHCP%2BObject%2BTypes)). | It groups two or more network objects on one network segment. A Shared Network can exist without a parent network container. Therefore, independent member Prefixes do not require one container ([object glossary](https://docs.infoblox.com/space/nios90/918683662), [option inheritance](https://docs.infoblox.com/space/nios91x/2292580777/About%2BIPv4%2BDHCP%2BOptions)). | Yes. A Shared Network can inherit Grid or Member values and override them. Member networks can then inherit its options ([DHCP inheritance](https://docs.infoblox.com/space/NAG8/22251991), [IPv4 DHCP options](https://docs.infoblox.com/space/nios91x/2292580777/About%2BIPv4%2BDHCP%2BOptions)). | Not required. A parent network container is optional. | Available addresses in member Subnets enter one common allocation pool ([object glossary](https://docs.infoblox.com/space/nios90/918683662)). |

### Universal terminology and invariants

Use **DHCP Link** for the domain object and `DHCPLink` for the model. Define it as a DHCP selection and allocation domain that associates same-family Subnets with one client attachment link or one relay-selected link. “Subnet Selection Group” is an acceptable explanatory phrase, but it is not the primary model name. “Allocation Domain” is too narrow because selection and inherited configuration are also relevant. “Shared Network” and “Superscope” should remain vendor aliases.

The universal model should have these invariants:

- Scope identity by DHCP Server or configuration authority, routing domain, address family, and stable name or source key. CNR DHCPv4 has an inferred Network, so its adapter must derive a stable source key from the primary Scope relationship.
- Require one address family per DHCP Link. Do not combine an IPv4 group and an IPv6 group because vendor objects and selection paths are family-specific.
- Allow each Subnet to have zero or one DHCP Link membership in one Server and family. Permit groups with zero or one member for staged configuration, even though two members give the object its operational purpose.
- Require members to describe one client attachment or relay selection domain. Do not require CIDR adjacency, common supernet containment, or an aggregate Prefix.
- Keep attachment selectors, such as Server interface, relay address, link address, and interface ID, as structured data. They identify the link but do not identify the address aggregate.
- Treat group-level settings and options as an optional capability. ISC DHCP, Kea, Infoblox, and CNR DHCPv6 provide a group policy layer. Microsoft Superscopes and CNR DHCPv4 Networks do not.
- Treat allocation policy as implementation data. Common-pool, exhaustion fallback, round robin, and allocation priority are different behaviors. Membership must not imply one algorithm.
- If the group refers to an IPAM aggregate, store it as an optional `aggregate_prefix` relationship. Apply containment validation only when a deployment explicitly uses that aggregate as a constraint.

### Aggregate model evaluation

The proposal to separate the current address aggregate from the universal DHCP object identifies the correct semantic split. If the existing object remains as a distinct IPAM concept, use **Aggregate Prefix**, not **Shared Prefix**. “Shared” is already a DHCP topology term, and “Shared Prefix” can suggest that the prefix itself is shared between links.

The preferred upstream change does not need two parent models. Evolve the existing `SharedNetwork` into `DHCPLink`. Rename its required `prefix` field to optional `aggregate_prefix`, and preserve its existing membership, settings, options, and client-class relationships. Existing rows migrate to DHCP Links with `aggregate_prefix` populated. New prefixless rows can represent Kea, ISC DHCP, Microsoft, CNR, and Infoblox without synthetic IPAM data.

If compatibility requires a distinct `AggregatePrefix` model, it must be an optional IPAM classification related to `DHCPLink`. It must not own DHCP settings, options, selectors, or the exclusive Subnet parent relationship. Otherwise, a Subnet cannot represent both its address aggregate and its operational DHCP Link without another lossy exclusive choice. NetBox Prefix already stores most aggregate facts, so a separate model is useful only if it has additional aggregate-specific metadata.

## Maintainer-facing proposal

Suggested neutral term: **DHCP Link**. It means a family-specific group of DHCP Subnets that the Server selects as one attachment domain. “Subnet Selection Group” can remain an explanatory phrase. The existing `SharedNetwork` model and API name can remain during the first change to limit migration cost. Its documentation and interface can use the neutral meaning and list “shared network,” “multinet,” and “Superscope” as implementation terms.

### Must-have

- Make `SharedNetwork.prefix` optional. Treat it as an optional IPAM aggregate, not group identity.
- Add a required `family` field. Backfill it from the existing Prefix before making the field non-null.
- Replace global name identity with uniqueness on `(dhcp_server, family, name)`. Use the same tuple as the import and upsert key.
- Keep the existing one-group-per-Subnet relationship. Require every child Subnet to match the group's family. Apply Prefix containment only when the optional aggregate Prefix exists.
- Preserve group-level settings, options, client-class restrictions, direct-interface selection, relay addresses, and DHCPv6 interface ID. Use structured relay addresses. Keep a raw interface name when no NetBox Interface can be resolved.
- Define effective inheritance as `DHCPServer -> DHCP Link -> Subnet -> Pool or Reservation`. Keep nullable child values as inheritance markers. Expose both declared values and effective values through a stable resolver or serializer contract.
- Update REST, GraphQL, forms, CSV import, filters, and nested serializers so `family` is required and `prefix` is optional. Keep the existing endpoint during the compatibility period.
- Add a data migration in this order: add nullable family, backfill from Prefix, change Prefix to nullable, add family/member validation, replace the name constraint, then make family non-null. Reject ambiguous existing rows instead of guessing.

### Optional later additions

- Add an explicit selector record with types such as server interface, relay address, and link address. This is cleaner than expanding string fields when more DHCP implementations are added.
- Add source-binding metadata when integrations need identity that differs from `(dhcp_server, family, name)`.
- Add a group kind only after a second real grouping behavior appears. Do not add a Kea-specific kind now.
- Add an effective-configuration API view that reports each value and the scope that supplied it.

## Recommendation for netbox-kea now

Continue to import DHCP Server, Subnet, Pool, Host Reservation, local option, and local setting facts that the current plugin can represent. Keep reporting Kea Shared Network grouping as deferred. Do not create a fabricated aggregate Prefix. Do not copy group-level options or settings into every Subnet because that erases inheritance and can create false local overrides.

The Subnet Catalogue should retain Shared Network identity and membership only. Full Shared Network configuration, including selectors and group-level inherited values, should stay in a separate Kea adapter. After upstream supports a prefixless, family-specific group, the DHCP-plugin import can create the group by `(DHCPServer, family, name)`, attach catalogue Subnets, and apply parent-aware settings and options.

# Changelog

All notable changes to netbox-kea-ng are documented in this file.

This project follows [Conventional Commits](https://www.conventionalcommits.org/) and
uses [Semantic Versioning](https://semver.org/). python-semantic-release writes every
entry below from the commits on `main` and inserts each new release directly under the
marker, so nothing here is edited by hand.

Entries for v1.0.4 and earlier are empty. They are the release tags of
[devon-mar/netbox-kea](https://github.com/devon-mar/netbox-kea), which this project
forked, and predate its conventional-commit history.

<!-- version list -->

## v3.1.0 (2026-10-10)


## v3.0.0 (2026-10-10)

### Bug Fixes

- A lease does not rewrite the MAC description of a reserved host
  ([`35c2241`](https://github.com/marcinpsk/netbox-kea/commit/35c22419abfc904f3f1babd4e258f6b523d56635))

- Check a created Lease against its request and show a Reservation of the other kind
  ([`cb91d86`](https://github.com/marcinpsk/netbox-kea/commit/cb91d86e82515a7b3eddffb97766415dc42398f1))

- Fail only the claim rows of a Reservation without a known published name
  ([`9f17762`](https://github.com/marcinpsk/netbox-kea/commit/9f177623bc19f47b7e5c840b11fb8e1c931f4e80))

- Import an addressless Reservation without a known published name
  ([`5fc1584`](https://github.com/marcinpsk/netbox-kea/commit/5fc1584b1d60c72b5126658ac914d083e9623b82))

- Keep proxy values out of the setup script trace
  ([`83ba776`](https://github.com/marcinpsk/netbox-kea/commit/83ba776e93b514f613d51f47565eb15ec901965b))

- Keep Reservation claims free of Kea reads
  ([`eb6af05`](https://github.com/marcinpsk/netbox-kea/commit/eb6af05c4b48c118e5bfa830a1d77ccfed512466))

- Lock the legacy IP during repair and gate lease Sync on its IPAM permissions
  ([`76dcaba`](https://github.com/marcinpsk/netbox-kea/commit/76dcaba615f7be3fa35d5006b85d12e19bbe2652))

- Name every source of the qualifying suffix in the Reservation hostname help
  ([`3ba83c8`](https://github.com/marcinpsk/netbox-kea/commit/3ba83c83d482bcbadba2520fdf80572fef95060f))

- Name the changed lease facts as a list with "and"
  ([`156626b`](https://github.com/marcinpsk/netbox-kea/commit/156626b5479e646097321c58ec9e2cb6b3caacce))

- Name the file line of a lease CSV row in import errors
  ([`e5e21a3`](https://github.com/marcinpsk/netbox-kea/commit/e5e21a37f65ca953c0e46c93c7315ad69a120c04))

- Open the edit form of an addressless Reservation under a Pool suffix
  ([`1eb3cff`](https://github.com/marcinpsk/netbox-kea/commit/1eb3cfffd541de73efb3408277e24a342d8f1f55))

- Open the Reservation edit form when the DDNS suffix is unknown
  ([`8cd906b`](https://github.com/marcinpsk/netbox-kea/commit/8cd906b40fd26c34a99380e21ec012dba825d1d6))

- Prefill the published name of a lease in its Reserve link
  ([`487579d`](https://github.com/marcinpsk/netbox-kea/commit/487579d81a7a8062734dc3c224d918dfb516d6eb))

- Refuse a DHCP plugin import without unconstrained DHCP plugin grants
  ([`8404997`](https://github.com/marcinpsk/netbox-kea/commit/84049978023714797fdd10b4c588cda737c2919f))

- Reject two statistics rows for one Subnet
  ([`9da05b2`](https://github.com/marcinpsk/netbox-kea/commit/9da05b220f79f671190d3047e99e5e2171a8fd50))

- Select the Leases tab on the lease import page and show a full example file
  ([`aa2c909`](https://github.com/marcinpsk/netbox-kea/commit/aa2c90978e2d1aea38491349f7066d269729fd02))

- Show an unknown qualifying suffix on the hostname field
  ([`496d19a`](https://github.com/marcinpsk/netbox-kea/commit/496d19a7760bffedd897718ec05d8f2814c93327))

- Sync the published name from both the lease and the Reservation source
  ([`a72ff57`](https://github.com/marcinpsk/netbox-kea/commit/a72ff57681deb8a8664c7e90904669a88a25a9b9))

- Take a Pool DDNS qualifying suffix from the Pool of the address
  ([`4559ecb`](https://github.com/marcinpsk/netbox-kea/commit/4559ecbebeebf13217a5b63aa8018aee76396d8a))

- Warn when Kea refuses the Subnet utilization read
  ([`2d6b5ce`](https://github.com/marcinpsk/netbox-kea/commit/2d6b5ce4ae0fe180ad92e89474afd0cf8822269b))

- **jobs**: Write the traceback of an unhandled server error to the worker log
  ([`6923b58`](https://github.com/marcinpsk/netbox-kea/commit/6923b584ace271ddc4a600feb301c8a2177a960a))

- **leases**: Compare the hostname of a created lease without case
  ([`ab653a7`](https://github.com/marcinpsk/netbox-kea/commit/ab653a72c63d4d1985bbd0c818ef9cc5ca8c293d))

- **sync**: Classify the legacy link of a delegated prefix that is not current
  ([`eb771ed`](https://github.com/marcinpsk/netbox-kea/commit/eb771ed91b64917e88f091604858a69155464dbf))

### Build System

- **deps**: Bump pydantic from 2.12.5 to 2.13.5
  ([`b65ec3e`](https://github.com/marcinpsk/netbox-kea/commit/b65ec3e46e18ad9261668d24324bb16837ce9baf))

- **deps-dev**: Bump ruff from 0.16.9 to 0.16.10
  ([`4ad2d8a`](https://github.com/marcinpsk/netbox-kea/commit/4ad2d8ae4b9d8c9972b960b10b8c845d38473572))

- **deps-dev**: Bump types-requests
  ([`80caee0`](https://github.com/marcinpsk/netbox-kea/commit/80caee07f13dd3372bddb0feb556f5190fc816ae))

### Chores

- Guard the sync against a hostname that is not the published name
  ([`31ceb88`](https://github.com/marcinpsk/netbox-kea/commit/31ceb8870ee99e3231e64300a8853ce30c9daaaa))

- Match only a reported hostname in kea-sync-hostname-unpublished
  ([`b51ccb0`](https://github.com/marcinpsk/netbox-kea/commit/b51ccb0cce4f8724ac6ca2cb34cd489c69c0848c))

- **templates**: Remove the orphan dashboard and global templates
  ([`3faf8f9`](https://github.com/marcinpsk/netbox-kea/commit/3faf8f9d089885fdc28851d5b99d2c86cbb93ff1))

### Documentation

- Describe the Lease action, creation and signal contract
  ([`4362b52`](https://github.com/marcinpsk/netbox-kea/commit/4362b52689f472fd495f59654614f83483daa4c5))

- Describe the published name of a Reservation hostname
  ([`bd09763`](https://github.com/marcinpsk/netbox-kea/commit/bd097634a8dc66ff5c11a4217ff2bba374079aee))

- **context**: Name the Lease hostname as the lease source's IPAM name
  ([`923d2ab`](https://github.com/marcinpsk/netbox-kea/commit/923d2abc69fb51fdc69d7f0e763acf66459bd910))

- **ipam**: Say why a Global Reservation falls through to its link reports
  ([`0d4865f`](https://github.com/marcinpsk/netbox-kea/commit/0d4865f72df8d6c01e14e23a7adb3a2c2aef0abd))

### Features

- Add one owner for the name that Kea publishes for a reserved hostname
  ([`814a8c2`](https://github.com/marcinpsk/netbox-kea/commit/814a8c287c452e65e829007c9c1867e771c91c34))

- Check a fresh read before a Lease edit or delete, and create Leases from typed requests
  ([`52ce99b`](https://github.com/marcinpsk/netbox-kea/commit/52ce99be8c9648658b2e3121d5a18aaf5150aa68))

- Preview the published name in the Reservation form
  ([`666a4d2`](https://github.com/marcinpsk/netbox-kea/commit/666a4d21e0ad8e6781ff16a05ec15eea586aa8da))

- Reconcile live delegated-prefix Lease ownership safely
  ([`14448c9`](https://github.com/marcinpsk/netbox-kea/commit/14448c97800d5f6ab310433ea14cdbae93da2ce6))

- Reserve a delegated-prefix Lease as a Reservation prefix
  ([`bd748bc`](https://github.com/marcinpsk/netbox-kea/commit/bd748bcd8258647c00c270e20fe64643e061feea))

- Resolve the effective DDNS qualifying suffix of each Subnet
  ([`5779002`](https://github.com/marcinpsk/netbox-kea/commit/5779002e70fc29d3e5ff5eb4a000e49f767e16c1))

- Take the published name in the Reservation add and edit forms
  ([`e3ac265`](https://github.com/marcinpsk/netbox-kea/commit/e3ac2652430960d4621acd21f31396f6d10e77c8))

### Performance Improvements

- **sync**: Skip the lease link classification when no link is unclassified
  ([`e265d5d`](https://github.com/marcinpsk/netbox-kea/commit/e265d5d1f9675a0e99abb0be36b928522ec172ce))

### Refactoring

- Address the review notes on the published-name change
  ([`738ba9e`](https://github.com/marcinpsk/netbox-kea/commit/738ba9e71f6f7be66ab6bd69b76d9403aad2c46e))

- Require the catalogue for a Reservation claim
  ([`cc1e491`](https://github.com/marcinpsk/netbox-kea/commit/cc1e49116297e558a12ff9dc5df05fed22cfefe0))

### Testing

- Check that each sortable htmx table sits inside its container
  ([`3901123`](https://github.com/marcinpsk/netbox-kea/commit/3901123d9ffff1a2c1eb6bc38ecda52f0e4f6d72))

- Pass the verifying catalogue to the Reservation sync state claims
  ([`3aed9d0`](https://github.com/marcinpsk/netbox-kea/commit/3aed9d00cbd424659dc25804357116bc8d3704fe))

- Post a hardware address so the lease journal error tests reach the journal
  ([`3120413`](https://github.com/marcinpsk/netbox-kea/commit/312041379d26e7409cbddf24b6133485d6b31932))

- Record the lease edit and delete replies of a real Kea 3.2
  ([`095a7ba`](https://github.com/marcinpsk/netbox-kea/commit/095a7bab76a554e7a704543e7251678178cd15b8))

- **ipam**: Keep a legacy-marked Prefix in a DNS view syncable
  ([`05d0639`](https://github.com/marcinpsk/netbox-kea/commit/05d0639b6e8a7fbd37f655739c694e8a8f79bfea))

- **jobs**: Move the unhandled server error test next to the DNS view regression
  ([`ca32c6c`](https://github.com/marcinpsk/netbox-kea/commit/ca32c6c1a062775ef7251fa3a4808f83da9ce821))

- **stub**: Derive the transaction time of a lease add from its expire
  ([`32c255b`](https://github.com/marcinpsk/netbox-kea/commit/32c255bd900f9c34a058f2b7f5f0f53fa72aad27))

### Breaking Changes

- The lease_added signal sends creation (the LeaseRequest that Kea confirmed) and lease (the Lease
  that the read back observed, or None), instead of ip_address, hw_address and hostname. The
  leases_deleted signal sends leases (the Lease values whose deletion Kea confirmed) instead of
  ip_addresses. A refused, changed or failed action sends no signal. The DHCPv4 lease CSV import
  requires the hw-address column, and the import result shows the row number and the address of each
  failed row instead of the raw row.


## v2.0.0 (2026-10-08)

### Bug Fixes

- Accept only a local path as a return URL
  ([`51bb3b7`](https://github.com/marcinpsk/netbox-kea/commit/51bb3b720342001e569255783b07f20bfbb7b9ff))

- Bound Subnet ID lease queries to the Kea range
  ([`66a2ca5`](https://github.com/marcinpsk/netbox-kea/commit/66a2ca5c400fb6516ee58202d479681126ccc2ed))

- Check all Reservation MACs before the first MAC write
  ([`910f9bc`](https://github.com/marcinpsk/netbox-kea/commit/910f9bc1c50c6013ddbf3627795568fcfdcf498c))

- Check each Reservation MAC once with a bounded query
  ([`fcca25c`](https://github.com/marcinpsk/netbox-kea/commit/fcca25cf0633714e8646df604d34ad26d169cb00))

- Close coverage and relationship gaps in Lease reads
  ([`4a5d670`](https://github.com/marcinpsk/netbox-kea/commit/4a5d670632e557617c640691b3ba31073bd36640))

- Delete a selected delegated-prefix lease as a prefix
  ([`4d647d0`](https://github.com/marcinpsk/netbox-kea/commit/4d647d0bed6dd849362121f42163eea637c29895))

- Do not link a delegated-prefix lease row to a NetBox IP Address
  ([`b87e539`](https://github.com/marcinpsk/netbox-kea/commit/b87e5390ad9748609427e05f67da5e22d7da4b18))

- Fail one lease delete on a malformed Kea reply
  ([`ce44a99`](https://github.com/marcinpsk/netbox-kea/commit/ce44a99d1aee739790b2518e5207ad0ed07cedc0))

- Hide the background_job label on the lease bulk-delete page
  ([`fd68664`](https://github.com/marcinpsk/netbox-kea/commit/fd68664d2a7fa460dcf00d7c575c1177ef52493c))

- Keep a blank lease search query in the return URL
  ([`e479069`](https://github.com/marcinpsk/netbox-kea/commit/e47906943af97a38a20ea28956fdaa7295fd8373))

- Keep a Reservation conflict a conflict when its MAC does not resolve
  ([`82dd7eb`](https://github.com/marcinpsk/netbox-kea/commit/82dd7ebc3b1f3c8d4d308a5415bcdbe3af17d174))

- Make one event write per MAC sync and check Reservation MACs first
  ([`7d45edc`](https://github.com/marcinpsk/netbox-kea/commit/7d45edcacb3baf94a5355758e9f3ed773ba5ed42))

- Offer lease Sync and Edit only for address leases
  ([`83af234`](https://github.com/marcinpsk/netbox-kea/commit/83af2343ecb170734c260580ff8b708505b20ad5))

- Offer the create-reservation link only for address leases
  ([`7b1866e`](https://github.com/marcinpsk/netbox-kea/commit/7b1866e69c642343a97cd030fe9c7c9016c54bd8))

- Parse lease Subnet IDs as ASCII decimal digits only
  ([`5b9f157`](https://github.com/marcinpsk/netbox-kea/commit/5b9f15781917230c21fbd04f6d636760733258a9))

- Refuse lease Subnet IDs above the Kea range before any request
  ([`298e9a1`](https://github.com/marcinpsk/netbox-kea/commit/298e9a1d100ee42f8e19addb3cb0b505a1ebdf8a))

- Register each Option Definition view once
  ([`8dfc7f1`](https://github.com/marcinpsk/netbox-kea/commit/8dfc7f147c8fccd56bb6aee1e93b46b9c9fe6ca4))

- Render Subnet, Shared Network and Lease page controls in the content block
  ([`81d8257`](https://github.com/marcinpsk/netbox-kea/commit/81d8257490691730bc25902cf1f496bf6067687a))

- Report a Kea reply body that is not a JSON list as a malformed reply
  ([`840f21f`](https://github.com/marcinpsk/netbox-kea/commit/840f21f4708e9614e33f4e6133d5e7ab746c4bd2))

- Return 502 for every Kea transport failure on REST lease search
  ([`06eedc3`](https://github.com/marcinpsk/netbox-kea/commit/06eedc362705aceec916bea364db05bb3782b520))

- Return 502 for every malformed Kea reply on REST lease search
  ([`5107877`](https://github.com/marcinpsk/netbox-kea/commit/5107877e8b86a96e8b6de164174b438647db9b4a))

- Return to the lease search after a lease edit
  ([`5f518f5`](https://github.com/marcinpsk/netbox-kea/commit/5f518f5058ebdda0f982ee76137cecf3b6cbda28))

- Return to the search from the Reserve and Add Reservation links
  ([`e07628d`](https://github.com/marcinpsk/netbox-kea/commit/e07628d883db4fdbc052a85f34b782c877bfb779))

- Run every caught plugin transaction through event_scope.atomic
  ([`b046b5d`](https://github.com/marcinpsk/netbox-kea/commit/b046b5dda09ddf189065151d52fe49153f9d188b))

- Select the Reservations tab on the Reservation import page
  ([`f5f6530`](https://github.com/marcinpsk/netbox-kea/commit/f5f65303ecc214c9bcbc21374100c66860ac32c9))

- Select units from NetBox 4.6.9, where event_tracking first nests
  ([`fb2fb0f`](https://github.com/marcinpsk/netbox-kea/commit/fb2fb0f8b9255ef9256aeacffefe4df4c841c227))

- Serve the lease CSV import at one URL per family
  ([`6ea0120`](https://github.com/marcinpsk/netbox-kea/commit/6ea0120515112ecd65cb0ec5a6cc7748e95dec3e))

- Show a hostname-only Reservation as a host reservation
  ([`efd88f8`](https://github.com/marcinpsk/netbox-kea/commit/efd88f8ba05ee194972e918a00aa47cb889355c0))

- Show a readable message for Kea result 4
  ([`9524bc0`](https://github.com/marcinpsk/netbox-kea/commit/9524bc08f117ef0d939900d594546295675078b1))

- Show no empty meta fields box on the lease bulk-delete page
  ([`00910e7`](https://github.com/marcinpsk/netbox-kea/commit/00910e751385792dbbf726b7d72d1d17a7782a22))

- Show the prefix length on a delegated-prefix lease row
  ([`0acf285`](https://github.com/marcinpsk/netbox-kea/commit/0acf285698d03554802f31af2b472ec255ce2e00))

- Space header icons and tidy the lease import page
  ([`48a1cdf`](https://github.com/marcinpsk/netbox-kea/commit/48a1cdf7a63a5cfabf536255e404503a83cdb020))

### Chores

- Flag a RequestException handler without a RuntimeError sibling
  ([`067cb4d`](https://github.com/marcinpsk/netbox-kea/commit/067cb4d70a9d88fe703ece75b6208bd4a3a81a76))

- **opengrep**: Flag Exception and BaseException inside tuple handlers
  ([`6fef7d5`](https://github.com/marcinpsk/netbox-kea/commit/6fef7d5502b296f1156d2da6c33d7e3c83f1cd55))

- **opengrep**: Guard plugin transactions and NetBox event internals
  ([`a214975`](https://github.com/marcinpsk/netbox-kea/commit/a2149752f7ff9291a54401e60eed6c807a7a0fe8))

### Continuous Integration

- Run the event-rollback tests on NetBox 4.3
  ([`1850b07`](https://github.com/marcinpsk/netbox-kea/commit/1850b07492ea4bcd059dacce32434d4c7a3d7cc7))

### Documentation

- Design for queued events of a rolled-back plugin transaction
  ([`462566d`](https://github.com/marcinpsk/netbox-kea/commit/462566d23ee1493b32eb6ddbf2232d61b6944353))

- Document the Lease observation, REST and CSV contract
  ([`7391ec5`](https://github.com/marcinpsk/netbox-kea/commit/7391ec5e53282b502598c0881e304c8aac509687))

- List the complete Lease export columns and read edges
  ([`4797a3f`](https://github.com/marcinpsk/netbox-kea/commit/4797a3fe69f463d8aac96bb7b2cfdcec66e28e39))

- Name the unit gate in AGENTS.md without a NetBox patch release
  ([`92d83d0`](https://github.com/marcinpsk/netbox-kea/commit/92d83d0438bcd4255a0856770b2e0e124f29a8bf))

- State the NetBox 4.6.9 unit boundary in the design summary
  ([`ae1202f`](https://github.com/marcinpsk/netbox-kea/commit/ae1202f7f6dbe177cef06f0115dcf371989a9195))

- State when event rules see plugin changes, and the known limits
  ([`c3abcd9`](https://github.com/marcinpsk/netbox-kea/commit/c3abcd9c0f00ca3cb29faa0660320fedd0dc8a2c))

### Features

- Add event_scope.atomic as the owner of plugin transactions
  ([`36401ae`](https://github.com/marcinpsk/netbox-kea/commit/36401ae49a720ac3cb4179dfcb0b648e37754f73))

- Read Leases as typed observations in every read consumer
  ([`7ceaad1`](https://github.com/marcinpsk/netbox-kea/commit/7ceaad1997e81402795d626e655c6588d9d3b10e))

### Refactoring

- Catch DatabaseError without its own subclasses
  ([`64eef13`](https://github.com/marcinpsk/netbox-kea/commit/64eef13033477ef441739f7a9f96f08c922cf4b3))

- Catch only the exceptions that each Kea-adjacent try can raise
  ([`25708ab`](https://github.com/marcinpsk/netbox-kea/commit/25708ab8b34103d5aa79fd673eb0d66e28a7ece8))

### Testing

- Answer lease get-all in the branching guard read stub
  ([`9e96255`](https://github.com/marcinpsk/netbox-kea/commit/9e962551662358a4196c415c4df137cf4b168ced))

- Assert a lease with a string subnet-id is excluded and diagnosed
  ([`8c2911d`](https://github.com/marcinpsk/netbox-kea/commit/8c2911d79fe46e90f1185459ca0f156b011dab82))

- Derive the unit gate from the NetBox version, not from event_scope
  ([`d5d4c36`](https://github.com/marcinpsk/netbox-kea/commit/d5d4c362cabca631351daf7cb928bf3020b2122b))

- Exercise missing and null lease subnet-id before reservation lookup
  ([`492f027`](https://github.com/marcinpsk/netbox-kea/commit/492f027488df61de5b745739d1721bcb35dd4d67))

- Expect the not-found warning when a lease vanishes before delete
  ([`7dfe0c6`](https://github.com/marcinpsk/netbox-kea/commit/7dfe0c623280db89017ec0bf047bf4c6a551593e))

- Give the branching guard lease fixtures that a real Kea sends
  ([`e7dd3be`](https://github.com/marcinpsk/netbox-kea/commit/e7dd3be8971e8e472d14fbf3b89fca67826d3c3d))

- Pin the event limits of a late delete guard and a branch merge
  ([`349911d`](https://github.com/marcinpsk/netbox-kea/commit/349911d9c5a08be1fa11d6f6bbfcc91ecf12c93c))

- Remove a prefix Reservation test that pinned a wrong badge
  ([`f32b262`](https://github.com/marcinpsk/netbox-kea/commit/f32b2625ecc27c837fd7de479880e19c1e87f276))

### Breaking Changes

- Export All Leases and All Data use the documented columns family, kind, address, prefix_length,
  subnet_id, state, current, hostname, valid_lifetime, last_transaction, infinite, expires_at, then
  hw_address and client_id for DHCPv4, or duid, iaid, hw_address and preferred_lifetime for DHCPv6.
  A Subnet CIDR search for an unconfigured CIDR is refused instead of returning no leases.

- The leases4 and leases6 REST actions return a normalized Lease observation: count, results,
  diagnostics, complete, next_cursor, query, coverage and evaluated_at. Each result is the public
  Lease projection (family, kind, address, prefix_length, subnet_id, state, current, binding,
  hostname, valid_lifetime, last_transaction and expiration). The UI-enriched keys, the Kea
  extension values and the display labels are gone. Lease CSV exports add the Family, Kind and
  Prefix Length columns and drop the Type, Reserved and NetBox IP columns. Export All Leases and All
  Data refuse an observation with a malformed record. The Current View export is labeled limited
  coverage and is named leases_limited_coverage.csv.


## v1.14.0 (2026-10-07)

### Bug Fixes

- Resolve branch control targets without the script prefix
  ([`fb5175f`](https://github.com/marcinpsk/netbox-kea/commit/fb5175f9acf10d6b3941486da3f0f8802aa47eb9))

- Show a pending reservation on a lease, not Reserved
  ([`07a46fe`](https://github.com/marcinpsk/netbox-kea/commit/07a46fe40d494c448f8419b88a062cc08964a4fb))

### Continuous Integration

- Copy the CA bundle into the compose project for the build secret
  ([`522ba29`](https://github.com/marcinpsk/netbox-kea/commit/522ba29ff5e9f816b53cfef5df95f5dcc8549068))

### Documentation

- Separate the D12 completion note from the snapshot() follow-up
  ([`484e969`](https://github.com/marcinpsk/netbox-kea/commit/484e969c7e86acb8acc62ac4fa0d610e2d4f08fa))

- State that get_client() raises ValueError only
  ([`a3b4715`](https://github.com/marcinpsk/netbox-kea/commit/a3b4715be9d68b74178271b154db07cb434057ae))

### Testing

- Count every runtime snapshot in the documented inventory check
  ([`2ad984d`](https://github.com/marcinpsk/netbox-kea/commit/2ad984d2d51be45a1d08603ada0abae33b9df18d))

- Match branching job test paths at a path boundary
  ([`bfcb8bc`](https://github.com/marcinpsk/netbox-kea/commit/bfcb8bc54b931728b7b0a0f171a283842e8fe0d9))

- Reject a compose secret source that expands to an empty path
  ([`e0acf7a`](https://github.com/marcinpsk/netbox-kea/commit/e0acf7aa135dccb292dffd550983c61bb2ed161a))


## v1.13.0 (2026-10-05)

### Bug Fixes

- A legacy marker kind ends at a word boundary
  ([`0a0eeec`](https://github.com/marcinpsk/netbox-kea/commit/0a0eeec461b1384071793a0ee04bc33462d70c32))

- A plain OSError from a sync snapshot fails only that phase
  ([`b127f39`](https://github.com/marcinpsk/netbox-kea/commit/b127f398cd627bf41351d6b7a3c7124fb63976f4))

- Align ownership routing and sync VRF guidance
  ([`3c0c93c`](https://github.com/marcinpsk/netbox-kea/commit/3c0c93cbb3d8e3c341a63cf678fe52cb6951063e))

- An unknown stale_ip_cleanup mode fails the old cleanup like the reconciliation
  ([`41cd92d`](https://github.com/marcinpsk/netbox-kea/commit/41cd92d5303b785357b325bb4a2feb92ca427377))

- Claim IPAM ownership for per-row sync and lease add
  ([`4530703`](https://github.com/marcinpsk/netbox-kea/commit/453070392f2ecd49392e022aa3b335f06dc55403))

- Clean expired leases when Reservation sync is disabled
  ([`3fcf7d6`](https://github.com/marcinpsk/netbox-kea/commit/3fcf7d67e4514495a1fac86279563e75e04bb22d))

- Cleanup gives an object the hostname of its remaining links
  ([`a37afb3`](https://github.com/marcinpsk/netbox-kea/commit/a37afb381d1c5a9cb3bb233892106a0b3f9ccbf8))

- Drop the per-Server unowned count from sync summaries
  ([`1432d09`](https://github.com/marcinpsk/netbox-kea/commit/1432d095c80cd0fd5477c02c77a9dcd9f4eb55c7))

- Freeze adoption policy only for final-link cleanup
  ([`9264a0c`](https://github.com/marcinpsk/netbox-kea/commit/9264a0cb8e1b60aa10507f86e090f2203f7d8685))

- Handle malformed lease claims and cover reconciliation boundaries
  ([`9d2c0f5`](https://github.com/marcinpsk/netbox-kea/commit/9d2c0f504958def17b6f4110e30cdb8b3df54b43))

- Isolate MAC synchronization writes in a savepoint
  ([`f77c1d6`](https://github.com/marcinpsk/netbox-kea/commit/f77c1d66f6fd8c3319731e4e85132330c26f5e4e))

- KeaClient takes timeout and max_unpaged_leases with no default
  ([`7057edb`](https://github.com/marcinpsk/netbox-kea/commit/7057edb18f69637e2e5b87ed6f1e2c36f2123cd4))

- Keep Server receipts on a forced update
  ([`e6b7b4e`](https://github.com/marcinpsk/netbox-kea/commit/e6b7b4e43467f1041a1c014c4b0aa80c467fb13a))

- Lock ownership links before adoption policy
  ([`f5825b1`](https://github.com/marcinpsk/netbox-kea/commit/f5825b185289669c2a7a984e6a7b9402f9927f40))

- NetBox refuses to start with an unknown stale_ip_cleanup value
  ([`52a1cbd`](https://github.com/marcinpsk/netbox-kea/commit/52a1cbd00fddca89ef464d8a43474e9a387a15ae))

- NetBox validates every netbox_kea setting at startup
  ([`4c0f3d6`](https://github.com/marcinpsk/netbox-kea/commit/4c0f3d6a88c8191cb5d3e926913f89d3c80ef33e))

- NetBox validates sync_enabled at startup too
  ([`928f3b2`](https://github.com/marcinpsk/netbox-kea/commit/928f3b29921d7ac83c0ffd0292b99a9ae03a3c6b))

- One phase compares its own reports with the owner hostname rule
  ([`a7df8f2`](https://github.com/marcinpsk/netbox-kea/commit/a7df8f2efb78ea216c89f22c174321245673c729))

- Permit ownership checks during receipt completion
  ([`f920215`](https://github.com/marcinpsk/netbox-kea/commit/f920215ca3000bf1e86355c000d9e3ee6db966fc))

- Preserve branch refusals and assert cleanup outcomes
  ([`17fe719`](https://github.com/marcinpsk/netbox-kea/commit/17fe7193a7858da3efa3efe538cc1028c6f4e2c3))

- Preserve IPAM upgrade receipts and scoped summaries
  ([`5b9a323`](https://github.com/marcinpsk/netbox-kea/commit/5b9a323c7c2ec1143508e9e5359c960bb4344641))

- Preserve network import completion with invalid global options
  ([`1b2a98e`](https://github.com/marcinpsk/netbox-kea/commit/1b2a98ec1e70a06998bbca245098d06a4335c8c0))

- Preserve operator notes when claiming IP addresses
  ([`eca1ade`](https://github.com/marcinpsk/netbox-kea/commit/eca1ade62d12a8ed62d24ccb335c48b07747daee))

- Preserve prechange data for plugin updates
  ([#233](https://github.com/marcinpsk/netbox-kea/pull/233),
  [`3c43fc2`](https://github.com/marcinpsk/netbox-kea/commit/3c43fc2600e12774175a0f60ee2e7e2a96c99594))

- Preserve shared prefix markers and claimed MAC results
  ([`9290c28`](https://github.com/marcinpsk/netbox-kea/commit/9290c28bb0482447382b8661bcdb7491709694e4))

- Record IPAM sync changes in the changelog
  ([#234](https://github.com/marcinpsk/netbox-kea/pull/234),
  [`33dd3e4`](https://github.com/marcinpsk/netbox-kea/commit/33dd3e4fb0584bd6a382584f744ffb71036f04b1))

- Refuse busy cleanup identities in outer transactions
  ([`792046b`](https://github.com/marcinpsk/netbox-kea/commit/792046b5450d95a42d1a4e8a5c2b10995b40b4cd))

- Refuse busy ownership rows during cleanup
  ([`0ee7ad3`](https://github.com/marcinpsk/netbox-kea/commit/0ee7ad395b3cba21c6d1fb516d95e981897e0287))

- Reject malformed lease subnet IDs before reconciliation
  ([`9ebd9d3`](https://github.com/marcinpsk/netbox-kea/commit/9ebd9d313cbd1b5018393908584afdc5cedef7f1))

- Release deprecated IP ownership after operator edits
  ([`88674c5`](https://github.com/marcinpsk/netbox-kea/commit/88674c517f36c60092d742445d6eb633a3ed69ab))

- Remove obsolete lease mask resolver import
  ([`147d12f`](https://github.com/marcinpsk/netbox-kea/commit/147d12fc23995218721051125db5ebaf8a5e9e58))

- Resolve lease ownership facts from the Kea catalogue
  ([`c7a91b7`](https://github.com/marcinpsk/netbox-kea/commit/c7a91b75ca61c55f0a09ae3139b99303a311b61e))

- Retain delegated-prefix failure diagnostics
  ([`7b41592`](https://github.com/marcinpsk/netbox-kea/commit/7b4159291ccd83abda360abb8cd75d03cc27174f))

- Retain every commit's release guidance in changelog
  ([`c724167`](https://github.com/marcinpsk/netbox-kea/commit/c72416775b11d0152192f8e571a9e99666fc773d))

- Serialize adoption cleanup with policy changes
  ([`58b5a94`](https://github.com/marcinpsk/netbox-kea/commit/58b5a9488124669ca9063be7e501ab79d390241f))

- Sync manually added leases without explicit Subnet IDs
  ([`d892bfa`](https://github.com/marcinpsk/netbox-kea/commit/d892bfad54c4564e45cec284f69c3530216a2efc))

- The MAC address sync does not write the MAC address to the log
  ([`efe1584`](https://github.com/marcinpsk/netbox-kea/commit/efe1584901e3b0aedd52eb6135b7ebb11e0e8584))

- The MAC address sync logs only the exception type
  ([`c640a61`](https://github.com/marcinpsk/netbox-kea/commit/c640a613877bdea8e0879b72b342c79632200a8e))

- The old stale cleanup decides each row under the identity lock
  ([`2ed6c39`](https://github.com/marcinpsk/netbox-kea/commit/2ed6c39eba840b6b6c82224a0caa11fc3e6090fe))

- The old sync paths leave an object unchanged when the marker does not fit
  ([`7a34f07`](https://github.com/marcinpsk/netbox-kea/commit/7a34f0767325c4803b3b0ba253e4fff36f4a90ab))

### Chores

- Drop the upstream author from the pyproject authors
  ([`becd5d1`](https://github.com/marcinpsk/netbox-kea/commit/becd5d106b3947c5ffe55a0d5163565106e5a704))

- Put SPDX copyright headers in each file, with the holders from git blame
  ([`284ecb2`](https://github.com/marcinpsk/netbox-kea/commit/284ecb2fc5b4770e134286242fc10b3d00a3d613))

- **deps**: Bump the github-actions group with 2 updates
  ([`6ed2548`](https://github.com/marcinpsk/netbox-kea/commit/6ed25481a14dc5d75790587ed5d79fdefbfde55f))

- **deps**: Bump virtualenv
  ([`059edae`](https://github.com/marcinpsk/netbox-kea/commit/059edae798e32ea71cfdb26fbdeb6d23a6248987))

- **deps-dev**: Bump ruff from 0.16.8 to 0.16.9
  ([`c048769`](https://github.com/marcinpsk/netbox-kea/commit/c04876986102acec5c5a71ec3aa704702fc2daa3))

- **opengrep**: The IPAM sync job catches OSError, not RequestException
  ([`bd38c9f`](https://github.com/marcinpsk/netbox-kea/commit/bd38c9f7800057f219d89d53630905d65d0120dc))

### Code Style

- Drop em-dashes from the truncation log and the job summary list
  ([`3368d49`](https://github.com/marcinpsk/netbox-kea/commit/3368d498c8e3fc19181d0c3843235becdd24cb3f))

### Continuous Integration

- Codecov waits for both coverage uploads before it reports
  ([`da527e2`](https://github.com/marcinpsk/netbox-kea/commit/da527e2606c099d069366303c7ceb7f6d79f1fe6))

### Documentation

- A branch delete of an object that a Kea server owns is refused
  ([`2723979`](https://github.com/marcinpsk/netbox-kea/commit/2723979f6e3f921e9518e3ed543325e8732e6efd))

- ADR 0006, README and AGENTS.md describe the Reservation phase
  ([`b2d0e72`](https://github.com/marcinpsk/netbox-kea/commit/b2d0e72442d240415c5917b7b6a643cb46f274c6))

- ADR 0006, README and CONTEXT describe the marker and owner rules as built
  ([`025964b`](https://github.com/marcinpsk/netbox-kea/commit/025964b5899a287ebcb4eb970fd20698df17da24))

- Generate IPAM ownership upgrade notes
  ([`0dfe335`](https://github.com/marcinpsk/netbox-kea/commit/0dfe335e7b17ab9ce11b9eef48bfa76b91451ecf))

- IPAM Ownership links stay in main, and a branch delete that reaches one is refused
  ([`2d42645`](https://github.com/marcinpsk/netbox-kea/commit/2d42645038dd9b66b2c09dd3117fcd96aef3de93))

- Lease IP addresses use the Sync VRF, and the summary counts owner disagreements
  ([`6996b6b`](https://github.com/marcinpsk/netbox-kea/commit/6996b6bd31c03c6129dfc2fd3b1e8ccf4e080dc1))

- Refresh generated upgrade notes after develop rebase
  ([`41b50d7`](https://github.com/marcinpsk/netbox-kea/commit/41b50d72f66798c4170d05f8025c54599f866810))

- Regenerate complete IPAM upgrade guidance
  ([`e6ee457`](https://github.com/marcinpsk/netbox-kea/commit/e6ee4570d18e8e6831a2d37799be94c28affcf6e))

- Scope the two-phase last-link rule to IP addresses
  ([`f93902f`](https://github.com/marcinpsk/netbox-kea/commit/f93902fcbb73872556fa538d50762c3bcb9b2d6e))

### Features

- Add the IPAM ownership link model and the confirmation sequence
  ([`2adedde`](https://github.com/marcinpsk/netbox-kea/commit/2adedde4ab0c0824e56a8ee3c4f42993d9ba785e))

- Reconcile Prefixes and IP Ranges with optional deprecation
  ([`c2428c4`](https://github.com/marcinpsk/netbox-kea/commit/c2428c400047f8afb36a071ea4c57bd9fbf7eff2))

- Refuse a branch delete that reaches an IPAM ownership link through NetBox's AbortRequest
  ([`976c15d`](https://github.com/marcinpsk/netbox-kea/commit/976c15d94370e9f54cc9fae268945c0f5cf3ad99))

- The job's lease phase runs through reconcile() and owns its rows through links
  ([`fceb54e`](https://github.com/marcinpsk/netbox-kea/commit/fceb54ef0cd8b2c56ac1dfbadee6e0075a1cf717))

- The job's Reservation phase runs through reconcile()
  ([`1464be7`](https://github.com/marcinpsk/netbox-kea/commit/1464be7330d5c2354eab5af0046b5e2e00481697))

- The old stale cleanup never touches an IP address with an ownership link
  ([`51559a9`](https://github.com/marcinpsk/netbox-kea/commit/51559a900e4e6e4947d8e080f03617d0a62324ca))

- The sync marker is a block that keeps the operator note after it
  ([`9bdd4dd`](https://github.com/marcinpsk/netbox-kea/commit/9bdd4dd66b1ee949ab99f7f29d777e1abed3cb17))

### Performance Improvements

- Skip snapshots for unchanged sync objects
  ([`027b8c9`](https://github.com/marcinpsk/netbox-kea/commit/027b8c9afdfb5c4415910deaf6abe5dd62b687d1))

### Refactoring

- A failed lease snapshot goes through SyncReport.fail_snapshot
  ([`b095fe0`](https://github.com/marcinpsk/netbox-kea/commit/b095fe065f741db052adb678b069e559fd251c52))

- A stale link records the cutoff number of its mark
  ([`a50241b`](https://github.com/marcinpsk/netbox-kea/commit/a50241bf0983d2f4d1ab6e8e2f051bdfeedc3240))

- Adopt IPAM rows and remove legacy sync paths
  ([`aece546`](https://github.com/marcinpsk/netbox-kea/commit/aece546d5ae622b404407a17920a851d9c7580e9))

- Claim DHCP import IPAM ownership
  ([`31ea5dc`](https://github.com/marcinpsk/netbox-kea/commit/31ea5dcdf783b8ab2585dd4930831ad8890f724f))

- Drop Marker.note, which only tests read
  ([`251b033`](https://github.com/marcinpsk/netbox-kea/commit/251b0333a9998bcf9b878644c2820373d0e2acac))

- Remove obsolete claim guards and verify upgrade states
  ([`8b1d9b9`](https://github.com/marcinpsk/netbox-kea/commit/8b1d9b970e29b40a3b3aa5dbf76167f9c60404e4))

- Run bulk Reservation Sync through reconciliation
  ([`afddbfa`](https://github.com/marcinpsk/netbox-kea/commit/afddbfac0dd69f9924ddb53d1894aee74a5aba62))

- The old sync paths drop the two-pass status mode
  ([`c1b885f`](https://github.com/marcinpsk/netbox-kea/commit/c1b885f294a8335d0a1faeec9984c1c427d9bbca))

### Testing

- An incomplete Reservation snapshot keeps a Reservation link that is not the last
  ([`d65f461`](https://github.com/marcinpsk/netbox-kea/commit/d65f461fb48c608f834c34d7eff17c4df39cfbb7))

- Concurrent lease phases, row locks and operator edits on separate connections
  ([`4d6ba2f`](https://github.com/marcinpsk/netbox-kea/commit/4d6ba2f549d55835e32af08422ed9c8dc2c6d0b2))

- Guard 2 pins the relations that a delete in a branch reaches
  ([`66245fb`](https://github.com/marcinpsk/netbox-kea/commit/66245fbc3a90640a49070870f9cdc743da0d9a14))

- IPAM ownership links stay in main through branch sync, discard, merge and revert
  ([`534ba21`](https://github.com/marcinpsk/netbox-kea/commit/534ba21172609db963144de056004780f95b6e00))

- **ui**: The browser cleanup finds its NetBox IP by the sync marker
  ([`f8c5d09`](https://github.com/marcinpsk/netbox-kea/commit/f8c5d091b160660191ef636cb3537f899eeaec99))

### Additional Release Information

- `stale_ip_cleanup = "remove"` now removes expired leases, deleted Reservations and hostless leases
  after their last ownership link becomes stale. On upgrade, it removes only lease rows that expire
  after adoption. Rows already stale get no owner and stay unowned and counted.

- When every configured Server uses the same non-global sync_vrf, an unowned global marker address
  moves into it with its primary key and changelog intact. An existing owner or destination
  collision prevents the move. Mixed VRFs keep the global row. The read-only
  Server.ipam_first_complete_at field records initial completion across enabled job and DHCP import
  observations.


## Unreleased

### Bug Fixes

- A legacy marker kind ends at a word boundary
  ([`8d04bdd`](https://github.com/marcinpsk/netbox-kea/commit/8d04bdde5bdf8c782efd2d20636183b10c09ca88))

- A plain OSError from a sync snapshot fails only that phase
  ([`726a0a3`](https://github.com/marcinpsk/netbox-kea/commit/726a0a3700dca3055cdc4228879d4dea3da30036))

- Align ownership routing and sync VRF guidance
  ([`a5feaeb`](https://github.com/marcinpsk/netbox-kea/commit/a5feaeba8eb4f08afd1064e44814e36a549006be))

- An unknown stale_ip_cleanup mode fails the old cleanup like the reconciliation
  ([`198cbf7`](https://github.com/marcinpsk/netbox-kea/commit/198cbf7ff48101e4e5a24e912cbfdf8d7154047c))

- Claim IPAM ownership for per-row sync and lease add
  ([`98784ed`](https://github.com/marcinpsk/netbox-kea/commit/98784ed6f6ee99a05a00948a60799bd05bb116f9))

- Cleanup gives an object the hostname of its remaining links
  ([`59f9670`](https://github.com/marcinpsk/netbox-kea/commit/59f967094d0388707b6251ee5c1e32019f764632))

- KeaClient takes timeout and max_unpaged_leases with no default
  ([`30c276c`](https://github.com/marcinpsk/netbox-kea/commit/30c276c3913f1d9fda01a8d394f5b53706faa226))

- NetBox refuses to start with an unknown stale_ip_cleanup value
  ([`ad3789d`](https://github.com/marcinpsk/netbox-kea/commit/ad3789d56e82337c10d66680cca1a6c9b816175e))

- NetBox validates every netbox_kea setting at startup
  ([`d2fcc4a`](https://github.com/marcinpsk/netbox-kea/commit/d2fcc4a526d754fc09f16b4c1f61694d7b631115))

- NetBox validates sync_enabled at startup too
  ([`00ea72d`](https://github.com/marcinpsk/netbox-kea/commit/00ea72d5bb002c93fcd24cb5b1f749e7895dfab0))

- One phase compares its own reports with the owner hostname rule
  ([`f43ec13`](https://github.com/marcinpsk/netbox-kea/commit/f43ec13720bd6e6aff7ddccdf1f638e1274f11e1))

- Preserve shared prefix markers and claimed MAC results
  ([`3f7a7e1`](https://github.com/marcinpsk/netbox-kea/commit/3f7a7e1a6face4bd8e4324a4e40323e496c60ab2))

- Reject malformed lease subnet IDs before reconciliation
  ([`6816352`](https://github.com/marcinpsk/netbox-kea/commit/6816352d971e9adc4a2c2d04f57907938a74f581))

- Release deprecated IP ownership after operator edits
  ([`22a9b38`](https://github.com/marcinpsk/netbox-kea/commit/22a9b384597fe5cf67b222fffa869f81c796178c))

- Remove obsolete lease mask resolver import
  ([`5a9f1b1`](https://github.com/marcinpsk/netbox-kea/commit/5a9f1b157a875a42b9895fcf8490a650718c7c76))

- Resolve lease ownership facts from the Kea catalogue
  ([`163ee15`](https://github.com/marcinpsk/netbox-kea/commit/163ee153e6eec126c5b7f30eb4e664fca6978dc1))

- Retain every commit's release guidance in changelog
  ([`c12036d`](https://github.com/marcinpsk/netbox-kea/commit/c12036d240e4275e2dc725e89f22cd31139e8026))

- Sync manually added leases without explicit Subnet IDs
  ([`a0c5696`](https://github.com/marcinpsk/netbox-kea/commit/a0c5696f3db465e4f951cef367640bed8eb38b88))

- The MAC address sync does not write the MAC address to the log
  ([`b1fba1d`](https://github.com/marcinpsk/netbox-kea/commit/b1fba1d9134c54bdcf4a4072fe80fd348b620866))

- The MAC address sync logs only the exception type
  ([`c08f626`](https://github.com/marcinpsk/netbox-kea/commit/c08f6268cdc6cb01c9c3d85d04bcac2967fb73a9))

- The old stale cleanup decides each row under the identity lock
  ([`0902df4`](https://github.com/marcinpsk/netbox-kea/commit/0902df4d1b275328b286f6a5f8a36b22e0c871ff))

- The old sync paths leave an object unchanged when the marker does not fit
  ([`f5c26bd`](https://github.com/marcinpsk/netbox-kea/commit/f5c26bde2d37d5670157b13c096e72d70ea561ac))

### Chores

- Drop the upstream author from the pyproject authors
  ([`becd5d1`](https://github.com/marcinpsk/netbox-kea/commit/becd5d106b3947c5ffe55a0d5163565106e5a704))

- Put SPDX copyright headers in each file, with the holders from git blame
  ([`284ecb2`](https://github.com/marcinpsk/netbox-kea/commit/284ecb2fc5b4770e134286242fc10b3d00a3d613))

- **deps**: Bump the github-actions group with 2 updates
  ([`6ed2548`](https://github.com/marcinpsk/netbox-kea/commit/6ed25481a14dc5d75790587ed5d79fdefbfde55f))

- **deps**: Bump virtualenv
  ([`059edae`](https://github.com/marcinpsk/netbox-kea/commit/059edae798e32ea71cfdb26fbdeb6d23a6248987))

- **deps-dev**: Bump ruff from 0.16.8 to 0.16.9
  ([`c048769`](https://github.com/marcinpsk/netbox-kea/commit/c04876986102acec5c5a71ec3aa704702fc2daa3))

- **opengrep**: The IPAM sync job catches OSError, not RequestException
  ([`9f8890c`](https://github.com/marcinpsk/netbox-kea/commit/9f8890c63f4417e3144aada3e8414768625e9b86))

### Code Style

- Drop em-dashes from the truncation log and the job summary list
  ([`443d7af`](https://github.com/marcinpsk/netbox-kea/commit/443d7af9f6f328533cd4ab51b8dce75c0d2d3cf2))

### Continuous Integration

- Codecov waits for both coverage uploads before it reports
  ([`da527e2`](https://github.com/marcinpsk/netbox-kea/commit/da527e2606c099d069366303c7ceb7f6d79f1fe6))

### Documentation

- A branch delete of an object that a Kea server owns is refused
  ([`db04f5d`](https://github.com/marcinpsk/netbox-kea/commit/db04f5dbee0b208bb1e16a8fef5eb84d1861698b))

- ADR 0006, README and AGENTS.md describe the Reservation phase
  ([`04ba7d0`](https://github.com/marcinpsk/netbox-kea/commit/04ba7d0507e9fcf6d8c24c18fe0dc64e898a38fd))

- ADR 0006, README and CONTEXT describe the marker and owner rules as built
  ([`6f70b16`](https://github.com/marcinpsk/netbox-kea/commit/6f70b16576d863b5cf015a6cad878560c91c921b))

- Generate IPAM ownership upgrade notes
  ([`6fbd846`](https://github.com/marcinpsk/netbox-kea/commit/6fbd8468bc736f51512fdc63c4a36982b8566a63))

- IPAM Ownership links stay in main, and a branch delete that reaches one is refused
  ([`95bce1c`](https://github.com/marcinpsk/netbox-kea/commit/95bce1cd366577cb31c4f27edab566385c16ca93))

- Lease IP addresses use the Sync VRF, and the summary counts owner disagreements
  ([`7c22d2c`](https://github.com/marcinpsk/netbox-kea/commit/7c22d2c2b0b9f903f7eb91288b267e4073ece533))

- Refresh generated upgrade notes after develop rebase
  ([`cd4cc88`](https://github.com/marcinpsk/netbox-kea/commit/cd4cc88ecb52e65d58dcb64b3c17901233d7cb5e))

### Features

- Add the IPAM ownership link model and the confirmation sequence
  ([`5785ee3`](https://github.com/marcinpsk/netbox-kea/commit/5785ee302bda449844d3df53f60d6686c6eafb13))

- Reconcile Prefixes and IP Ranges with optional deprecation
  ([`6212a9e`](https://github.com/marcinpsk/netbox-kea/commit/6212a9e5647871ba8d01a97de3c913d1cb6d6bfd))

- Refuse a branch delete that reaches an IPAM ownership link through NetBox's AbortRequest
  ([`3e481b2`](https://github.com/marcinpsk/netbox-kea/commit/3e481b208018f7d3d9cd90de96ae7acd8f5933bf))

- The job's lease phase runs through reconcile() and owns its rows through links
  ([`18a6629`](https://github.com/marcinpsk/netbox-kea/commit/18a6629fec359603448c1247bd80315aa918c1c1))

- The job's Reservation phase runs through reconcile()
  ([`5730d23`](https://github.com/marcinpsk/netbox-kea/commit/5730d2364be0f7ebc89545f8366c8030cddca68f))

- The old stale cleanup never touches an IP address with an ownership link
  ([`06075aa`](https://github.com/marcinpsk/netbox-kea/commit/06075aa37e78ffca7b7bdaafdd8e8e8bf978b964))

- The sync marker is a block that keeps the operator note after it
  ([`aafdc16`](https://github.com/marcinpsk/netbox-kea/commit/aafdc1614b0ac2a873bbe589bb4ddd348bda7967))

### Refactoring

- A failed lease snapshot goes through SyncReport.fail_snapshot
  ([`1f5fced`](https://github.com/marcinpsk/netbox-kea/commit/1f5fceda5d7cf74c2f91423f1635bfd31f5b0ad0))

- A stale link records the cutoff number of its mark
  ([`1f84f4d`](https://github.com/marcinpsk/netbox-kea/commit/1f84f4dc0c68bba67205489a4234728abe5ee7bf))

- Adopt IPAM rows and remove legacy sync paths
  ([`b3f82c7`](https://github.com/marcinpsk/netbox-kea/commit/b3f82c71b3f9689339013a5fbf1107090b7c5611))

- Claim DHCP import IPAM ownership
  ([`ce8826f`](https://github.com/marcinpsk/netbox-kea/commit/ce8826fd7c2252720361030034da119707be9b7e))

- Drop Marker.note, which only tests read
  ([`eec09fb`](https://github.com/marcinpsk/netbox-kea/commit/eec09fb6f66a9bcbc0708f76572ec714438ee84a))

- Remove obsolete claim guards and verify upgrade states
  ([`1c8c571`](https://github.com/marcinpsk/netbox-kea/commit/1c8c571500656d45b37b8cd71dcc3ed31af4d46a))

- Run bulk Reservation Sync through reconciliation
  ([`4a23238`](https://github.com/marcinpsk/netbox-kea/commit/4a23238be5804000b29dfd9ef994337f5681912d))

- The old sync paths drop the two-pass status mode
  ([`2cf29c2`](https://github.com/marcinpsk/netbox-kea/commit/2cf29c2125fd07802eb29775dca00f4116043d31))

### Testing

- An incomplete Reservation snapshot keeps a Reservation link that is not the last
  ([`a24889a`](https://github.com/marcinpsk/netbox-kea/commit/a24889a69b7a7667c9d5e2115b3adf7596c4ce18))

- Concurrent lease phases, row locks and operator edits on separate connections
  ([`67fd33f`](https://github.com/marcinpsk/netbox-kea/commit/67fd33fc8fa4c491e54047b6a8c4cb524509a20d))

- Guard 2 pins the relations that a delete in a branch reaches
  ([`0938dfb`](https://github.com/marcinpsk/netbox-kea/commit/0938dfb5b67bb256d7f2caf32a4a50a571865584))

- IPAM ownership links stay in main through branch sync, discard, merge and revert
  ([`0840a59`](https://github.com/marcinpsk/netbox-kea/commit/0840a597c3c8c5049d217cfea1643fcebc0bd16f))

- **ui**: The browser cleanup finds its NetBox IP by the sync marker
  ([`8229a76`](https://github.com/marcinpsk/netbox-kea/commit/8229a765f325292ee5c54a428418e673597f37c0))

### Additional Release Information

- `stale_ip_cleanup = "remove"` now removes expired leases, deleted Reservations and hostless leases
  after their last ownership link becomes stale. On upgrade, it removes only lease rows that expire
  after adoption. Rows already stale get no owner and stay unowned and counted.

- When every configured Server uses the same non-global sync_vrf, an unowned global marker address
  moves into it with its primary key and changelog intact. An existing owner or destination
  collision prevents the move. Mixed VRFs keep the global row. The read-only
  Server.ipam_first_complete_at field records initial completion across enabled job and DHCP import
  observations.


## v1.12.0 (2026-10-01)

### Bug Fixes

- A Reservation mutation requires exactly one reply from Kea
  ([`b76b07f`](https://github.com/marcinpsk/netbox-kea/commit/b76b07ff020c642b1c8b8136ff8571f7422177ec))

- Every KeaClient must carry a write guard
  ([`4deb722`](https://github.com/marcinpsk/netbox-kea/commit/4deb7225720bf7bf7a8bfb600c2db9cc6dbf503f))

- KeaIpamSyncJob.enqueue_once accepts a positional interval
  ([`6491095`](https://github.com/marcinpsk/netbox-kea/commit/64910951733a83e11a346d1963e40337cef2f3bd))

- Keep every netbox_kea model in main with a constant resolver
  ([`09eb53b`](https://github.com/marcinpsk/netbox-kea/commit/09eb53b041b0d7df46d43dbfedafabd1ab1d3211))

- Let an error in the SyncConfig save fail as a server error
  ([`66a0709`](https://github.com/marcinpsk/netbox-kea/commit/66a070915fe79d02e243b89f8e19c5f25fd40718))

- Make the branch banner a navbar button, because NetBox renders the navbar twice
  ([`3a856c3`](https://github.com/marcinpsk/netbox-kea/commit/3a856c33d05e872d48176d4c4cef847cf7d31d5a))

- Migration 0018 refuses an invalid sync_interval_minutes by name
  ([`818efc3`](https://github.com/marcinpsk/netbox-kea/commit/818efc38a304310decd21c8b4706d4a1ba08c4e6))

- Read the sync interval from SyncConfig when the job is scheduled
  ([`dd4e44c`](https://github.com/marcinpsk/netbox-kea/commit/dd4e44c8e3d5d51b8108d89f54a294c82000b5fa))

- Refuse a Kea configuration write before the cache invalidation
  ([`9780e03`](https://github.com/marcinpsk/netbox-kea/commit/9780e0317cbf98014ce0faaf9dda21b7f980e304))

### Chores

- **deps**: Bump the uv group across 1 directory with 3 updates
  ([`37cd8d1`](https://github.com/marcinpsk/netbox-kea/commit/37cd8d102e284e6672e1030c2199627a644c2bf0))

### Continuous Integration

- Pin the NetBox release and netbox-branching once, and reuse the integration steps
  ([`f383043`](https://github.com/marcinpsk/netbox-kea/commit/f383043f6514ae68f33a3776ee20af203848a03a))

- Read the coverage settings from pyproject in the branching-test job
  ([`4972f71`](https://github.com/marcinpsk/netbox-kea/commit/4972f718766049e63a6ece02d925a97e84e63b91))

- Run the branching browser tests on the netbox-branching harness variant
  ([`40fbd87`](https://github.com/marcinpsk/netbox-kea/commit/40fbd87abb3693f676043741e7ae576803320da9))

- Run the netbox-branching tests with NetBox 4.7.0 and netbox-branching 1.2.1
  ([`e94c957`](https://github.com/marcinpsk/netbox-kea/commit/e94c95725cbc20af83234625b37a2cf2bbf43ac2))

- Upload the branching-test coverage to Codecov
  ([`9f3bfcd`](https://github.com/marcinpsk/netbox-kea/commit/9f3bfcd3584a826e215e9dc34d6b7ca3920e089f))

### Documentation

- Delete, do not merge, a pre-upgrade branch that changed a Kea server password
  ([`3a5c1f0`](https://github.com/marcinpsk/netbox-kea/commit/3a5c1f0e14abd827786009a95e47a28642a10f9b))

- Describe the increment 4 branching sinks
  ([`dbb6efd`](https://github.com/marcinpsk/netbox-kea/commit/dbb6efdb33bf355dfaec6337cbb08a79294caa64))

- Drop an em-dash from the KeaClient paragraph in AGENTS.md
  ([`35af538`](https://github.com/marcinpsk/netbox-kea/commit/35af5385dcd10d0a978146111daec3de6424ef12))

- Reflow the increment 3 note, add the GraphQL case and the issue numbers
  ([`20cd0f7`](https://github.com/marcinpsk/netbox-kea/commit/20cd0f7eadc3449bfd69542b453c1690a86f52c1))

- Say in AGENTS.md that every KeaClient takes a write guard
  ([`a917e4b`](https://github.com/marcinpsk/netbox-kea/commit/a917e4b2ce876e098c8d64d438510ba9af0cb008))

- Say in the README that a branch refuses plugin writes outside a web request too
  ([`5206294`](https://github.com/marcinpsk/netbox-kea/commit/5206294e06e4d34b96162016bc7c68e3084fd3a9))

- Say that the plugin refuses changes in a branch, and record increment 3
  ([`c8b72b9`](https://github.com/marcinpsk/netbox-kea/commit/c8b72b9f689d344cc87169e857f1b13e1657baf3))

- Say the migration reads PLUGINS_CONFIG once to create the SyncConfig row
  ([`fbb1659`](https://github.com/marcinpsk/netbox-kea/commit/fbb165960b48a26efd1c486b77883a18c318755e))

- Say the Sync Jobs page sets the sync interval, not NetBox admin
  ([`eb0df49`](https://github.com/marcinpsk/netbox-kea/commit/eb0df49439fc82afa52080a1c43ba86357bdbb91))

- Say where the branching browser tests run, and what the banner is
  ([`b92a535`](https://github.com/marcinpsk/netbox-kea/commit/b92a5358c5ef81ed83416b6b9152d240966b57f1))

- Say which unusable branch selections get netbox-branching's HTTP 400
  ([`04d1542`](https://github.com/marcinpsk/netbox-kea/commit/04d1542d11f2b1e88b4c64f6c499037b206556f2))

- State the sync VRF protection and the partial netbox-branching support
  ([`4772200`](https://github.com/marcinpsk/netbox-kea/commit/477220053fd5f015f3b4453cd432ad74338bec4f))

### Features

- Create the SyncConfig row in a migration, so a GET of a sync page writes nothing
  ([`1f08395`](https://github.com/marcinpsk/netbox-kea/commit/1f0839587615c6f7e4aa39a10ccaac10082264e3))

- Fake every netbox_kea migration on a branch, and check it
  ([`c3fb487`](https://github.com/marcinpsk/netbox-kea/commit/c3fb4876a7e5e2007d72c62dff08771a09262fe4))

- KeaClient.command() takes a KeaCommand member and a target family
  ([`5ecbb7a`](https://github.com/marcinpsk/netbox-kea/commit/5ecbb7a3409bf71bb166e2dd9da4effbf1bfcaf7))

- KeaIpamSyncJob fails in a branch before any read
  ([`ed0a3c3`](https://github.com/marcinpsk/netbox-kea/commit/ed0a3c3d3cbbb7f715e371c241d93bb8804b9db4))

- Keep netbox_kea rows in main when netbox-branching is installed
  ([`ed93a49`](https://github.com/marcinpsk/netbox-kea/commit/ed93a490b2d5fb729995179d893f0c9f462a73dc))

- Protect a VRF that a Server syncs into
  ([`cad0731`](https://github.com/marcinpsk/netbox-kea/commit/cad0731c07632223e0b717fd6ee405e1377edda1))

- Refuse a save or a delete of a netbox_kea row in a branch
  ([`4e2f4d3`](https://github.com/marcinpsk/netbox-kea/commit/4e2f4d33f4c72a4ae471ef0052d6542b31c95ba4))

- Refuse plugin changes in a branch, and name the sources of plugin responses
  ([`606f6c9`](https://github.com/marcinpsk/netbox-kea/commit/606f6c96fd67740f0bc4afdb13c06e726cb3f85e))

- Show a branch banner on plugin pages, and no reservation links on the IP panel
  ([`1d3355d`](https://github.com/marcinpsk/netbox-kea/commit/1d3355df62c9b9f219b71884dd3fb994cdb83656))

- The Kea transport refuses a write command in a branch
  ([`4b5907a`](https://github.com/marcinpsk/netbox-kea/commit/4b5907abda55963c5acc7504716e5a266097cd5f))

### Refactoring

- Drop the Reservation reply checks that command() already makes
  ([`d881e5f`](https://github.com/marcinpsk/netbox-kea/commit/d881e5f3364d6b77a0c4c81b0f80e34671ce7c53))

- KeaCommand.is_write names the write kind once
  ([`4438948`](https://github.com/marcinpsk/netbox-kea/commit/4438948ec2a14a15b57205ea9b18751b5305e00e))

- Keep the branch-refusal receivers in branching.py
  ([`450d158`](https://github.com/marcinpsk/netbox-kea/commit/450d158e64cf077969ab4b3cfa47ec1268f61fe6))

- Take SAFE_METHODS from REST framework, and name each refusal code once
  ([`479b080`](https://github.com/marcinpsk/netbox-kea/commit/479b080812890f389c870b9cc821b64ca1200190))

### Testing

- A failed cache invalidation is logged, and the change is still sent and persisted
  ([`096fa6b`](https://github.com/marcinpsk/netbox-kea/commit/096fa6bf4c21bf7ffdeb2512396126d9bcd7f415))

- A merge that deletes a VRF a Server now syncs into fails and changes nothing
  ([`bfe373a`](https://github.com/marcinpsk/netbox-kea/commit/bfe373ade70c84a0c4329afcf64d05458a7c6d9f))

- A REST view outside the plugin gets netbox-branching's 400 for an unready branch header
  ([`cf1b639`](https://github.com/marcinpsk/netbox-kea/commit/cf1b6390ab7cede9a67f8384a29e79d82239797a))

- Add a netbox-branching harness variant and browser tests for Sync all
  ([`b608b80`](https://github.com/marcinpsk/netbox-kea/commit/b608b80bf2bea890930029a3373f266e03b730f3))

- Add archived, failed and empty-cookie rows to the selector table
  ([`7a19222`](https://github.com/marcinpsk/netbox-kea/commit/7a1922259505a30c9eef717db82bf0e2333ac1c0))

- An empty reply to reservation-add raises before the persist step
  ([`7764ba3`](https://github.com/marcinpsk/netbox-kea/commit/7764ba3253fabf83e1a1b40bc02d580cacb071f1))

- BranchActive from a view outside the plugin also gets the 409
  ([`c3916bc`](https://github.com/marcinpsk/netbox-kea/commit/c3916bc1d7bfcb8511847ebcfce8096ab9d880c6))

- Browser tests compare the state from before the page loads
  ([`7494e9b`](https://github.com/marcinpsk/netbox-kea/commit/7494e9b871b631134512aef0dd21bbf430a60fa3))

- Guard 1 sends every method to every plugin URL in a provisioned branch
  ([`bb09994`](https://github.com/marcinpsk/netbox-kea/commit/bb09994dd714a53670c51694263673d02ad3f2fe))

- Guard 1 sends POST, PUT, PATCH and DELETE to every plugin route
  ([`b475ec0`](https://github.com/marcinpsk/netbox-kea/commit/b475ec06e2150555dd72bdb7368a0f2a81e6a1a3))

- Let the test settings and the CI configuration writer load netbox-branching
  ([`6f511fe`](https://github.com/marcinpsk/netbox-kea/commit/6f511fe3e2a5f5efda96f493466381b7754e3c59))

- Name three KeaClient.command() tests after what they assert
  ([`28c2f84`](https://github.com/marcinpsk/netbox-kea/commit/28c2f845bbae7d1a06f66f867aaef1c0d3ef54f1))

- Pass timeout= to the CI configuration writer run
  ([`5024170`](https://github.com/marcinpsk/netbox-kea/commit/502417068223175286f33bc811b64655cda7badf))

- Prove the SyncConfig row on a fresh database comes from migration 0018
  ([`cb57cba`](https://github.com/marcinpsk/netbox-kea/commit/cb57cbadd6bc600074f8fbc6296e8e5f3dc904eb))

- Put the KeaClient.command() tests in their own class
  ([`ec0a5c0`](https://github.com/marcinpsk/netbox-kea/commit/ec0a5c09ba0049f9262f07207556b226971fe31f))

- Remove an unused Kea command helper that used the old command() signature
  ([`af0249a`](https://github.com/marcinpsk/netbox-kea/commit/af0249a856ed97acbbf559324caad3f21ab3d94f))

- The boundary scanner reports a KeaCommand attribute that is not a member
  ([`93ac74a`](https://github.com/marcinpsk/netbox-kea/commit/93ac74a80970ce98307aecdae285f47afca20859))

- The boundary scanner treats a mapping-pattern capture as a rebinding
  ([`fdd80de`](https://github.com/marcinpsk/netbox-kea/commit/fdd80de29a265a5a3076841b5a7546a3dd61fb9f))

- The reservation boundary scanner resolves a name only through its kea import
  ([`7c600f3`](https://github.com/marcinpsk/netbox-kea/commit/7c600f3a55722d99aa967193236f2cf7cd2d63ba))

- Type the cache-invalidation refusal test, so mypy checks its body
  ([`6c3b7bc`](https://github.com/marcinpsk/netbox-kea/commit/6c3b7bc0ec8f2b54c7e1d1e71b71fc9c9efaa89d))

- Type the guard 4 helpers with Sequence, as SeparateDatabaseAndState holds them
  ([`082adc7`](https://github.com/marcinpsk/netbox-kea/commit/082adc7f82cf58b45351c5117c6fe430e44aa204))


## v1.11.0 (2026-10-01)

### Bug Fixes

- Build the Subnet update before the first step, so an edit that NetBox cannot build sends nothing
  ([`aab511e`](https://github.com/marcinpsk/netbox-kea/commit/aab511e917189fa12fadac8fe0c8cd92a576ebbf))

- Compare a Subnet lifetime or timer only when the edit writes it
  ([`9aa05f9`](https://github.com/marcinpsk/netbox-kea/commit/9aa05f9b023fe4ebd25c595f20d548db8fdd018b))

- Keep Shared Network facts out of the Catalogue, and fail closed on an unknown membership
  ([`3807b49`](https://github.com/marcinpsk/netbox-kea/commit/3807b49858f9922f428cb450083a164cc154f825))

- Keep the option text that Kea holds when a save leaves the value
  ([`304e095`](https://github.com/marcinpsk/netbox-kea/commit/304e095edd9885555f5a55ad7e0a1f7e4d1a6082))

- Name a live value that the edit form cannot show
  ([`c0e6c0e`](https://github.com/marcinpsk/netbox-kea/commit/c0e6c0eaa89dec80e62495d11cc89baefdb207e9))

- Name config-set in the help of the shared network and server options edit pages
  ([`9d76e31`](https://github.com/marcinpsk/netbox-kea/commit/9d76e31cc77b3f6b37ccf1ff5ec11394dc853746))

- Redirect the Pool add GET when NetBox cannot confirm the Subnet
  ([`bc30b73`](https://github.com/marcinpsk/netbox-kea/commit/bc30b73491a2ae2e679899f9ddb761d5593d353d))

- Refuse a Shared Network or Subnet edit whose shown values changed in Kea
  ([`7cdd103`](https://github.com/marcinpsk/netbox-kea/commit/7cdd103f8a45d7a390f2bf4a0b8ab3ec0b6c2f73))

- Refuse a Subnet edit when a live Pool entry is malformed, instead of dropping it
  ([`94454fa`](https://github.com/marcinpsk/netbox-kea/commit/94454faae0d00dd7ef33d3e5c0ab1265c5439516))

- Refuse a Subnet form whose page could not load the Shared Networks
  ([`ec21d5f`](https://github.com/marcinpsk/netbox-kea/commit/ec21d5f5bbcc5f5be66c65421dff0ae20a5b6ce0))

- Scan only comments and docstrings for source line numbers, and say an unknown membership sends no
  undo
  ([`4cc3c5f`](https://github.com/marcinpsk/netbox-kea/commit/4cc3c5fdcf986c6dbe82bb0f51fb6e4492285ae8))

- Select the managed DHCP Option entry with one rule for display and save
  ([`fb00a3b`](https://github.com/marcinpsk/netbox-kea/commit/fb00a3b2c335c409508a10be0d699414ffb48bd7))

- Show why Kea could not persist a Reservation change
  ([`aa24096`](https://github.com/marcinpsk/netbox-kea/commit/aa240963dd4d51c963356b0ed1b23a416e8e9717))

- **config_write**: Catch only a malformed configuration, and require a Verified Subnet for Subnet
  options
  ([`9557842`](https://github.com/marcinpsk/netbox-kea/commit/9557842464c0ccaa5468b071fcd011e5a83f12ee))

- **config_write**: Check the Pool before the send, and keep ID reuse from reading as a rejection
  ([`21a0d39`](https://github.com/marcinpsk/netbox-kea/commit/21a0d390c6faf656c7de0c13b2ea039e93326372))

- **config_write**: Keep the outcome when the lock transaction fails to commit after the change
  ([`3452dc2`](https://github.com/marcinpsk/netbox-kea/commit/3452dc21bcf9c90db4172c4d435d4c2e7a757d8a))

- **config_write**: Refuse a Subnet edit whose Shared Network changed after the page
  ([`ac350b5`](https://github.com/marcinpsk/netbox-kea/commit/ac350b562c60ebddcf9e385e8cc58d5394d310bc))

- **config_write**: Send Subnet delete and Pool writes only to the Subnet the page showed
  ([`fe1b182`](https://github.com/marcinpsk/netbox-kea/commit/fe1b1820a8c783b39ca6bf50df540d3bf6aa6996))

- **dhcp_options**: Refuse a save when a shown option value changed
  ([`1040349`](https://github.com/marcinpsk/netbox-kea/commit/1040349379a40f4a1573dda69621b25136dc1833))

- **kea**: Keep the Pool-level fields of each Pool that a Subnet edit keeps
  ([`8eb8005`](https://github.com/marcinpsk/netbox-kea/commit/8eb8005122f6fb5b2ab5355dbdb1b084ae46b4b9))

- **kea**: Send Pool changes only as subnet delta commands
  ([`f2e0ece`](https://github.com/marcinpsk/netbox-kea/commit/f2e0ece50834daf9ecea0ed8d9543891cd5fc502))

- **opengrep**: Exempt only a lambda passed to _run_config_change
  ([`405ff45`](https://github.com/marcinpsk/netbox-kea/commit/405ff451f21d4f82cf8fb71914275f625748a548))

- **opengrep**: Refuse a _run_config_change defined outside views/_base.py
  ([`6e75f4c`](https://github.com/marcinpsk/netbox-kea/commit/6e75f4c84123339c6549aadaa8f3cfce07fe10aa))

- **scripts**: End a stalled download in the Kea recording script
  ([`9ef124b`](https://github.com/marcinpsk/netbox-kea/commit/9ef124b30e5b92aca6fee3289bbcdf9256b45e1b))

### Chores

- **coverage**: Exclude raise NotImplementedError
  ([`a13bc03`](https://github.com/marcinpsk/netbox-kea/commit/a13bc03368c76578ada14b0203b17b3b9312f2dd))

- **opengrep**: Require timeout= on every subprocess call
  ([`2576714`](https://github.com/marcinpsk/netbox-kea/commit/257671457b88b80875d0e92d25cb3eed1a36c2a8))

### Continuous Integration

- Make the unit-test coverage run read pyproject's coverage config
  ([`c85e9d1`](https://github.com/marcinpsk/netbox-kea/commit/c85e9d12a92878dc244ee01bf5331408098acf9c))

### Documentation

- Escape the pipe in Family | None, so the R4-N1 row keeps four columns
  ([`5e4bfa9`](https://github.com/marcinpsk/netbox-kea/commit/5e4bfa9ced54338359fce0c1885892fd11f2b7c3))

- Netbox-branching compatibility design (ratified r6) and ADR 0007
  ([`9d66dcf`](https://github.com/marcinpsk/netbox-kea/commit/9d66dcf03b75fe6b8b3deb8447ea78dd44bcf0a7))

- Remove the review history from the netbox-branching design
  ([`88f0188`](https://github.com/marcinpsk/netbox-kea/commit/88f0188299367c50dbf2ca50c0c42d78f4f88496))

- Say which read each check under the lock sends
  ([`d1ea9bd`](https://github.com/marcinpsk/netbox-kea/commit/d1ea9bdd1373c38dbea84d39f8a1959bb16742a4))

- Scope two netbox-branching acceptance conditions to the contract table
  ([`c882ce2`](https://github.com/marcinpsk/netbox-kea/commit/c882ce2e91ee518744e8f5046a812ad77a5a6e5d))

- Tell operators that a database idle timeout can end the change lock
  ([`4c5670b`](https://github.com/marcinpsk/netbox-kea/commit/4c5670bfba9dc9dcbe8534a5ee88af3b50edf988))

- **adr**: ADR 0007 names the writes that its branch refusal does not cover
  ([`d9f9347`](https://github.com/marcinpsk/netbox-kea/commit/d9f9347e8517b432406a9f9bbba80906cc20215c))

- **adr**: Say that the ADR 0007 refusals arrive in the tracked increments
  ([`77a2431`](https://github.com/marcinpsk/netbox-kea/commit/77a24316ea8f2e33648e5ec85184c9b9e562cac8))

- **agents**: Name #207 as the removal ticket, and say that old write paths skip the lock
  ([`2fdc7c4`](https://github.com/marcinpsk/netbox-kea/commit/2fdc7c44e2e9cf8057a07b6a0120ccc0c0b80c2a))

### Features

- Log a Configuration Change that holds the Kea server lock longer than the lock wait
  ([`c428c04`](https://github.com/marcinpsk/netbox-kea/commit/c428c04a235388a08086a4cf4e126ac77eeb2fa7))

- **config_write**: Add the Configuration Change outcome and use it for Shared Network add and
  delete
  ([`6d56f99`](https://github.com/marcinpsk/netbox-kea/commit/6d56f99db295595d9482462c29fce085d9cc7cac))

- **opengrep**: Refuse a broad except around a config_write call in a view
  ([`b84c9d5`](https://github.com/marcinpsk/netbox-kea/commit/b84c9d5ce27a1fc310134385ad0bba59d80746df))

### Performance Improvements

- Check a Subnet after a step with one Subnet list read
  ([`3fe210b`](https://github.com/marcinpsk/netbox-kea/commit/3fe210bfe6adec2e334fd7f2876caae2077ff462))

- Send no config-get from the Subnet add and edit POST
  ([`a91524a`](https://github.com/marcinpsk/netbox-kea/commit/a91524a5128bd09771472c0ac26046ce09acc5c6))

- Take the target Shared Network from the Subnet scope read
  ([`fe60730`](https://github.com/marcinpsk/netbox-kea/commit/fe607303cd1afb8810c6842307e415711bf73536))

### Refactoring

- Delete the last old write exceptions, and refuse a view that catches the rejection
  ([`61ad2be`](https://github.com/marcinpsk/netbox-kea/commit/61ad2bed063d2b3393f3ba122430b6707368ba40))

- Move the form option projection to dhcp_options
  ([`981fe1a`](https://github.com/marcinpsk/netbox-kea/commit/981fe1a7e4545fd493dc1e71d566d7598efd571c))

- One typed Pool parser, with Pool form errors for out-of-Subnet and overlapping Pools
  ([#216](https://github.com/marcinpsk/netbox-kea/pull/216),
  [`c1ecf1a`](https://github.com/marcinpsk/netbox-kea/commit/c1ecf1a6c25d2a0b6e1445ef3263bc79a2f36f8d))

- Rename the Subnet membership fields to original_network
  ([`e6cb2bd`](https://github.com/marcinpsk/netbox-kea/commit/e6cb2bdd0bbb032e371ce509f9067f2757e75bb9))

- **config_write**: Add a Subnet and assign it to a Shared Network as one change
  ([#223](https://github.com/marcinpsk/netbox-kea/pull/223),
  [`390c151`](https://github.com/marcinpsk/netbox-kea/commit/390c15122ce1ce8c42d00d47c2228b903bc79280))

- **config_write**: Edit a Subnet and move it between Shared Networks as one change
  ([`2dc2645`](https://github.com/marcinpsk/netbox-kea/commit/2dc264582430a8ff12c6b9797f2cdf2637e9012f))

- **config_write**: Run the read-modify-write changes through config_write
  ([`04ed6bd`](https://github.com/marcinpsk/netbox-kea/commit/04ed6bd396a210df335654fa460b051be152c4a3))

- **forms**: Derive the hidden shown copies from one declaration
  ([`5a2df92`](https://github.com/marcinpsk/netbox-kea/commit/5a2df92a0437ebd2f1038dc68844224ebbf0ae1d))

- **pools**: Move the Pool type and its parser below kea.py
  ([`8c383a4`](https://github.com/marcinpsk/netbox-kea/commit/8c383a45e9eed9f39e26939732bf4e876f710702))

- **reservations**: Use the shared config_write persist step
  ([#219](https://github.com/marcinpsk/netbox-kea/pull/219),
  [`2d5c4d0`](https://github.com/marcinpsk/netbox-kea/commit/2d5c4d0deb16a36a55fea795d3693dbbf233dcbe))

### Testing

- Assert the commands of the refused GET in the null option-data edit test
  ([`3b440c4`](https://github.com/marcinpsk/netbox-kea/commit/3b440c4e740fe23ec70934fddec77f256f3feea9))

- Cover a failed Pool sync in the IPAM job
  ([`dee9c38`](https://github.com/marcinpsk/netbox-kea/commit/dee9c3868f07ec58f12b3323b9417e60bebd61ca))

- Fail loudly when a stub Subnet names a Shared Network that the stub does not hold
  ([`d0a9ab9`](https://github.com/marcinpsk/netbox-kea/commit/d0a9ab9276ac7cfcd3b96c8bbe2110dbb30b525f))

- Fail on a source line number in a comment or docstring
  ([`bd79cef`](https://github.com/marcinpsk/netbox-kea/commit/bd79cef5c1c0f397b0fae64e51837040d9f16a5b))

- Name the behaviour instead of source line numbers in test docstrings
  ([`90b61e6`](https://github.com/marcinpsk/netbox-kea/commit/90b61e667497ae5bb5550ac6c5231d49d87bdd95))

- Name the code that the Subnet view tests exercise today
  ([`7cd4de7`](https://github.com/marcinpsk/netbox-kea/commit/7cd4de70f41484ddf44ae803f9329e935624eb58))

- Name the inherited option tests after _inherited_subnet_options
  ([`e44f86f`](https://github.com/marcinpsk/netbox-kea/commit/e44f86ff906061289acdddd667a3a19d7506307e))

- Refuse a dotted --cov source, which splits the psycopg error classes
  ([`f257e01`](https://github.com/marcinpsk/netbox-kea/commit/f257e012f9cc8960f64c31bd29a329d35baacea2))

- **config_write**: A Pool failure whose read shows the change and another writer's Pool is unknown
  ([`fe4cdbb`](https://github.com/marcinpsk/netbox-kea/commit/fe4cdbb344a1d6d843991f5ebd12dda6635f18b9))

- **config_write**: Cover a non-lock database error and a missing TLS file on the change
  ([`2c6debf`](https://github.com/marcinpsk/netbox-kea/commit/2c6debfe0fdf6eeea0e5e5d1697f514151d1bfea))

- **config_write**: Cover the Subnet options target checks and a config-get without a configuration
  ([`b0be1d2`](https://github.com/marcinpsk/netbox-kea/commit/b0be1d2d4bd10fa26e2216244a2364af522e3855))

- **config_write**: Tighten the Pool-keep test to an NTP-only save
  ([`741c9ea`](https://github.com/marcinpsk/netbox-kea/commit/741c9ea5cda69bc8893d647909b170bd7996c5e1))

- **kea**: Take Kea command names from a recorded list-commands reply
  ([`d2990cf`](https://github.com/marcinpsk/netbox-kea/commit/d2990cfb67c9d415be9aba3b12b83b6fe315cc74))

- **kea_stub**: Answer the Subnet edit commands in SubnetDaemon
  ([`c9e993c`](https://github.com/marcinpsk/netbox-kea/commit/c9e993c783d994d73f8e3f234d6d5d8feb80a31f))

- **kea_stub**: Check the commands that a list-commands Response object advertises
  ([`be294b1`](https://github.com/marcinpsk/netbox-kea/commit/be294b14db900de914c50ba3b7960d26d6f882a0))

- **subnets**: Remove subnet_cidr from the Kea Pool objects in two fixtures
  ([`8d4cf9d`](https://github.com/marcinpsk/netbox-kea/commit/8d4cf9d2e67630f40f8ac63c6f63956f79d07e2e))

- **views**: A Subnet that Kea holds in another text form saves from its page
  ([`eaa8fe4`](https://github.com/marcinpsk/netbox-kea/commit/eaa8fe4e0b06465978d46114704732d269fe3e4d))


## v1.10.2 (2026-09-28)

### Bug Fixes

- Run each DHCP-plugin import write in a savepoint, and pass sync a parsed Subnet network (#199)
  ([#215](https://github.com/marcinpsk/netbox-kea/pull/215),
  [`c449171`](https://github.com/marcinpsk/netbox-kea/commit/c449171cb341bd5aab853459871e04138966819f))

### Chores

- **deps**: Bump the github-actions group with 4 updates
  ([#193](https://github.com/marcinpsk/netbox-kea/pull/193),
  [`c5abb6b`](https://github.com/marcinpsk/netbox-kea/commit/c5abb6b9e1b5269e9f22acaac60e724cd0e2e44a))

- **deps-dev**: Bump djangorestframework-stubs from 3.17.1 to 3.18.0
  ([#163](https://github.com/marcinpsk/netbox-kea/pull/163),
  [`3f91cd8`](https://github.com/marcinpsk/netbox-kea/commit/3f91cd8c3b7cf3f2db64bf61df1ed497039c9aa8))

- **deps-dev**: Bump python-semantic-release from 10.6.2 to 10.7.0
  ([#192](https://github.com/marcinpsk/netbox-kea/pull/192),
  [`242ed87`](https://github.com/marcinpsk/netbox-kea/commit/242ed87fd5356d891b371e32e7716844a61539b3))

- **deps-dev**: Bump ruff from 0.16.7 to 0.16.8
  ([#191](https://github.com/marcinpsk/netbox-kea/pull/191),
  [`e059a51`](https://github.com/marcinpsk/netbox-kea/commit/e059a51d46c8aafb624f39ebcfa116df814cdde9))

### Documentation

- Record the Config Write outcome and IPAM ownership decisions
  ([#195](https://github.com/marcinpsk/netbox-kea/pull/195),
  [`cb2d712`](https://github.com/marcinpsk/netbox-kea/commit/cb2d712ea65d961552c698b1236708266f243a39))

### Testing

- **ui**: Assert the harness data exists instead of skipping (#160)
  ([#194](https://github.com/marcinpsk/netbox-kea/pull/194),
  [`046e625`](https://github.com/marcinpsk/netbox-kea/commit/046e625e9bcda336972b7a5559672a5c9b8159db))


## v1.10.1 (2026-09-21)

### Bug Fixes

- **ui**: Hide the Help button this package ships no page for
  ([#181](https://github.com/marcinpsk/netbox-kea/pull/181),
  [`c197115`](https://github.com/marcinpsk/netbox-kea/commit/c197115cb69d9d510aa2f24b033872bc1f892dca))


## v1.10.0 (2026-09-19)

### Bug Fixes

- Catalog ([#142](https://github.com/marcinpsk/netbox-kea/pull/142),
  [`4872b00`](https://github.com/marcinpsk/netbox-kea/commit/4872b00a6cce0ef3120e785447c8dbe2a68ffdf1))

- Restore pytest-django database semantics and repair a squashed-away migration dependency (#153)
  ([#159](https://github.com/marcinpsk/netbox-kea/pull/159),
  [`b76318b`](https://github.com/marcinpsk/netbox-kea/commit/b76318bb0dbd706b679dc579ff6740cebeb10c21))

- Send Kea's subnet lifetime parameter names on subnet update
  ([#177](https://github.com/marcinpsk/netbox-kea/pull/177),
  [`266679e`](https://github.com/marcinpsk/netbox-kea/commit/266679e0bf2ab88b1e91975d3e5abbea084b758d))

### Chores

- **deps**: Bump codecov/codecov-action from 7.0.0 to 7.1.0 in the github-actions group
  ([#167](https://github.com/marcinpsk/netbox-kea/pull/167),
  [`96496ce`](https://github.com/marcinpsk/netbox-kea/commit/96496ce6fc95f3236f615bb708f2a262bb6e8c13))

- **deps**: Bump sqlparse in the uv group across 1 directory
  ([#138](https://github.com/marcinpsk/netbox-kea/pull/138),
  [`09640c9`](https://github.com/marcinpsk/netbox-kea/commit/09640c989174f89fce0bd5aa29ce84492a16324a))

- **deps**: Bump the github-actions group across 1 directory with 3 updates
  ([#158](https://github.com/marcinpsk/netbox-kea/pull/158),
  [`e1b2a95`](https://github.com/marcinpsk/netbox-kea/commit/e1b2a9595afb3d79570127448e24fc097259d772))

- **deps**: Bump the github-actions group with 2 updates
  ([#149](https://github.com/marcinpsk/netbox-kea/pull/149),
  [`5df698e`](https://github.com/marcinpsk/netbox-kea/commit/5df698e4f0d85e1e37d712fba072e46f3e0c9fc9))

- **deps**: Bump the github-actions group with 3 updates
  ([#145](https://github.com/marcinpsk/netbox-kea/pull/145),
  [`d7e870e`](https://github.com/marcinpsk/netbox-kea/commit/d7e870ee6fa1ff8c0db5fe92d6193a4c6a2e7c19))

- **deps**: Bump the uv group across 1 directory with 2 updates
  ([#162](https://github.com/marcinpsk/netbox-kea/pull/162),
  [`45480dd`](https://github.com/marcinpsk/netbox-kea/commit/45480dd4222ede9061fef3fc4c72a57b7eadc5ad))

- **deps-dev**: Bump mypy from 2.3.0 to 2.3.1
  ([#143](https://github.com/marcinpsk/netbox-kea/pull/143),
  [`f90aacd`](https://github.com/marcinpsk/netbox-kea/commit/f90aacdd21bdab1f2e05943fb6c26df257beb397))

- **deps-dev**: Bump pre-commit from 4.6.1 to 4.6.2
  ([#134](https://github.com/marcinpsk/netbox-kea/pull/134),
  [`68a135d`](https://github.com/marcinpsk/netbox-kea/commit/68a135dd0a0f8cad398c9e3f7a576d039165b2fd))

- **deps-dev**: Bump pytest-django from 4.12.0 to 4.14.0
  ([#133](https://github.com/marcinpsk/netbox-kea/pull/133),
  [`872a5d6`](https://github.com/marcinpsk/netbox-kea/commit/872a5d6f4f0653eb8fc1cfcc1224a14590729f2b))

- **deps-dev**: Bump pytest-playwright from 0.8.0 to 0.9.0
  ([#164](https://github.com/marcinpsk/netbox-kea/pull/164),
  [`7f3bfba`](https://github.com/marcinpsk/netbox-kea/commit/7f3bfbadbb104c878d45d171bb3adc7c4c8345be))

- **deps-dev**: Bump pytest-playwright from 0.8.0 to 0.9.0
  ([#136](https://github.com/marcinpsk/netbox-kea/pull/136),
  [`93be512`](https://github.com/marcinpsk/netbox-kea/commit/93be51221a234d900b27543ceaf6949276addaed))

- **deps-dev**: Bump python-semantic-release from 10.6.1 to 10.6.2
  ([#151](https://github.com/marcinpsk/netbox-kea/pull/151),
  [`6f040f2`](https://github.com/marcinpsk/netbox-kea/commit/6f040f2c17653d919a05d5039fae9974cf9754f3))

- **deps-dev**: Bump ruff from 0.16.1 to 0.16.2
  ([#135](https://github.com/marcinpsk/netbox-kea/pull/135),
  [`3291763`](https://github.com/marcinpsk/netbox-kea/commit/32917634805b1d501061244bb2d6dccb466c7ed7))

- **deps-dev**: Bump ruff from 0.16.2 to 0.16.3
  ([#144](https://github.com/marcinpsk/netbox-kea/pull/144),
  [`67dc1c1`](https://github.com/marcinpsk/netbox-kea/commit/67dc1c1391ef564d34c2f9a0e5bf3e8e9bbee910))

- **deps-dev**: Bump ruff from 0.16.3 to 0.16.4
  ([#148](https://github.com/marcinpsk/netbox-kea/pull/148),
  [`a5f5a95`](https://github.com/marcinpsk/netbox-kea/commit/a5f5a950cefb68a4807c9698e7e834665c75a4eb))

- **deps-dev**: Bump ruff from 0.16.4 to 0.16.6
  ([#156](https://github.com/marcinpsk/netbox-kea/pull/156),
  [`4bb5fab`](https://github.com/marcinpsk/netbox-kea/commit/4bb5fabff3872c48b11d9604284fe3d0111f19af))

- **deps-dev**: Bump ruff from 0.16.4 to 0.16.7
  ([#166](https://github.com/marcinpsk/netbox-kea/pull/166),
  [`32cefd7`](https://github.com/marcinpsk/netbox-kea/commit/32cefd799d7dedfc7c16755b286f95633e28dae8))

- **deps-dev**: Bump types-pyyaml from 6.0.12.20250915 to 6.0.12.20260906
  ([#165](https://github.com/marcinpsk/netbox-kea/pull/165),
  [`108f76c`](https://github.com/marcinpsk/netbox-kea/commit/108f76c7db1cbf43da8ee4bee0e80d4ddc074408))

- **deps-dev**: Bump types-requests ([#157](https://github.com/marcinpsk/netbox-kea/pull/157),
  [`a2ee20b`](https://github.com/marcinpsk/netbox-kea/commit/a2ee20be812c382f10c74a9c3f79784e715a6c16))

### Documentation

- Record the Server Configuration split and track the ADRs
  ([#176](https://github.com/marcinpsk/netbox-kea/pull/176),
  [`8a6b0da`](https://github.com/marcinpsk/netbox-kea/commit/8a6b0daaca78d4e09ed69336ddb463e7922d60f3))

### Features

- Reservation rework ([#147](https://github.com/marcinpsk/netbox-kea/pull/147),
  [`9dcd502`](https://github.com/marcinpsk/netbox-kea/commit/9dcd502e31e0a6b0859c6b5709b72eb89e4f666d))


## v1.9.0 (2026-08-13)

### Features

- Read subnet suggestions from subnet_cmds on both datalists
  ([#119](https://github.com/marcinpsk/netbox-kea/pull/119),
  [`6727f4f`](https://github.com/marcinpsk/netbox-kea/commit/6727f4fd9b495fb9d5ada3f45c7c689f4d20443e))


## v1.8.0 (2026-08-11)

### Chores

- **deps**: Bump django in the uv group across 1 directory
  ([#118](https://github.com/marcinpsk/netbox-kea/pull/118),
  [`ed0c684`](https://github.com/marcinpsk/netbox-kea/commit/ed0c684280336f02c8624d6136414fcd641b13c9))

- **deps-dev**: Bump ruff from 0.16.0 to 0.16.1
  ([#115](https://github.com/marcinpsk/netbox-kea/pull/115),
  [`315e001`](https://github.com/marcinpsk/netbox-kea/commit/315e0012da68ff8989e96ee86efbae373631db87))

### Features

- Replace subnet_id with subnet_cidr on reservation add/edit forms
  ([`0e03fb8`](https://github.com/marcinpsk/netbox-kea/commit/0e03fb8f922f9d47afa7eab388d24da7de54268c))


## v1.7.2 (2026-08-07)

### Bug Fixes

- Handle invalid CA paths in status badge ([#117](https://github.com/marcinpsk/netbox-kea/pull/117),
  [`baae8c3`](https://github.com/marcinpsk/netbox-kea/commit/baae8c3b5df2fb812819d07b919a1711dfa88f8d))

### Chores

- **deps**: Bump gitpython in the uv group across 1 directory
  ([#112](https://github.com/marcinpsk/netbox-kea/pull/112),
  [`44a4d59`](https://github.com/marcinpsk/netbox-kea/commit/44a4d591c163f0a25888a514b8af78fe85d9f590))

- **deps**: Bump the github-actions group with 2 updates
  ([#116](https://github.com/marcinpsk/netbox-kea/pull/116),
  [`34eaad2`](https://github.com/marcinpsk/netbox-kea/commit/34eaad2df7205b66df5b6fa0212ddffd1890911a))

- **deps-dev**: Bump types-requests ([#114](https://github.com/marcinpsk/netbox-kea/pull/114),
  [`939ac51`](https://github.com/marcinpsk/netbox-kea/commit/939ac519ee685c061a1fbf45231faceb0d507591))


## v1.7.1 (2026-07-28)

### Bug Fixes

- Render and manage reservations that reserve no address
  ([#111](https://github.com/marcinpsk/netbox-kea/pull/111),
  [`49084ca`](https://github.com/marcinpsk/netbox-kea/commit/49084cabae03bfd456baec6f3b15c65020a98da1))

### Chores

- **deps**: Bump the github-actions group with 2 updates
  ([#103](https://github.com/marcinpsk/netbox-kea/pull/103),
  [`e6d43b1`](https://github.com/marcinpsk/netbox-kea/commit/e6d43b120e00a6420918c9b641f9213428ec8f2c))

- **deps-dev**: Bump mypy from 1.19.1 to 2.3.0
  ([#107](https://github.com/marcinpsk/netbox-kea/pull/107),
  [`adb49ef`](https://github.com/marcinpsk/netbox-kea/commit/adb49ef8784eb4351cb3d1bafb3e77373c667d53))

- **deps-dev**: Bump pre-commit from 4.5.1 to 4.6.1
  ([#105](https://github.com/marcinpsk/netbox-kea/pull/105),
  [`80255e0`](https://github.com/marcinpsk/netbox-kea/commit/80255e0c2ef8cd2190778a6b6c83e2f09af72dd9))

- **deps-dev**: Bump pynetbox from 7.6.1 to 7.8.0
  ([#108](https://github.com/marcinpsk/netbox-kea/pull/108),
  [`9c21e4e`](https://github.com/marcinpsk/netbox-kea/commit/9c21e4e6c6fc334477da78db46664ae8cc722996))

- **deps-dev**: Bump python-semantic-release from 10.5.3 to 10.6.1
  ([#104](https://github.com/marcinpsk/netbox-kea/pull/104),
  [`812b31f`](https://github.com/marcinpsk/netbox-kea/commit/812b31fbe46cd537fedb67cc3e644d34bc68b12a))

- **deps-dev**: Bump ruff from 0.15.2 to 0.16.0
  ([#106](https://github.com/marcinpsk/netbox-kea/pull/106),
  [`c8d8823`](https://github.com/marcinpsk/netbox-kea/commit/c8d88236573265a7a1ce9b803c1984950f0d3c6b))

### Continuous Integration

- Bump the Kea test harness to 3.2.0 ([#100](https://github.com/marcinpsk/netbox-kea/pull/100),
  [`314858e`](https://github.com/marcinpsk/netbox-kea/commit/314858e6538a288189f5deea248eabd58da05532))

### Testing

- **jobs**: Drive the sync-job tests through a real KeaClient
  ([#101](https://github.com/marcinpsk/netbox-kea/pull/101),
  [`1361808`](https://github.com/marcinpsk/netbox-kea/commit/1361808281196d49dc27341ea042a0289abf0dc8))

- **server**: Add NetBox ViewTestCases/APIViewTestCases coverage for Server
  ([#102](https://github.com/marcinpsk/netbox-kea/pull/102),
  [`4d279d0`](https://github.com/marcinpsk/netbox-kea/commit/4d279d0ea277c1fe5e9ca8bdaaf00b90d15d003a))

- **ui**: Make configure_table wait for column moves to land
  ([#109](https://github.com/marcinpsk/netbox-kea/pull/109),
  [`af1b0ec`](https://github.com/marcinpsk/netbox-kea/commit/af1b0ec4128d2d7c4a328a543568e4dd5e7a5fc0))


## v1.7.0 (2026-07-23)

### Chores

- **deps**: Bump gitpython in the uv group across 1 directory
  ([#98](https://github.com/marcinpsk/netbox-kea/pull/98),
  [`13c2164`](https://github.com/marcinpsk/netbox-kea/commit/13c2164bc0220c53f49e8552cfce6795a1b3821e))

### Features

- Omit the Kea 'service' arg on direct-daemon connections
  ([#99](https://github.com/marcinpsk/netbox-kea/pull/99),
  [`b4c15ec`](https://github.com/marcinpsk/netbox-kea/commit/b4c15ec7655aeb62e30d7877c37eebb3437eeef7))

### Testing

- De-mock the view tests (PR A) — real KeaClient + HTTP-boundary stub
  ([#97](https://github.com/marcinpsk/netbox-kea/pull/97),
  [`368adad`](https://github.com/marcinpsk/netbox-kea/commit/368adadbb4458a03ff441629f314ae94c62fd386))


## v1.6.0 (2026-07-22)

### Chores

- Cover .github/CODEOWNERS in REUSE.toml ([#85](https://github.com/marcinpsk/netbox-kea/pull/85),
  [`2413516`](https://github.com/marcinpsk/netbox-kea/commit/2413516f791284005a7cdf527ba83fc251c5c0d1))

- **deps**: Bump requests from 2.33.0 to 2.34.2
  ([#86](https://github.com/marcinpsk/netbox-kea/pull/86),
  [`1958b0d`](https://github.com/marcinpsk/netbox-kea/commit/1958b0da0a2a0354bcb47b52d7896986dbe1d2a2))

- **deps**: Bump the github-actions group with 7 updates
  ([#89](https://github.com/marcinpsk/netbox-kea/pull/89),
  [`cdfb522`](https://github.com/marcinpsk/netbox-kea/commit/cdfb52213043b92ef08865fd900f1466292542db))

- **deps**: Bump the uv group across 1 directory with 2 updates
  ([#93](https://github.com/marcinpsk/netbox-kea/pull/93),
  [`edd89d4`](https://github.com/marcinpsk/netbox-kea/commit/edd89d4f0598eccf83df05bd851dab63570edc1f))

- **deps**: Bump the uv group across 1 directory with 3 updates
  ([#92](https://github.com/marcinpsk/netbox-kea/pull/92),
  [`bfb2e7c`](https://github.com/marcinpsk/netbox-kea/commit/bfb2e7c60d7c434ab584a45bcead208734e1050b))

- **deps-dev**: Bump pytest from 9.0.3 to 9.1.1
  ([#91](https://github.com/marcinpsk/netbox-kea/pull/91),
  [`cbf2c2e`](https://github.com/marcinpsk/netbox-kea/commit/cbf2c2ef0de4a69968fa793633393f14bbda40e7))

- **deps-dev**: Bump pytest-cov from 7.0.0 to 7.1.0
  ([#90](https://github.com/marcinpsk/netbox-kea/pull/90),
  [`ec3e23c`](https://github.com/marcinpsk/netbox-kea/commit/ec3e23c02dcd395bfc33a214e0b78066eb1f85ed))

- **deps-dev**: Bump pytest-playwright from 0.7.2 to 0.8.0
  ([#88](https://github.com/marcinpsk/netbox-kea/pull/88),
  [`f9d3bd4`](https://github.com/marcinpsk/netbox-kea/commit/f9d3bd41118abaad8034e7ceeae1a6e5fed57efd))

- **deps-dev**: Update django-stubs[compatible-mypy] requirement
  ([#87](https://github.com/marcinpsk/netbox-kea/pull/87),
  [`4b11de3`](https://github.com/marcinpsk/netbox-kea/commit/4b11de37a424ef7020f473999d551d63f60d0792))

### Continuous Integration

- Kea 3.0+ support — reframe Control Agent, test against Kea 3.0.3
  ([#95](https://github.com/marcinpsk/netbox-kea/pull/95),
  [`61b05bc`](https://github.com/marcinpsk/netbox-kea/commit/61b05bc6b581dc95eabf6cc951f4664e6a97a6b6))

- Streamline NetBox matrix (floor/ceiling/dev) + pin driver Python
  ([#94](https://github.com/marcinpsk/netbox-kea/pull/94),
  [`575b32d`](https://github.com/marcinpsk/netbox-kea/commit/575b32d1be6af20c5442c74b873b1b8d31a69b58))

### Features

- Add support for DDNS Qualifying Suffix on subnets
  ([#83](https://github.com/marcinpsk/netbox-kea/pull/83),
  [`36cf684`](https://github.com/marcinpsk/netbox-kea/commit/36cf684eb00fe1fa55fa08715953936c048f1049))

### Testing

- Stabilize two flaky Playwright UI tests ([#96](https://github.com/marcinpsk/netbox-kea/pull/96),
  [`bca241c`](https://github.com/marcinpsk/netbox-kea/commit/bca241c8ffe4842e441988731a9af4a3a02e38e9))


## v1.5.2 (2026-07-21)

### Bug Fixes

- Allow auto-id to work when there's empty subnets
  ([#84](https://github.com/marcinpsk/netbox-kea/pull/84),
  [`6884a53`](https://github.com/marcinpsk/netbox-kea/commit/6884a53f1a5f2791938b780e39422ab161d15ad0))

### Chores

- Add CODEOWNERS to auto-request @marcinpsk on PRs
  ([`1f14e9f`](https://github.com/marcinpsk/netbox-kea/commit/1f14e9fdb7dd1f5ecc5ecd683adcef37f4d5e3d4))

### Continuous Integration

- Ruff bandit (S) rules + custom opengrep ruleset (on top of CodeRabbit)
  ([#82](https://github.com/marcinpsk/netbox-kea/pull/82),
  [`1f9d986`](https://github.com/marcinpsk/netbox-kea/commit/1f9d9863930ecc17dead44f04867632369f477e3))


## v1.5.1 (2026-06-22)

### Bug Fixes

- **combined**: Validate Kea response shape before indexing resp[0]
  ([#81](https://github.com/marcinpsk/netbox-kea/pull/81),
  [`ac12a2e`](https://github.com/marcinpsk/netbox-kea/commit/ac12a2e3488c5131092f4aefc4389403effedc05))


## v1.5.0 (2026-06-20)

### Features

- Dhcp plugin import ([#80](https://github.com/marcinpsk/netbox-kea/pull/80),
  [`1e6a8d9`](https://github.com/marcinpsk/netbox-kea/commit/1e6a8d9242e65b985d081242b1a88bc9d2a4782c))


## v1.4.5 (2026-06-18)

### Bug Fixes

- Ip mask / sync guards ([#79](https://github.com/marcinpsk/netbox-kea/pull/79),
  [`52637b0`](https://github.com/marcinpsk/netbox-kea/commit/52637b055e1d0a6a89b9ecae14ad0172063b8df2))


## v1.4.4 (2026-05-30)

### Bug Fixes

- Move ghost-job self-heal out of ready(); migrate deprecated querystring tag
  ([#70](https://github.com/marcinpsk/netbox-kea/pull/70),
  [`9f651f6`](https://github.com/marcinpsk/netbox-kea/commit/9f651f6ca04c3f20913ebe616c1963468be8ab70))

### Chores

- Update envrc example and test infrastructure configuration
  ([#69](https://github.com/marcinpsk/netbox-kea/pull/69),
  [`1a73b87`](https://github.com/marcinpsk/netbox-kea/commit/1a73b87685d795e890cdeb91351e0173ea79c07b))

### Documentation

- Update copilot instructions, CLAUDE.md, README; add .envrc.example; freeze uv.lock
  ([#68](https://github.com/marcinpsk/netbox-kea/pull/68),
  [`f83d9d1`](https://github.com/marcinpsk/netbox-kea/commit/f83d9d1e80b2d38dfefa992d612e3075a4bb8053))


## v1.4.3 (2026-05-14)

### Bug Fixes

- Self-heal ghost scheduled-job DB records on worker startup
  ([#67](https://github.com/marcinpsk/netbox-kea/pull/67),
  [`0457b3b`](https://github.com/marcinpsk/netbox-kea/commit/0457b3b71e12f63a25d2cd6472e73c91fd715f38))


## v1.4.2 (2026-05-13)

### Bug Fixes

- Use condition= instead of deprecated check= in CheckConstraint migration
  ([#66](https://github.com/marcinpsk/netbox-kea/pull/66),
  [`56f74d5`](https://github.com/marcinpsk/netbox-kea/commit/56f74d5ff101d8d89b23ad6fa9b94c47fa36e533))


## v1.4.1 (2026-05-08)

### Bug Fixes

- Sync prefixes ranges ([#63](https://github.com/marcinpsk/netbox-kea/pull/63),
  [`b44c78b`](https://github.com/marcinpsk/netbox-kea/commit/b44c78b21adfebde83437cf44ce83b026e9c5e76))


## v1.4.0 (2026-05-01)

### Features

- Sync Kea subnets to NetBox Prefixes and pools to IPRanges
  ([#62](https://github.com/marcinpsk/netbox-kea/pull/62),
  [`473f15f`](https://github.com/marcinpsk/netbox-kea/commit/473f15f66c5246ddf6ca1575d1612fa20a30adf3))


## v1.3.3 (2026-04-10)

### Bug Fixes

- Remove DB query from AppConfig.ready() ([#60](https://github.com/marcinpsk/netbox-kea/pull/60),
  [`2e3ed06`](https://github.com/marcinpsk/netbox-kea/commit/2e3ed066d5d82183abf71ca83f924a156433af4b))


## v1.3.2 (2026-04-09)

### Bug Fixes

- Sync __version__ in __init__.py on semantic-release version bumps
  ([#58](https://github.com/marcinpsk/netbox-kea/pull/58),
  [`8cf7668`](https://github.com/marcinpsk/netbox-kea/commit/8cf76689a02c318f43363586c776fe1a41ad54a6))


## v1.3.1 (2026-04-09)

### Bug Fixes

- Per protocol credentials ([#59](https://github.com/marcinpsk/netbox-kea/pull/59),
  [`1691321`](https://github.com/marcinpsk/netbox-kea/commit/16913213e2edbefa8d55a35872d278c061090fc2))


## v1.3.0 (2026-04-07)

### Features

- Per protocol credentials ([#57](https://github.com/marcinpsk/netbox-kea/pull/57),
  [`5ba79ec`](https://github.com/marcinpsk/netbox-kea/commit/5ba79ec62798dc33db5a0de6aab3e16107839ebe))


## v1.2.0 (2026-04-05)

### Features

- Rq jobs
  ([`ee51a78`](https://github.com/marcinpsk/netbox-kea/commit/ee51a78ce2300c00398e3dfb26e7988b94a44c52))


## v1.1.1 (2026-03-24)

### Bug Fixes

- Hide server password in change log
  ([`0e0ce2e`](https://github.com/marcinpsk/netbox-kea/commit/0e0ce2e601cecc79762c1dda1afa509fb860347c))


## v1.1.0 (2026-03-23)

### Features

- Add several enhancements ([#1](https://github.com/marcinpsk/netbox-kea/pull/1),
  [`8f7d6a1`](https://github.com/marcinpsk/netbox-kea/commit/8f7d6a154842cb8843816cfb1448ccbe7c997050))


## v1.0.4 (2026-02-10)


## v1.0.3 (2025-09-14)


## v1.0.2 (2025-07-03)


## v1.0.1 (2024-09-10)


## v1.0.0 (2024-05-23)


## v0.2.0 (2023-08-22)


## v0.1.1 (2023-06-21)


## v0.1.0 (2023-05-17)

- Initial Release

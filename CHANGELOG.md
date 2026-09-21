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

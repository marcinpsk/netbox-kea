# NetBox Kea Plugin - Copilot Instructions

## Project Overview

**NetBox Kea** is a NetBox plugin that integrates Kea DHCP server management into NetBox. It allows viewing Kea daemon status, DHCP leases, and subnets directly from the NetBox UI, with bidirectional linking between NetBox devices/VMs and DHCP leases.

- **Repository**: `netbox-kea` (clone from GitHub)
- **Language**: Python 3.10+
- **Framework**: Django (via NetBox plugin framework)
- **Testing**: pytest with Playwright for UI tests
- **Package Manager**: uv (with pip fallback)

---

## 1. Build/Test/Lint Commands

### Build & Package
```bash
# Build distribution package
uv build

# Install development dependencies
uv sync
```

### Linting & Formatting
```bash
# Check code with ruff (linter)
uv run ruff check

# Check formatting with ruff
uv run ruff format --check

# Auto-fix linting issues
uv run ruff check --fix

# Auto-format code
uv run ruff format
```

### Testing

#### Run All Tests
```bash
# Requires Docker and Docker Compose (full integration environment)
# This runs the test_setup.sh which builds Docker containers
./tests/test_setup.sh
uv run pytest --tracing=retain-on-failure -v
```

#### Run Specific Test File
```bash
# API tests only
uv run pytest tests/test_netbox_kea_api_server.py -v

# UI tests only
uv run pytest tests/test_ui.py -v
```

#### Run Single Test Function
```bash
# Run a specific test function
uv run pytest tests/test_netbox_kea_api_server.py::test_server_api_add_delete -v

# Run tests matching a pattern
uv run pytest -k "test_server" -v
```

#### CI/CD Workflow
The GitHub Actions CI pipeline (`.github/workflows/ci.yml`) runs:
1. **Lint job**: Ruff linting and formatting checks
2. **Test job**: Runs against multiple NetBox versions (v4.0, v4.1, v4.2, v4.3, v4.4, v4.5)
   - Spins up Docker containers (NetBox + Kea)
   - Installs Playwright browsers
   - Runs pytest with trace retention for debugging

### Test Dependencies (from pyproject.toml)
- `pytest>=8.0.0,<10.0.0` - Test framework
- `pytest-playwright>=0.6.0,<0.8.0` - Playwright integration
- `pynetbox>=7.3.0,<7.7.0` - NetBox API client
- `ruff>=0.8.0` - Linter/formatter
- `mypy>=1.14.0,<1.20.0` - Type checker
- `django-stubs[compatible-mypy]>=5.0.0,<6.0.0` - Django type hints

---

## 2. Architecture & Data Flow

### Model Layer
**Location**: `netbox_kea/models.py`

**Server Model** (only model in this plugin):
```python
class Server(NetBoxModel):  # Extends NetBox's NetBoxModel base class
    name: str (unique)
    server_url: str
    username: str | None
    password: str | None
    ssl_verify: bool
    client_cert_path: str | None
    client_key_path: str | None
    ca_file_path: str | None
    dhcp6: bool
    dhcp4: bool
    tags: TaggableManager (NetBox standard)
```

Uses custom validation (`clean()` method):
- Ensures at least one DHCP version (v4 or v6) is enabled
- Validates certificate/key pair usage
- Validates file paths exist on disk
- Tests connectivity to Kea Control Agent at save time

### Kea Client Integration
**Location**: `netbox_kea/kea.py`

**KeaClient Class**: Wraps HTTP requests to Kea's Control Agent
- Manages HTTP sessions with auth (basic, cert-based)
- Handles SSL verification (bool or CA file path)
- Sends JSON-RPC commands to Kea (`command()` method)
- Returns `KeaResponse` (TypedDict with `result`, `arguments`, `text`)
- Raises `KeaException` for non-zero result codes

**Integration Pattern**: Each Server model has a `get_client()` method
```python
def get_client(self) -> KeaClient:
    return KeaClient(
        url=self.server_url,
        username=self.username,
        password=self.password,
        verify=self.ca_file_path or self.ssl_verify,
        client_cert=self.client_cert_path or None,
        client_key=self.client_key_path or None,
        timeout=settings.PLUGINS_CONFIG["netbox_kea"]["kea_timeout"],
    )
```

### View Architecture
**Location**: `netbox_kea/views.py`

**View Hierarchy**:
1. **Server Management Views** (Model CRUD):
   - `ServerView` extends `generic.ObjectView` - Detail view
   - `ServerEditView` extends `generic.ObjectEditView` - Form-based edit
   - `ServerDeleteView` extends `generic.ObjectDeleteView` - Delete
   - `ServerListView` extends `generic.ObjectListView` - List with filtering/pagination
   - `ServerBulkDeleteView` extends `generic.BulkDeleteView` - Bulk delete

2. **Server Status View**:
   - `ServerStatusView` (registered as model view "status" tab)
   - Queries Kea via `client.command()` for:
     - Control Agent status (`status-get`)
     - Kea version (`version-get`)
     - DHCP daemon status (DHCPv4, DHCPv6)
     - HA (High Availability) status if enabled
   - Passes status dict to template `netbox_kea/server_status.html`

3. **DHCP Leases Views** (Non-model children):
   - `BaseServerLeasesView` - Generic base for lease listing
   - `ServerLeases4View` & `ServerLeases6View` - DHCPv4/v6 specific
   - Features:
     - **Search by**: IP, hostname, MAC (v4)/DUID (v6), subnet, subnet ID
     - **HTMX-enabled**: Return partial HTML for form submissions
     - **Pagination**: Only for subnet-based searches (Kea limitation)
     - **Export**: CSV export via custom `export_table()` utility
   - Template: `netbox_kea/server_dhcp_leases.html` (full page)
   - HTMX template: `netbox_kea/server_dhcp_leases_htmx.html` (partial)

4. **DHCP Subnets Views**:
   - `BaseServerDHCPSubnetsView` extends `generic.ObjectChildrenView`
   - `ServerDHCP4SubnetsView` & `ServerDHCP6SubnetsView`
   - Fetches subnets via `config-get` command (expensive, gets full config)
   - Shows shared networks and subnet linking
   - Supports table export

5. **Lease Deletion Views**:
   - `BaseServerLeasesDeleteView` - Abstract base
   - `ServerLeases4DeleteView` & `ServerLeases6DeleteView`
   - POST handler bulk deletes leases via `lease4-del` / `lease6-del` commands
   - Uses fake model (`FakeLeaseModel`) to reuse NetBox's `bulk_delete.html` template

### Form Layer
**Location**: `netbox_kea/forms.py`

**Form Classes**:
- `ServerForm` extends `NetBoxModelForm` - Server CRUD form with custom password widget
- `ServerFilterForm` extends `NetBoxModelFilterSetForm` - Filterset form with DHCP toggles
- `BaseLeasesSarchForm` - Base for lease search (validates CIDR, hex strings, IPs)
  - `Leases4SearchForm` - DHCPv4: IP, hostname, MAC, client ID, subnet
  - `Leases6SearchForm` - DHCPv6: IP, hostname, DUID, subnet
- `BaseLeaseDeleteForm` - Delete form with fake `pk` field (IPs as strings)
  - `Lease4DeleteForm` & `Lease6DeleteForm`
- `MultipleIPField` - Custom field for validating multiple IP addresses
- `VeryHiddenInput` - Custom widget that renders as empty string (bypass form validation)

### Table Layer
**Location**: `netbox_kea/tables.py`

**Table Classes**:
- `ServerTable` extends `NetBoxTable` - Shows servers with linkifiable name
- `GenericTable` extends `BaseTable` - Non-model base (doesn't require model)
  - `SubnetTable` - Shows subnets with actions dropdown, linkified to leases search
  - `BaseLeaseTable` - Non-model lease table with custom columns
    - `LeaseTable4` - Adds client_id column
    - `LeaseTable6` - Adds type, duid, iaid, preferred_lft columns
  - `LeaseDeleteTable` - IP addresses for delete confirmation

**Custom Columns**:
- `DurationColumn` - Formats seconds as HH:MM:SS
- `ActionsColumn` - Renders dropdown action menus (HTML templates)
- `MonospaceColumn` - Monospace font for HW addresses, DUIDs

**Notable Pattern**: Lease tables use `pk` column with `ToggleColumn` (checkboxes) but accessor="ip_address" to show IPs in checkboxes

### Filterset Layer
**Location**: `netbox_kea/filtersets.py`

**ServerFilterSet** extends `NetBoxModelFilterSet`
- Filters by: id, name, server_url, dhcp4, dhcp6

### API Layer
**REST API** (`netbox_kea/api/`):
- `ServerViewSet` extends `NetBoxModelViewSet` - Auto-generates CRUD endpoints
  - Registered via `NetBoxRouter` in `urls.py`
  - Endpoint: `/api/plugins/netbox-kea/servers/`
- `ServerSerializer` extends `NetBoxModelSerializer`
  - Brief fields: id, url, name, server_url
  - Full fields: All server fields + tags, last_updated, display
  - Password marked as write-only (not shown in responses)

**GraphQL** (`netbox_kea/graphql.py`):
- `ServerType` strawberry-django type (wraps Server model)
  - Fields exposed: id, name, server_url, username, ssl_verify, cert paths, dhcp flags
  - Password intentionally NOT exposed
- `Query` root with:
  - `server(id: int)` - Get single server
  - `server_list` - Get all servers

### Data Flow: From URL to Response

**Example: View Server Status**
```
1. URL: /plugins/kea/servers/1/status/ (ServerStatusView)
2. URL Router (urls.py): Maps to registered model view "status"
3. View Layer:
   - ServerStatusView.get_extra_context() called
   - Creates KeaClient via instance.get_client()
4. Kea Client:
   - Sends: {"command": "status-get"}
   - Sends: {"command": "version-get"}
   - Sends: {"command": "status-get", "service": ["dhcp4", "dhcp6"]}
5. Template Rendering:
   - server_status.html receives statuses dict
   - Displays Control Agent, DHCP v4/v6, HA status
```

**Example: Search DHCP Leases**
```
1. URL: /plugins/kea/servers/1/leases4/?q=192.168.1.1&by=ip
2. View: ServerLeases4View.get()
3. Request handling:
   - If request.htmx (HTMX request):
     - Form validation
     - Call get_leases() or get_leases_page()
     - Render HTMX partial (server_dhcp_leases_htmx.html)
   - Otherwise:
     - Render full page with empty table
4. Kea Commands:
   - For "ip" search: {"command": "lease4-get", "arguments": {"ip-address": "192.168.1.1"}}
   - For "hw" search: {"command": "lease4-get-by-hw-address", ...}
   - For "subnet" search: {"command": "lease4-get-page", ...} with pagination
5. Lease Formatting:
   - utilities.format_leases() enriches raw Kea data:
     - Converts UNIX timestamps to datetime
     - Calculates expires_at and expires_in
     - Replaces "-" with "_" in keys (template compatibility)
6. Table Rendering:
   - LeaseTable4 configured with request
   - Renders HTML with action dropdown menus
```

### Navigation & URL Structure
**Location**: `netbox_kea/navigation.py` and `netbox_kea/urls.py`

**Menu Integration**:
```python
menu_items = (
    PluginMenuItem(
        link="plugins:netbox_kea:server_list",
        link_text="Servers",
        permissions=["netbox_kea.view_server"],
        buttons=(
            PluginMenuButton(..., link="plugins:netbox_kea:server_add", ...),
        ),
    ),
)
```

**URL Patterns** (in urls.py):
```
/servers/              -> ServerListView
/servers/add/          -> ServerEditView (create)
/servers/<id>/         -> ServerView + child views (status, leases4, leases6, subnets4, subnets6)
/servers/<id>/status/  -> ServerStatusView
/servers/<id>/leases4/ -> ServerLeases4View
/servers/<id>/leases6/ -> ServerLeases6View
```

### Plugin Configuration
**Location**: `netbox_kea/__init__.py`

```python
class NetBoxKeaConfig(PluginConfig):
    name = "netbox_kea"
    verbose_name = "Kea"
    description = "Kea integration for NetBox"
    version = "1.0.4"
    base_url = "kea"
    default_settings = {"kea_timeout": 30}  # Configurable via settings
```

---

## 3. Key Conventions

### View Structure & HTMX Integration

**Base Classes Used**:
- `generic.ObjectView` - Detail view
- `generic.ObjectEditView` - CRUD form view
- `generic.ObjectDeleteView` - Delete view
- `generic.ObjectListView` - List with filtersets
- `generic.BulkDeleteView` - Bulk operations
- `generic.ObjectChildrenView` - Display child objects (subnets)

**HTMX Pattern** (seen in BaseServerLeasesView):
```python
def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
    if not request.htmx:
        # Full page response
        return super().get(request, **kwargs)

    # HTMX partial response
    return render(request, "netbox_kea/server_dhcp_leases_htmx.html", {...})
```

Lease search form is HTMX-enabled:
- Form submission via HTMX replaces `#leases-table` div
- Partial template only renders the table and pagination
- Permission checks use `request.user.has_perm("netbox_kea.bulk_delete_lease_from_server")`

**ViewTab Registration**:
- Standard tabs: `ViewTab(label="...", weight=...)`
- Optional tabs: `OptionalViewTab(label="...", is_enabled=lambda instance: instance.dhcp6)`

### API Endpoint Structure

**REST API Pattern** (DRF + NetBox):
```python
class ServerViewSet(NetBoxModelViewSet):
    queryset = models.Server.objects.prefetch_related("tags")
    filterset_class = filtersets.ServerFilterSet
    serializer_class = ServerSerializer
```

Registered via:
```python
router = NetBoxRouter()
router.register("servers", views.ServerViewSet)
urlpatterns = router.urls
```

**Endpoints Generated**:
- `GET /api/plugins/netbox-kea/servers/` - List
- `POST /api/plugins/netbox-kea/servers/` - Create
- `GET /api/plugins/netbox-kea/servers/{id}/` - Detail
- `PATCH /api/plugins/netbox-kea/servers/{id}/` - Partial update
- `DELETE /api/plugins/netbox-kea/servers/{id}/` - Delete

**Serializer Features**:
- `HyperlinkedIdentityField` for self-links
- `write_only=True` for password field (never returned)
- `display` field (NetBox standard for human-readable representation)

### Custom Table Columns

**Pattern Examples**:

```python
# Duration formatting
class DurationColumn(tables.Column):
    def render(self, value: int):
        return format_duration(value)  # Returns "HH:MM:SS"

# Template-based actions
class ActionsColumn(tables.TemplateColumn):
    def __init__(self, template: str):
        super().__init__(
            template,
            attrs={"td": {"class": "text-end text-nowrap noprint"}},
            verbose_name="",
        )

# Monospace styling
class MonospaceColumn(tables.Column):
    def __init__(self, *args, additional_classes: list[str] | None = None, **kwargs):
        cls_str = "font-monospace"
        if additional_classes:
            cls_str += " " + " ".join(additional_classes)
        super().__init__(*args, attrs={"td": {"class": cls_str}}, **kwargs)

# GenericTable (non-model)
class SubnetTable(GenericTable):
    id = tables.Column(verbose_name="ID")
    subnet = tables.Column(linkify=lambda record, table: ...)  # Dynamic URLs
    shared_network = tables.Column(verbose_name="Shared Network")
    actions = ActionsColumn(SUBNET_ACTIONS)  # HTML template with dropdown
```

**Non-Model Table Pattern**:
- Inherit from `BaseTable` instead of `NetBoxTable`
- Implement `@property objects_count` (used for pagination display)
- Manually pass dicts/objects as data (not ORM querysets)

### Form Structure

**NetBox Form Base Classes**:
- `NetBoxModelForm` - For model CRUD
- `NetBoxModelFilterSetForm` - For filterset forms
- Custom `forms.Form` - For searches/actions

**Validation Pattern** (in BaseLeasesSarchForm):
```python
def clean(self) -> dict[str, Any] | None:
    cleaned_data = super().clean()
    q = cleaned_data.get("q")
    by = cleaned_data.get("by")

    # Mutual validation
    if q and not by:
        raise ValidationError({"by": "..."})

    # Type-specific validation
    if by == constants.BY_SUBNET:
        net = IPNetwork(q, version=self.Meta.ip_version)
        # ... validate CIDR notation
        cleaned_data["q"] = net

    elif by == constants.BY_HW_ADDRESS:
        cleaned_data["q"] = str(EUI(q, version=48, dialect=mac_unix_expanded))

    return cleaned_data
```

**Custom Field Pattern** (MultipleIPField):
```python
class MultipleIPField(forms.MultipleChoiceField):
    def __init__(self, version: Literal[6, 4], *args, **kwargs):
        self._version = version
        super().__init__(*args, widget=forms.MultipleHiddenInput, **kwargs)

    def clean(self, value: Any) -> Any:
        if not isinstance(value, list):
            raise forms.ValidationError(...)
        return [str(IPAddress(ip, version=self._version)) for ip in value]
```

### Migration Structure

**Location**: `netbox_kea/migrations/0001_initial.py`

**Dependencies**:
- Depends on NetBox's `extras` app migration `0092_delete_jobresult`
- Uses Django's migration framework
- Ruff excludes migrations from linting

**Model Creation**:
```python
class Migration(migrations.Migration):
    initial = True
    dependencies = [('extras', '0092_delete_jobresult')]

    operations = [
        migrations.CreateModel(
            name='Server',
            fields=[
                ('id', models.BigAutoField(...)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('last_updated', models.DateTimeField(auto_now=True)),
                ('custom_field_data', models.JSONField(...)),
                ('name', models.CharField(unique=True, max_length=255)),
                # ... other fields
                ('tags', taggit.managers.TaggableManager(...)),
            ],
            options={'abstract': False},
        ),
    ]
```

**NetBox Base Model Features** (inherited via `NetBoxModel`):
- Automatic `id`, `created`, `last_updated` fields
- `custom_field_data` (JSON) for NetBox's custom fields
- TaggableManager for tags
- Permissions automatically created (view_server, add_server, change_server, delete_server, bulk_delete_server)

### Unusual Patterns

1. **Non-Model Tables**: Uses `GenericTable` base class for subnets and leases (not stored in DB)
2. **Fake Model for Bulk Delete**: `FakeLeaseModel` mimics Django model interface to reuse NetBox templates
3. **Generic View with TypeVar**: `BaseServerLeasesView(Generic[T])` - parameterized view for table type
4. **OptionalViewTab**: Custom ViewTab that conditionally renders based on lambda (e.g., only show v6 tab if DHCPv6 enabled)
5. **Lease Data Enrichment**: Raw Kea responses transformed via `format_leases()` utility:
   - Key name normalization (`"ip-address"` → `"ip_address"`)
   - Timestamp conversion (UNIX seconds → datetime)
   - Derived calculations (`expires_in = expires_at - now`)

### GraphQL Integration

**Type Definition**:
```python
@strawberry_django.type(
    models.Server,
    fields=(
        "id", "name", "server_url", "username", "ssl_verify",
        "client_cert_path", "client_key_path", "ca_file_path",
        "dhcp6", "dhcp4",
    ),
)
class ServerType(NetBoxObjectType):
    pass  # Inherits from NetBox's NetBoxObjectType
```

**Query Root**:
```python
@strawberry.type
class Query:
    @strawberry.field
    def server(self, id: int) -> ServerType:
        return models.Server.objects.get(pk=id)

    server_list: list[ServerType] = strawberry_django.field()
```

**Schema Registration**:
```python
schema = [Query]  # Returned from plugin's graphql module
```

**Notable**: Password field is explicitly excluded from `fields` tuple (security)

---

## 4. Testing Setup

### Test Framework
- **pytest** >= 8.0.0 - Test runner
- **pytest-playwright** >= 0.6.0 - Browser automation
- **pynetbox** >= 7.3.0 - NetBox API client for test setup

### Test Directory Structure
```
tests/
├── __init__.py
├── conftest.py              # Session-level fixtures
├── constants.py             # Test constants (same as netbox_kea/constants.py)
├── kea.py                   # KeaClient copy (symlink to avoid import issues)
├── test_setup.sh            # Docker environment setup
├── test_netbox_kea_api_server.py  # API tests (~293 lines)
├── test_ui.py               # UI tests (~1595 lines, uses Playwright)
└── docker/
    ├── Dockerfile           # NetBox container with plugin
    ├── Dockerfile-kea       # Kea DHCP container
    ├── docker-compose.yml   # Full test stack
    ├── docker-compose.override.yml
    ├── plugins.py           # Plugin config (PLUGINS = ["netbox_kea"])
    ├── nginx.conf           # Reverse proxy for HTTPS testing
    ├── htpasswd             # Basic auth credentials
    ├── kea_configs/         # Kea DHCPv4/v6 configs
    └── certs/               # Generated SSL certs

```

### Test Fixtures (conftest.py)

**Session-Level Fixtures**:
- `netbox_url()` - "http://localhost:8000"
- `netbox_token()` - Admin token (v1 or v2 based on NetBox version)
- `netbox_username()` - "admin"
- `netbox_password()` - "admin"
- `kea_url()` - "http://kea-ctrl-agent:8000"
- `nb_http()` - requests.Session with auth headers
- `nb_api()` - pynetbox.api instance (auto-clears servers)

**Kea Connection Fixtures**:
- `kea_basic_url()` - "http://nginx" (basic auth testing)
- `kea_basic_username()` / `kea_basic_password()` - "kea"
- `kea_https_url()` - "https://nginx"
- `kea_cert_url()` - "https://nginx:444" (client cert testing)
- `kea_client_cert()` - "/certs/netbox.crt"
- `kea_client_key()` - "/certs/netbox.key"
- `kea_ca()` - "/certs/nginx.crt"

### Test Fixtures (test_ui.py)

**Autouse Fixtures** (run for every test):
- `clear_leases(kea_client)` - Wipes all leases before each test
- `reset_user_preferences()` - Resets table configs and pagination

**Server Setup Fixtures**:
- `with_test_server` - Creates test server (both DHCPv4+v6), navigates to detail page
- `with_test_server_only6` - DHCPv6-only server
- `with_test_server_only4` - DHCPv4-only server

**Data Fixtures**:
- `kea_client()` - KeaClient("http://localhost:8001") for direct Kea commands
- `lease6()` - Creates and returns a DHCPv6 lease object
- `lease6_netbox_device()` - Creates matching NetBox device for lease6
- Similar fixtures for v4 leases

**Page/UI Fixtures**:
- `page` - Playwright Page object (provided by pytest-playwright)
- `netbox_login` - Auto-logs in to NetBox
- `plugin_base` - Base URL for plugin pages

**Session Fixtures** (NetBox setup):
- `test_tag()` - Creates a tag
- `test_site()` - Creates a site
- `test_device_type()` - Creates device type
- `test_device_role()` - Creates device role
- `test_cluster()` - Creates a cluster

### Running Tests

**Full Integration Test** (requires Docker):
```bash
./tests/test_setup.sh  # Generates certs, builds/starts containers
uv run pytest --tracing=retain-on-failure -v
```

**API Tests Only**:
```bash
uv run pytest tests/test_netbox_kea_api_server.py -v
```

**Single Test Function**:
```bash
uv run pytest tests/test_ui.py::test_server_add_delete -v
```

**With Specific Marker**:
```bash
uv run pytest -m "parametrize" -v
```

### Test Categories

**API Tests** (test_netbox_kea_api_server.py):
- CRUD operations (create, update, delete)
- Bulk operations
- GraphQL queries
- Error handling (missing cert, invalid paths, auth failures)
- SSL/HTTPS variations (basic auth, client certs, CA, insecure)
- DHCPv4/v6 validation
- ~50+ parametrized test cases

**UI Tests** (test_ui.py):
- Navigation (menu visibility, permissions)
- Server CRUD via web form
- Server status view
- DHCP subnet listing and export
- DHCP lease searching (by IP, hostname, MAC/DUID, subnet)
- Lease deletion
- Table column configuration
- Pagination
- Permission checks
- Uses Playwright for browser automation
- ~1595 lines of tests

### Playwright Configuration

**Test Tracing**:
```bash
uv run pytest --tracing=retain-on-failure -v
```
- Traces saved to `test-results/` (uploaded as artifact in CI)
- Useful for debugging UI test failures

**Playwright Installation** (CI):
```bash
uv run playwright install --with-deps
```
- Installs browser binaries and system dependencies

### Docker Test Environment

**Services** (docker-compose.yml):
1. **netbox** - NetBox 4.x container with plugin mounted
2. **postgres** - PostgreSQL database
3. **redis** - Cache/queue
4. **nginx** - Reverse proxy (HTTPS, basic auth)
5. **kea-dhcp4** - Kea DHCPv4 server
6. **kea-dhcp6** - Kea DHCPv6 server
7. **kea-ctrl-agent** - Kea Control Agent (JSON-RPC)

**Setup Process** (test_setup.sh):
```bash
# 1. Generate SSL certs (client and server)
openssl req ... -out certs/netbox.crt  # Client cert
openssl req ... -out certs/nginx.crt   # Server cert

# 2. Copy wheel to docker/
WHL_FILE=$(ls ./dist/ | grep .whl)
cp "./dist/$WHL_FILE" ./tests/docker/

# 3. Build and start containers
docker compose build --build-arg "FROM=netboxcommunity/netbox:$NETBOX_CONTAINER_TAG"
docker compose up -d
```

---

## 5. NetBox Plugin Conventions

### Plugin Registration

**Base Class**:
```python
from netbox.plugins import PluginConfig

class NetBoxKeaConfig(PluginConfig):
    name = "netbox_kea"
    verbose_name = "Kea"
    base_url = "kea"
    version = "1.0.4"
    default_settings = {"kea_timeout": 30}

config = NetBoxKeaConfig  # Must be exported as 'config'
```

**Installation**:
1. Add to `local_requirements.txt`: `netbox-kea`
2. Enable in `configuration.py`: `PLUGINS = ["netbox_kea"]`
3. Run migrations: `./manage.py migrate`

### Model Base Classes

**NetBoxModel** (extends Django Model):
- Provides: `id`, `created`, `last_updated`, `custom_field_data`
- Automatically creates permissions: view, add, change, delete, bulk_delete
- Integrates with NetBox's custom fields and tags

### View Base Classes

**From `netbox.views.generic`**:
- `ObjectView` - Read-only detail view
- `ObjectEditView` - Create/update form view
- `ObjectDeleteView` - Delete view
- `ObjectListView` - List with filterset
- `BulkDeleteView` - Bulk operations
- `ObjectChildrenView` - Display related objects

**Decorators**:
- `@register_model_view(Model)` - Register detail views
- `@register_model_view(Model, "action")` - Register custom actions/tabs

### Mixin Classes

**From `utilities.views`**:
- `GetReturnURLMixin` - Handle return_url parameter
- `ViewTab` / `OptionalViewTab` - Register tabbed views

**Usage**:
```python
@register_model_view(Server, "leases4")
class ServerLeases4View(BaseServerLeasesView):
    tab = OptionalViewTab(
        label="DHCPv4 Leases",
        weight=1020,
        is_enabled=lambda s: s.dhcp4
    )
```

### Filter Classes

**NetBoxModelFilterSet**:
```python
from netbox.filtersets import NetBoxModelFilterSet

class ServerFilterSet(NetBoxModelFilterSet):
    class Meta:
        model = Server
        fields = ("id", "name", "server_url", "dhcp4", "dhcp6")
```

### Form Classes

**NetBoxModelForm**:
```python
from netbox.forms import NetBoxModelForm

class ServerForm(NetBoxModelForm):
    class Meta:
        model = Server
        fields = ("name", "server_url", ...)
        widgets = {"password": forms.PasswordInput()}
```

**NetBoxModelFilterSetForm**:
```python
from netbox.forms import NetBoxModelFilterSetForm

class ServerFilterForm(NetBoxModelFilterSetForm):
    model = Server
    tag = TagFilterField(model)
    dhcp4 = forms.NullBooleanField(...)
```

### Table Classes

**NetBoxTable** (for models):
```python
from netbox.tables import NetBoxTable, BooleanColumn

class ServerTable(NetBoxTable):
    name = tables.Column(linkify=True)
    dhcp6 = BooleanColumn()

    class Meta(NetBoxTable.Meta):
        model = Server
        fields = ("pk", "name", "server_url", "dhcp6", "dhcp4")
        default_columns = ("pk", "name", "server_url", "dhcp6", "dhcp4")
```

**BaseTable** (for non-model data):
```python
from netbox.tables import BaseTable

class GenericTable(BaseTable):
    exempt_columns = ("actions", "pk")

    class Meta(BaseTable.Meta):
        empty_text = "No rows"
        fields: tuple[str, ...] = ()

    @property
    def objects_count(self):
        return len(self.data)
```

### API Classes

**NetBoxModelViewSet**:
```python
from netbox.api.viewsets import NetBoxModelViewSet

class ServerViewSet(NetBoxModelViewSet):
    queryset = models.Server.objects.prefetch_related("tags")
    filterset_class = filtersets.ServerFilterSet
    serializer_class = ServerSerializer
```

**NetBoxModelSerializer**:
```python
from netbox.api.serializers import NetBoxModelSerializer

class ServerSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_kea-api:server-detail"
    )

    class Meta:
        model = Server
        fields = ("id", "name", "url", "display", "tags", "last_updated")
        brief_fields = ("id", "url", "name")
        extra_kwargs = {"password": {"write_only": True}}
```

**Router**:
```python
from netbox.api.routers import NetBoxRouter

router = NetBoxRouter()
router.register("servers", views.ServerViewSet)
urlpatterns = router.urls
```

### Menu Integration

**PluginMenuItem** and **PluginMenuButton**:
```python
from netbox.plugins import PluginMenuItem, PluginMenuButton

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_kea:server_list",
        link_text="Servers",
        permissions=["netbox_kea.view_server"],
        buttons=(
            PluginMenuButton(
                link="plugins:netbox_kea:server_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
                permissions=["netbox_kea.add_server"],
            ),
        ),
    ),
)
```

### GraphQL Integration

**NetBoxObjectType**:
```python
from netbox.graphql.types import NetBoxObjectType
import strawberry_django

@strawberry_django.type(models.Server, fields=(...))
class ServerType(NetBoxObjectType):
    pass

@strawberry.type
class Query:
    @strawberry.field
    def server(self, id: int) -> ServerType:
        return models.Server.objects.get(pk=id)

    server_list: list[ServerType] = strawberry_django.field()

schema = [Query]  # Exported from graphql module
```

---

## 6. CI/CD Pipeline

### GitHub Actions Workflows

**Location**: `.github/workflows/`

#### CI Workflow (`ci.yml`)
**Triggers**:
- Push to any branch
- Pull requests
- Weekly schedule (Sunday 00:00 UTC)

**Jobs**:

1. **Lint Job**:
   - Runs on: `ubuntu-latest`
   - Steps:
     ```yaml
     - actions/checkout@v6
     - astral-sh/ruff-action@v3  # ruff check
     - astral-sh/ruff-action@v3 --args format --check  # ruff format --check
     ```

2. **Test Job** (matrix):
   - Runs against NetBox versions: v4.0, v4.1, v4.2, v4.3, v4.4, v4.5
   - Steps:
     ```yaml
     - actions/checkout@v6
     - astral-sh/setup-uv@v7 (enable-cache: true)
     - actions/setup-python@v6 (python-version-file: pyproject.toml)
     - uv run playwright install --with-deps
     - uv build
     - ./tests/test_setup.sh (NETBOX_CONTAINER_TAG: matrix.netbox)
     - uv run pytest --tracing=retain-on-failure -v
     - actions/upload-artifact@v7 (playwright-traces)
     - docker compose logs (on failure)
     ```

#### Release Workflow (`release.yml`)
**Trigger**:
- Release published

**Job**:
- Build wheel: `uv build`
- Publish to PyPI: `uv publish` (with trusted publisher auth)

### Linting Configuration (ruff)

**File**: `pyproject.toml`

```toml
[tool.ruff]
exclude = ["netbox_kea/migrations"]

[tool.ruff.lint]
select = [
    "C4",   # flake8-comprehensions
    "E",    # pycodestyle error
    "EXE",  # flake8-executable
    "F",    # pyflakes
    "I",    # isort
    "ISC",  # flake8-implicit-str-concat
    "PERF", # perflint
    "PIE",  # flake8-pie
    "PYI",  # flake8-pyi
    "UP",   # pyupgrade
    "W",    # pycodestyle warning
]
ignore = [
    "E501",  # Line too long (handled by formatter)
    # Conflicts with formatter:
    "W191", "E111", "E114", "E117",
    "D206", "D300",
    "Q000", "Q001", "Q002", "Q003",
    "COM812", "COM819", "ISC001", "ISC002",
]
```

### Build System

**Package**: `hatchling`

**pyproject.toml** (build configuration):
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.sdist]
include = ["netbox_kea"]
```

**Version**: Defined in `pyproject.toml` (1.0.4) and `__init__.py`

---

## 7. Existing AI Config Files

**None found** in the repository. No existing:
- CLAUDE.md
- AGENTS.md
- .cursorrules
- .cursor/ directory
- .windsurfrules
- CONVENTIONS.md
- AIDER_CONVENTIONS.md
- .clinerules
- .cline_rules
- .github/copilot-instructions.md

This is a new copilot-instructions.md file.

---

## 8. README/CONTRIBUTING Key Content

### From README.md

**Features**:
- Uses Kea management API (Control Agent + lease_cmds hook)
- View Kea daemon statuses (control agent, DHCPv4, DHCPv6)
- View, delete, export, search DHCP leases
- Search NetBox devices/VMs directly from DHCP leases
- View DHCP subnets from Kea configuration
- REST API and GraphQL support

**Requirements**:
- NetBox 4.0 - 4.5 (tested with v4.0, v4.1, v4.2, v4.3, v4.4, v4.5)
- Python 3.10+ (per pyproject.toml)
- Kea Control Agent (REST API)
- Kea `lease_cmds` hook library
- Tested with Kea v2.4.1 with memfile lease database

**Installation**:
1. Add `netbox-kea` to `local_requirements.txt`
2. Enable in `configuration.py`: `PLUGINS = ["netbox_kea"]`
3. Run `./manage.py migrate`

**Custom Links** (NetBox Web UI):
Examples for linking from NetBox to Kea leases:
- Prefix → DHCP leases by subnet: `/plugins/kea/servers/<ID>/leases{{ object.prefix.version }}/?q={{ object.prefix }}&by=subnet`
- Interface → DHCP leases by MAC: `/plugins/kea/servers/<ID>/leases4/?q={{ object.mac_address }}&by=hw`
- Device → DHCP leases by hostname: `/plugins/kea/servers/<ID>/leases4/?q={{ object.name|lower }}&by=hostname`

**Limitations**:
- Pagination only supported for subnet-based lease searches (Kea API limitation - forward only, no backwards)
- Subnet listing fetches full Kea config (expensive with `config-get`)

**Compatibility**:
- Tested with Kea v2.4.1 with memfile lease database
- Other versions/databases may work

---

## Common Development Tasks

### Adding a New Field to Server Model

1. **models.py**: Add field with proper verbose_name, help_text, validators
2. **migrations/**: Run `python manage.py makemigrations netbox_kea`
3. **forms.py**: Add to ServerForm.Meta.fields
4. **tables.py**: Add to ServerTable.Meta.fields
5. **filtersets.py**: Add to ServerFilterSet.Meta.fields
6. **api/serializers.py**: Add to ServerSerializer.Meta.fields
7. **graphql.py**: Add to @strawberry_django.type fields tuple
8. **Test**: Add test in test_netbox_kea_api_server.py

### Adding a New Search Parameter for Leases

1. **constants.py**: Add new BY_* constant
2. **forms.py**: Add choice to Leases4SearchForm and Leases6SearchForm
3. **forms.py**: Add validation logic in BaseLeasesSarchForm.clean()
4. **views.py**: Add handling in BaseServerLeasesView.get_leases() method
5. **utilities.py**: Add any custom formatting if needed
6. **Test**: Add test case in test_ui.py

### Debugging Tests

1. **Run with trace retention**: `pytest --tracing=retain-on-failure`
2. **Check trace**: Open `test-results/` in Playwright Inspector
3. **Run single test**: `pytest tests/test_ui.py::test_name -v -s`
4. **Check Docker logs**: `docker compose -f tests/docker/docker-compose.yml logs -f`

### Running Tests Locally Without Docker

Not easily possible due to Kea dependency. Use provided Docker setup.

---

## Key Files Quick Reference

| File | Purpose |
|------|---------|
| `netbox_kea/__init__.py` | Plugin config (PluginConfig) |
| `netbox_kea/models.py` | Server model, validation, Kea client integration |
| `netbox_kea/views.py` | All views (CRUD, status, leases, subnets) + HTMX handling |
| `netbox_kea/forms.py` | Forms for Server CRUD, lease search, deletion |
| `netbox_kea/tables.py` | Tables with custom columns (duration, actions, monospace) |
| `netbox_kea/filtersets.py` | Filterset for server filtering |
| `netbox_kea/kea.py` | KeaClient (JSON-RPC over HTTP to Kea) |
| `netbox_kea/graphql.py` | GraphQL types and schema |
| `netbox_kea/navigation.py` | Plugin menu registration |
| `netbox_kea/constants.py` | Constants (search types, regex patterns) |
| `netbox_kea/utilities.py` | Helpers (duration formatting, lease enrichment, exports) |
| `netbox_kea/api/views.py` | REST API viewset |
| `netbox_kea/api/serializers.py` | REST API serializers |
| `netbox_kea/api/urls.py` | REST API routes |
| `netbox_kea/urls.py` | Plugin URL routes |
| `netbox_kea/migrations/0001_initial.py` | Initial schema migration |
| `pyproject.toml` | Dependencies, build config, ruff config |
| `.github/workflows/ci.yml` | CI pipeline (lint + test matrix) |
| `.github/workflows/release.yml` | Release to PyPI |
| `tests/conftest.py` | Pytest fixtures (NetBox, Kea, auth) |
| `tests/test_netbox_kea_api_server.py` | API tests |
| `tests/test_ui.py` | UI/integration tests with Playwright |
| `tests/test_setup.sh` | Docker environment setup |

---

## Developer Notes

1. **Never commit secrets**: Use fixtures for API keys, passwords in tests
2. **Type hints**: Use full annotations (`from __future__ import annotations` not needed, Python 3.10+)
3. **Form validation**: Use Django's `ValidationError` with dict keys for field-specific errors
4. **Kea Error Handling**: Check result codes with `check_response()` or pass `check=()` parameter
5. **HTMX Checks**: Use `request.htmx` to detect HTMX requests (from netbox utilities)
6. **Template Names**: Follow NetBox pattern: `netbox_kea/model_action.html`
7. **URLs**: Use `reverse()` with `args=[...]` for reversing URLs with arguments
8. **Permissions**: Use `request.user.has_perm()` with permission string like `"netbox_kea.bulk_delete_lease_from_server"`
9. **Lease Data**: Always call `format_leases()` to normalize timestamps and keys before table rendering
10. **Non-Model Tables**: Use `GenericTable` and pass dicts, not querysets

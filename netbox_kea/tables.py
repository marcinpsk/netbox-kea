import django_tables2 as tables
from django.urls import reverse
from django.utils.http import urlencode
from netbox.tables import BaseTable, BooleanColumn, NetBoxTable, ToggleColumn, columns

from netbox_kea.utilities import format_duration

from .models import Server

SUBNET_ACTIONS = """<span class="btn-group dropdown">
  <a class="btn btn-sm btn-secondary dropdown-toggle" href="#" type="button" data-bs-toggle="dropdown">
  <i class="mdi mdi-magnify"></i></a>
  <ul class="dropdown-menu">
    {% if record.pk %}
    <li>
      <a href="{% url "ipam:prefix" pk=record.pk %}" class="dropdown-item">
        <i class="mdi mdi-open-in-app" aria-hidden="true" title="View prefix"></i>
        View prefix
      </a>
    </li>
    {% endif %}
    {% if record.subnet %}
    <li>
      <a href="{% url "ipam:prefix_list" %}?prefix={{ record.subnet }}" class="dropdown-item">
        <i class="mdi mdi-magnify" aria-hidden="true" title="Search for prefix"></i>
        Search for prefix
      </a>
    </li>
    {% endif %}
    {% if record.server_pk and record.id %}
    <li><hr class="dropdown-divider"></li>
    {% if record.dhcp_version == 4 %}
    <li>
      <a href="{% url "plugins:netbox_kea:server_subnet4_pool_add" record.server_pk record.id %}"
         class="dropdown-item">
        <i class="mdi mdi-plus-circle-outline" aria-hidden="true"></i>
        Add pool
      </a>
    </li>
    <li>
      <a href="{% url "plugins:netbox_kea:server_subnet4_wipe_leases" record.server_pk record.id %}"
         class="dropdown-item text-warning">
        <i class="mdi mdi-delete-sweep-outline" aria-hidden="true"></i>
        Wipe leases
      </a>
    </li>
    <li>
      <a href="{% url "plugins:netbox_kea:server_subnet4_delete" record.server_pk record.id %}"
         class="dropdown-item text-danger">
        <i class="mdi mdi-trash-can-outline" aria-hidden="true"></i>
        Delete subnet
      </a>
    </li>
    {% else %}
    <li>
      <a href="{% url "plugins:netbox_kea:server_subnet6_pool_add" record.server_pk record.id %}"
         class="dropdown-item">
        <i class="mdi mdi-plus-circle-outline" aria-hidden="true"></i>
        Add pool
      </a>
    </li>
    <li>
      <a href="{% url "plugins:netbox_kea:server_subnet6_wipe_leases" record.server_pk record.id %}"
         class="dropdown-item text-warning">
        <i class="mdi mdi-delete-sweep-outline" aria-hidden="true"></i>
        Wipe leases
      </a>
    </li>
    <li>
      <a href="{% url "plugins:netbox_kea:server_subnet6_delete" record.server_pk record.id %}"
         class="dropdown-item text-danger">
        <i class="mdi mdi-trash-can-outline" aria-hidden="true"></i>
        Delete subnet
      </a>
    </li>
    {% endif %}
    {% endif %}
  </ul>
</span>
"""  # noqa: E501


LEASE_ACTIONS = """<span class="btn-group dropdown">
    <a class="btn btn-sm btn-secondary dropdown-toggle" href="#" type="button" data-bs-toggle="dropdown">
    <i class="mdi mdi-magnify"></i></a>
    <ul class="dropdown-menu">
        {% if record.ip_address %}
        <li>
            <a href="{% url "ipam:ipaddress_list" %}?address={{ record.ip_address }}" class="dropdown-item">
                <i class="mdi mdi-magnify" aria-hidden="true" title="Search IPs"></i>
                Search IPs
            </a>
        </li>
        {% endif %}
        {% if record.hw_address %}
        <li>
            <a href="{% url "dcim:interface_list" %}?mac_address={{ record.hw_address }}" class="dropdown-item">
                <i class="mdi mdi-magnify" aria-hidden="true" title="Search interfaces"></i>
                Search interfaces
            </a>
        </li>
        <li>
            <a href="{% url "virtualization:vminterface_list" %}?mac_address={{ record.hw_address }}" class="dropdown-item">
                <i class="mdi mdi-magnify" aria-hidden="true" title="Search VM interfaces"></i>
                Search VM interfaces
            </a>
        </li>
        {% endif %}
        {% if record.hostname %}
        <li>
            <a href="{% url "dcim:device_list" %}?q={{ record.hostname }}" class="dropdown-item">
                <i class="mdi mdi-magnify" aria-hidden="true" title="Search devices"></i>
                Search devices
            </a>
        </li>
        <li>
            <a href="{% url "virtualization:virtualmachine_list" %}?q={{ record.hostname }}" class="dropdown-item">
                <i class="mdi mdi-magnify" aria-hidden="true" title="Search VMs"></i>
                Search VMs
            </a>
        </li>
        {% endif %}
    </ul>
</span>
"""  # noqa: E501


class DurationColumn(tables.Column):
    """Table column that renders integer seconds as ``HH:MM:SS``."""

    def render(self, value: int):
        """Value is in seconds."""
        return format_duration(value)


class ActionsColumn(tables.TemplateColumn):
    """Table column that renders a dropdown actions menu from a Django template string."""

    def __init__(self, template: str) -> None:
        super().__init__(
            template,
            attrs={"td": {"class": "text-end text-nowrap noprint"}},
            verbose_name="",
        )


class MonospaceColumn(tables.Column):
    """Table column that renders cell text in a monospace font."""

    def __init__(self, *args, additional_classes: list[str] | None = None, **kwargs):
        cls_str = "font-monospace"
        if additional_classes is not None:
            cls_str += " " + " ".join(additional_classes)
        super().__init__(*args, attrs={"td": {"class": cls_str}}, **kwargs)


class ServerTable(NetBoxTable):
    """Table for listing Kea Server objects in the NetBox UI."""

    name = tables.Column(linkify=True)
    dhcp6 = BooleanColumn()
    dhcp4 = BooleanColumn()

    class Meta(NetBoxTable.Meta):
        model = Server
        fields = (
            "pk",
            "name",
            "server_url",
            "username",
            "ssl_verify",
            "client_cert_path",
            "client_key_path",
            "ca_file_path",
            "dhcp6",
            "dhcp4",
        )
        default_columns = ("pk", "name", "server_url", "dhcp6", "dhcp4")


# we can't use NetBox table because it requires an actual model
class GenericTable(BaseTable):
    """Base table for non-model data (e.g. leases and subnets returned from Kea API)."""

    exempt_columns = ("actions", "pk")

    def __init__(self, *args, **kwargs):
        # NetBox v4.5.5 removed the ``user=`` kwarg from BaseTable.__init__.
        # Earlier versions require it to load saved column preferences from the DB.
        # We try with the kwarg first; on TypeError (v4.5.5+) we retry without it.
        try:
            super().__init__(*args, **kwargs)
        except TypeError:
            kwargs.pop("user", None)
            super().__init__(*args, **kwargs)

    class Meta(BaseTable.Meta):
        empty_text = "No rows"
        fields: tuple[str, ...] = ()

    @property
    def objects_count(self):
        """Return the number of rows in the table."""
        return len(self.data)


class SubnetTable(GenericTable):
    """Table for displaying Kea DHCP subnets with links to lease search."""

    id = tables.Column(verbose_name="ID")
    subnet = tables.Column(
        linkify=lambda record, table: (
            (
                reverse(
                    f"plugins:netbox_kea:server_leases{record['dhcp_version']}",
                    args=[record["server_pk"]],
                )
                + "?"
                + urlencode({"by": "subnet", "q": record["subnet"]})
            )
            if record.get("subnet")
            else None
        ),
    )
    shared_network = tables.Column(verbose_name="Shared Network")
    pools = tables.TemplateColumn(
        verbose_name="Pool(s)",
        orderable=False,
        template_code="""{% for pool in record.pools %}<span class="badge text-bg-secondary me-1">{{ pool }}{% if record.server_pk and record.id %} {% if record.dhcp_version == 4 %}<a href="{% url "plugins:netbox_kea:server_subnet4_pool_delete" record.server_pk record.id pool %}" class="text-white ms-1" aria-label="Delete pool {{ pool }}"><i class="mdi mdi-close-circle-outline" style="font-size:0.85em" aria-hidden="true"></i></a>{% else %}<a href="{% url "plugins:netbox_kea:server_subnet6_pool_delete" record.server_pk record.id pool %}" class="text-white ms-1" aria-label="Delete pool {{ pool }}"><i class="mdi mdi-close-circle-outline" style="font-size:0.85em" aria-hidden="true"></i></a>{% endif %}{% endif %}</span> {% empty %}— {% endfor %}{% if record.server_pk and record.id %}{% if record.dhcp_version == 4 %}<a href="{% url "plugins:netbox_kea:server_subnet4_pool_add" record.server_pk record.id %}" class="btn btn-sm btn-outline-secondary ms-1" aria-label="Add pool"><i class="mdi mdi-plus" aria-hidden="true"></i></a>{% else %}<a href="{% url "plugins:netbox_kea:server_subnet6_pool_add" record.server_pk record.id %}" class="btn btn-sm btn-outline-secondary ms-1" aria-label="Add pool"><i class="mdi mdi-plus" aria-hidden="true"></i></a>{% endif %}{% endif %}""",
    )
    utilization = tables.TemplateColumn(
        verbose_name="Utilization",
        orderable=False,
        template_code=(
            "{% if record.utilization %}"
            '<span class="badge '
            "{% if record.utilization_pct == 100 %}text-bg-danger"
            "{% elif record.utilization_pct >= 80 %}text-bg-warning"
            "{% else %}text-bg-success{% endif %}"
            '">{{ record.utilization }}</span>'
            "{% endif %}"
        ),
    )
    options = tables.TemplateColumn(
        verbose_name="Options",
        orderable=False,
        template_code="""{% with opts=record.options %}
{% if opts %}
<span>
{% if opts.gateway %}<span title="Gateway">GW: {{ opts.gateway }}</span>{% endif %}
{% if opts.dns_servers %} <span data-bs-toggle="tooltip" title="DNS: {{ opts.dns_servers }}">
  <abbr>DNS{% if opts.domain_name %}: {{ opts.domain_name }}{% endif %}</abbr>
</span>{% endif %}
{% if opts.ntp_servers %} <span data-bs-toggle="tooltip" title="NTP">NTP: {{ opts.ntp_servers }}</span>{% endif %}
</span>
{% endif %}{% endwith %}""",
    )
    actions = ActionsColumn(SUBNET_ACTIONS)

    class Meta(GenericTable.Meta):
        empty_text = "No subnets"
        fields = ("id", "subnet", "pools", "utilization", "options", "shared_network", "actions")
        default_columns = ("id", "subnet", "pools", "utilization", "options", "shared_network")


class BaseLeaseTable(GenericTable):
    """Base table for DHCP lease data; subclassed for v4 and v6."""

    # This column is for the select checkboxes.
    pk = ToggleColumn(verbose_name="IP Address", accessor="ip_address", visible=True)
    ip_address = tables.Column(verbose_name="IP Address")
    hostname = tables.Column(verbose_name="Hostname")
    subnet_id = tables.Column(verbose_name="Subnet ID")
    hw_address = MonospaceColumn(verbose_name="Hardware Address")
    valid_lft = DurationColumn(verbose_name="Valid Lifetime")
    cltt = columns.DateTimeColumn(verbose_name="Client Last Transaction Time")
    expires_at = columns.DateTimeColumn(verbose_name="Expires At")
    expires_in = DurationColumn(verbose_name="Expires In")
    reserved = tables.TemplateColumn(
        verbose_name="Reserved",
        orderable=False,
        template_code=(
            "{% if record.reservation_url %}"
            '<a href="{{ record.reservation_url }}" class="badge text-bg-success text-decoration-none">'
            "Reserved</a>"
            "{% elif record.create_reservation_url %}"
            '<a href="{{ record.create_reservation_url }}" class="badge text-bg-warning text-decoration-none">'
            "+ Reserve</a>"
            "{% endif %}"
        ),
    )
    netbox_ip = tables.TemplateColumn(
        verbose_name="NetBox IP",
        orderable=False,
        template_code=(
            "{% if record.netbox_ip_url %}"
            '<a href="{{ record.netbox_ip_url }}" class="badge text-bg-success text-decoration-none">'
            '<i class="mdi mdi-link-variant"></i> Synced</a>'
            "{% elif record.sync_url %}"
            '<button type="button"'
            ' hx-post="{{ record.sync_url }}"'
            ' hx-vals=\'{"ip_address":"{{ record.ip_address|escapejs }}","hostname":"{{ record.hostname|default:""|escapejs }}"}\''
            ' hx-target="closest td"'
            ' hx-swap="innerHTML"'
            ' class="badge text-bg-secondary border-0"'
            ' style="cursor:pointer">'
            '<i class="mdi mdi-sync"></i> Sync</button>'
            "{% endif %}"
        ),
    )
    actions = ActionsColumn(LEASE_ACTIONS)

    class Meta(GenericTable.Meta):
        empty_text = "No leases found."
        fields = (
            "ip_address",
            "hostname",
            "subnet_id",
            "hw_address",
            "valid_lft",
            "cltt",
            "expires_at",
            "expires_in",
            "reserved",
            "netbox_ip",
            "actions",
        )
        default_columns = ("ip_address", "hostname", "reserved", "netbox_ip")


class LeaseTable4(BaseLeaseTable):
    """Lease table for DHCPv4, adding the client-id column."""

    client_id = tables.Column(verbose_name="Client ID")

    class Meta(BaseLeaseTable.Meta):
        fields = ("client_id", *BaseLeaseTable.Meta.fields)


class LeaseTable6(BaseLeaseTable):
    """Lease table for DHCPv6, adding type, preferred lifetime, DUID and IAID columns."""

    type = tables.Column(verbose_name="Type", accessor="type")
    preferred_lft = DurationColumn(verbose_name="Preferred Lifetime")
    duid = MonospaceColumn(verbose_name="DUID", additional_classes=["text-break"])
    iaid = MonospaceColumn(verbose_name="IAID")

    class Meta(BaseLeaseTable.Meta):
        fields = ("type", "duid", "iaid", *BaseLeaseTable.Meta.fields)


class LeaseDeleteTable(GenericTable):
    """Minimal table used on the bulk-delete confirmation page."""

    ip_address = tables.Column(verbose_name="IP Address", accessor="ip")

    class Meta(NetBoxTable.Meta):
        empty_text = "No leases"
        fields = ("ip_address",)
        default_columns = ("ip_address",)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Reservation tables
# ─────────────────────────────────────────────────────────────────────────────

RESERVATION_ACTIONS_V4 = """
<span class="btn-group">
  <a href="{% url "plugins:netbox_kea:server_reservation4_edit" record.server_pk record.subnet_id record.ip_address %}"
     class="btn btn-sm btn-warning" aria-label="Edit reservation {{ record.ip_address }}"><i class="mdi mdi-pencil" aria-hidden="true"></i></a>
  <a href="{% url "plugins:netbox_kea:server_reservation4_delete" record.server_pk record.subnet_id record.ip_address %}"
     class="btn btn-sm btn-danger" aria-label="Delete reservation {{ record.ip_address }}"><i class="mdi mdi-trash-can-outline" aria-hidden="true"></i></a>
</span>
"""

RESERVATION_ACTIONS_V6 = """
<span class="btn-group">
  <a href="{% url "plugins:netbox_kea:server_reservation6_edit" record.server_pk record.subnet_id record.ip_address %}"
     class="btn btn-sm btn-warning" aria-label="Edit reservation {{ record.ip_address }}"><i class="mdi mdi-pencil" aria-hidden="true"></i></a>
  <a href="{% url "plugins:netbox_kea:server_reservation6_delete" record.server_pk record.subnet_id record.ip_address %}"
     class="btn btn-sm btn-danger" aria-label="Delete reservation {{ record.ip_address }}"><i class="mdi mdi-trash-can-outline" aria-hidden="true"></i></a>
</span>
"""


_LEASE_STATUS_LINK_V4 = (
    "{% if record.has_active_lease is not None %}"
    "{% if record.has_active_lease %}"
    "<a href=\"{% url 'plugins:netbox_kea:server_leases4' record.server_pk %}?q={{ record.ip_address }}&by=ip\""
    ' class="badge text-bg-success text-decoration-none">Active Lease</a>'
    "{% else %}"
    '<span class="badge text-bg-secondary">No Lease</span>'
    "{% endif %}"
    "{% endif %}"
)

_LEASE_STATUS_LINK_V6 = (
    "{% if record.has_active_lease is not None %}"
    "{% if record.has_active_lease %}"
    "<a href=\"{% url 'plugins:netbox_kea:server_leases6' record.server_pk %}?q={{ record.ip_address }}&by=ip\""
    ' class="badge text-bg-success text-decoration-none">Active Lease</a>'
    "{% else %}"
    '<span class="badge text-bg-secondary">No Lease</span>'
    "{% endif %}"
    "{% endif %}"
)


class ReservationTable4(GenericTable):
    """Table for DHCPv4 host reservations returned from the Kea API."""

    subnet_id = tables.Column(verbose_name="Subnet ID", accessor="subnet-id")
    hw_address = MonospaceColumn(verbose_name="Hardware Address", accessor="hw-address")
    ip_address = tables.Column(verbose_name="IP Address", accessor="ip-address")
    hostname = tables.Column(verbose_name="Hostname")
    lease_status = tables.TemplateColumn(
        verbose_name="Lease",
        orderable=False,
        template_code=_LEASE_STATUS_LINK_V4,
    )
    netbox_ip = tables.TemplateColumn(
        verbose_name="NetBox IP",
        orderable=False,
        template_code=(
            "{% if record.netbox_ip_url %}"
            '<a href="{{ record.netbox_ip_url }}" class="badge text-bg-success text-decoration-none">'
            '<i class="mdi mdi-link-variant"></i> Synced</a>'
            "{% elif record.sync_url %}"
            '<button type="button"'
            ' hx-post="{{ record.sync_url }}"'
            ' hx-vals=\'{"ip_address":"{{ record.ip_address|escapejs }}","hostname":"{{ record.hostname|default:""|escapejs }}"}\''
            ' hx-target="closest td"'
            ' hx-swap="innerHTML"'
            ' class="badge text-bg-secondary border-0"'
            ' style="cursor:pointer">'
            '<i class="mdi mdi-sync"></i> Sync</button>'
            "{% endif %}"
        ),
    )
    actions = ActionsColumn(RESERVATION_ACTIONS_V4)

    class Meta(GenericTable.Meta):
        empty_text = "No reservations found."
        fields = ("subnet_id", "hw_address", "ip_address", "hostname", "lease_status", "netbox_ip", "actions")
        default_columns = ("subnet_id", "hw_address", "ip_address", "hostname", "lease_status", "netbox_ip", "actions")


class ReservationTable6(GenericTable):
    """Table for DHCPv6 host reservations returned from the Kea API."""

    subnet_id = tables.Column(verbose_name="Subnet ID", accessor="subnet-id")
    duid = MonospaceColumn(verbose_name="DUID")
    ip_addresses = tables.Column(verbose_name="IPv6 Addresses", accessor="ip-addresses")
    hostname = tables.Column(verbose_name="Hostname")
    lease_status = tables.TemplateColumn(
        verbose_name="Lease",
        orderable=False,
        template_code=_LEASE_STATUS_LINK_V6,
    )
    netbox_ip = tables.TemplateColumn(
        verbose_name="NetBox IP",
        orderable=False,
        template_code=(
            "{% if record.netbox_ip_url %}"
            '<a href="{{ record.netbox_ip_url }}" class="badge text-bg-success text-decoration-none">'
            '<i class="mdi mdi-link-variant"></i> Synced</a>'
            "{% elif record.sync_url %}"
            '<button type="button"'
            ' hx-post="{{ record.sync_url }}"'
            ' hx-vals=\'{"ip_address":"{{ record.ip_address|escapejs }}","hostname":"{{ record.hostname|default:""|escapejs }}"}\''
            ' hx-target="closest td"'
            ' hx-swap="innerHTML"'
            ' class="badge text-bg-secondary border-0"'
            ' style="cursor:pointer">'
            '<i class="mdi mdi-sync"></i> Sync</button>'
            "{% endif %}"
        ),
    )
    actions = ActionsColumn(RESERVATION_ACTIONS_V6)

    class Meta(GenericTable.Meta):
        empty_text = "No reservations found."
        fields = ("subnet_id", "duid", "ip_addresses", "hostname", "lease_status", "netbox_ip", "actions")
        default_columns = ("subnet_id", "duid", "ip_addresses", "hostname", "lease_status", "netbox_ip", "actions")


# ─────────────────────────────────────────────────────────────────────────────


def _server_column() -> tables.TemplateColumn:
    """Create a Server column linking to the server detail page."""
    return tables.TemplateColumn(
        template_code=(
            "<a href=\"{% url 'plugins:netbox_kea:server' record.server_pk %}\">{{ record.server_name }}</a>"
        ),
        verbose_name="Server",
        orderable=False,
    )


class GlobalReservationTable4(GenericTable):
    """DHCPv4 reservation table aggregated across multiple servers."""

    server = _server_column()
    subnet_id = tables.Column(verbose_name="Subnet ID", accessor="subnet-id")
    hw_address = MonospaceColumn(verbose_name="Hardware Address", accessor="hw-address")
    ip_address = tables.Column(verbose_name="IP Address", accessor="ip-address")
    hostname = tables.Column(verbose_name="Hostname")
    lease_status = tables.TemplateColumn(
        verbose_name="Lease",
        orderable=False,
        template_code=_LEASE_STATUS_LINK_V4,
    )
    netbox_ip = tables.TemplateColumn(
        verbose_name="NetBox IP",
        orderable=False,
        template_code=(
            "{% if record.netbox_ip_url %}"
            '<a href="{{ record.netbox_ip_url }}" class="badge text-bg-success text-decoration-none">'
            '<i class="mdi mdi-link-variant"></i> Synced</a>'
            "{% elif record.sync_url %}"
            '<button type="button"'
            ' hx-post="{{ record.sync_url }}"'
            ' hx-vals=\'{"ip_address":"{{ record.ip_address|escapejs }}","hostname":"{{ record.hostname|default:""|escapejs }}"}\''
            ' hx-target="closest td"'
            ' hx-swap="innerHTML"'
            ' class="badge text-bg-secondary border-0"'
            ' style="cursor:pointer">'
            '<i class="mdi mdi-sync"></i> Sync</button>'
            "{% endif %}"
        ),
    )
    actions = ActionsColumn(RESERVATION_ACTIONS_V4)

    class Meta(GenericTable.Meta):
        empty_text = "No reservations found."
        fields = ("server", "subnet_id", "hw_address", "ip_address", "hostname", "lease_status", "netbox_ip", "actions")
        default_columns = (
            "server",
            "subnet_id",
            "hw_address",
            "ip_address",
            "hostname",
            "lease_status",
            "netbox_ip",
            "actions",
        )


class GlobalReservationTable6(GenericTable):
    """DHCPv6 reservation table aggregated across multiple servers."""

    server = _server_column()
    subnet_id = tables.Column(verbose_name="Subnet ID", accessor="subnet-id")
    duid = MonospaceColumn(verbose_name="DUID")
    ip_addresses = tables.Column(verbose_name="IPv6 Addresses", accessor="ip-addresses")
    hostname = tables.Column(verbose_name="Hostname")
    lease_status = tables.TemplateColumn(
        verbose_name="Lease",
        orderable=False,
        template_code=_LEASE_STATUS_LINK_V6,
    )
    netbox_ip = tables.TemplateColumn(
        verbose_name="NetBox IP",
        orderable=False,
        template_code=(
            "{% if record.netbox_ip_url %}"
            '<a href="{{ record.netbox_ip_url }}" class="badge text-bg-success text-decoration-none">'
            '<i class="mdi mdi-link-variant"></i> Synced</a>'
            "{% elif record.sync_url %}"
            '<button type="button"'
            ' hx-post="{{ record.sync_url }}"'
            ' hx-vals=\'{"ip_address":"{{ record.ip_address|escapejs }}","hostname":"{{ record.hostname|default:""|escapejs }}"}\''
            ' hx-target="closest td"'
            ' hx-swap="innerHTML"'
            ' class="badge text-bg-secondary border-0"'
            ' style="cursor:pointer">'
            '<i class="mdi mdi-sync"></i> Sync</button>'
            "{% endif %}"
        ),
    )
    actions = ActionsColumn(RESERVATION_ACTIONS_V6)

    class Meta(GenericTable.Meta):
        empty_text = "No reservations found."
        fields = ("server", "subnet_id", "duid", "ip_addresses", "hostname", "lease_status", "netbox_ip", "actions")
        default_columns = (
            "server",
            "subnet_id",
            "duid",
            "ip_addresses",
            "hostname",
            "lease_status",
            "netbox_ip",
            "actions",
        )


class GlobalLeaseTable4(BaseLeaseTable):
    """DHCPv4 lease table aggregated across multiple servers."""

    server = _server_column()

    class Meta(BaseLeaseTable.Meta):
        fields = ("server", *BaseLeaseTable.Meta.fields)
        default_columns = ("server", "ip_address", "hostname", "hw_address", "subnet_id", "reserved", "netbox_ip")


class GlobalLeaseTable6(BaseLeaseTable):
    """DHCPv6 lease table aggregated across multiple servers."""

    server = _server_column()
    type = tables.Column(verbose_name="Type", accessor="type")
    preferred_lft = DurationColumn(verbose_name="Preferred Lifetime")
    duid = MonospaceColumn(verbose_name="DUID", additional_classes=["text-break"])
    iaid = MonospaceColumn(verbose_name="IAID")

    class Meta(BaseLeaseTable.Meta):
        fields = ("server", "type", "duid", "iaid", *BaseLeaseTable.Meta.fields)
        default_columns = ("server", "ip_address", "hostname", "duid", "subnet_id", "reserved", "netbox_ip")


class GlobalSubnetTable4(GenericTable):
    """DHCPv4 subnet table aggregated across multiple servers."""

    server = _server_column()
    id = tables.Column(verbose_name="ID")
    subnet = tables.Column(
        linkify=lambda record, table: (
            (
                reverse(
                    "plugins:netbox_kea:server_leases4",
                    args=[record["server_pk"]],
                )
                + "?"
                + urlencode({"by": "subnet", "q": record["subnet"]})
            )
            if record.get("subnet")
            else None
        ),
    )
    shared_network = tables.Column(verbose_name="Shared Network")
    pools = tables.TemplateColumn(
        verbose_name="Pool(s)",
        orderable=False,
        template_code="""{% for pool in record.pools %}<span class="badge text-bg-secondary me-1">{{ pool }}{% if record.server_pk and record.id %} {% if record.dhcp_version == 4 %}<a href="{% url "plugins:netbox_kea:server_subnet4_pool_delete" record.server_pk record.id pool %}" class="text-white ms-1" aria-label="Delete pool {{ pool }}"><i class="mdi mdi-close-circle-outline" style="font-size:0.85em" aria-hidden="true"></i></a>{% else %}<a href="{% url "plugins:netbox_kea:server_subnet6_pool_delete" record.server_pk record.id pool %}" class="text-white ms-1" aria-label="Delete pool {{ pool }}"><i class="mdi mdi-close-circle-outline" style="font-size:0.85em" aria-hidden="true"></i></a>{% endif %}{% endif %}</span> {% empty %}— {% endfor %}{% if record.server_pk and record.id %}{% if record.dhcp_version == 4 %}<a href="{% url "plugins:netbox_kea:server_subnet4_pool_add" record.server_pk record.id %}" class="btn btn-sm btn-outline-secondary ms-1" aria-label="Add pool"><i class="mdi mdi-plus" aria-hidden="true"></i></a>{% else %}<a href="{% url "plugins:netbox_kea:server_subnet6_pool_add" record.server_pk record.id %}" class="btn btn-sm btn-outline-secondary ms-1" aria-label="Add pool"><i class="mdi mdi-plus" aria-hidden="true"></i></a>{% endif %}{% endif %}""",
    )
    options = tables.TemplateColumn(
        verbose_name="Options",
        orderable=False,
        template_code="""{% with opts=record.options %}{% if opts %}
<span>{% if opts.gateway %}GW: {{ opts.gateway }}{% endif %}
{% if opts.dns_servers %} | DNS: {{ opts.dns_servers }}{% endif %}
{% if opts.ntp_servers %} | NTP: {{ opts.ntp_servers }}{% endif %}
</span>{% endif %}{% endwith %}""",
    )
    utilization = tables.TemplateColumn(
        verbose_name="Utilization",
        orderable=False,
        template_code=(
            "{% if record.utilization %}"
            '<span class="badge '
            "{% if record.utilization_pct == 100 %}text-bg-danger"
            "{% elif record.utilization_pct >= 80 %}text-bg-warning"
            "{% else %}text-bg-success{% endif %}"
            '">{{ record.utilization }}</span>'
            "{% endif %}"
        ),
    )

    class Meta(GenericTable.Meta):
        empty_text = "No subnets found."
        fields = ("server", "id", "subnet", "pools", "utilization", "options", "shared_network")
        default_columns = ("server", "id", "subnet", "pools", "utilization", "options", "shared_network")


class GlobalSubnetTable6(GlobalSubnetTable4):
    """DHCPv6 subnet table aggregated across multiple servers."""

    subnet = tables.Column(
        linkify=lambda record, table: (
            (
                reverse(
                    "plugins:netbox_kea:server_leases6",
                    args=[record["server_pk"]],
                )
                + "?"
                + urlencode({"by": "subnet", "q": record["subnet"]})
            )
            if record.get("subnet")
            else None
        ),
    )

    class Meta(GlobalSubnetTable4.Meta):
        pass

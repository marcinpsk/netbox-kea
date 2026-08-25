import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
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
    {% if record.server_pk and record.id and record.can_change %}
    <li><hr class="dropdown-divider"></li>
    {% if record.dhcp_version == 4 %}
    <li>
      <a href="{% url "plugins:netbox_kea:server_subnet4_edit" record.server_pk record.id %}"
         class="dropdown-item">
        <i class="mdi mdi-pencil-outline" aria-hidden="true"></i>
        Edit subnet
      </a>
    </li>
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
      <a href="{% url "plugins:netbox_kea:server_subnet4_options_edit" record.server_pk record.id %}"
         class="dropdown-item">
        <i class="mdi mdi-tune" aria-hidden="true"></i>
        Edit options
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
      <a href="{% url "plugins:netbox_kea:server_subnet6_edit" record.server_pk record.id %}"
         class="dropdown-item">
        <i class="mdi mdi-pencil-outline" aria-hidden="true"></i>
        Edit subnet
      </a>
    </li>
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
      <a href="{% url "plugins:netbox_kea:server_subnet6_options_edit" record.server_pk record.id %}"
         class="dropdown-item">
        <i class="mdi mdi-tune" aria-hidden="true"></i>
        Edit options
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
        {% if record.edit_url %}
        <li>
            <a href="{{ record.edit_url }}" class="dropdown-item">
                <i class="mdi mdi-pencil-outline" aria-hidden="true" title="Edit lease"></i>
                Edit lease
            </a>
        </li>
        {% endif %}
        {% if record.reservation_url and record.can_change_reservation %}
        <li>
            <a href="{{ record.reservation_url }}" class="dropdown-item">
                <i class="mdi mdi-bookmark-outline" aria-hidden="true" title="Edit reservation"></i>
                Edit reservation
            </a>
        </li>
        {% endif %}
        {% if record.edit_url or record.reservation_url and record.can_change_reservation %}
        <li><hr class="dropdown-divider"></li>
        {% endif %}
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


class ExpiryDurationColumn(DurationColumn):
    """DurationColumn that applies an expiry CSS class from ``record['expiry_class']``."""

    def render(self, value: int, record: dict):
        """Wrap the duration text in a <span> with the expiry CSS class when set."""
        text = super().render(value)
        cls = record.get("expiry_class", "")
        if cls:
            return format_html('<span class="{}">{}</span>', cls, text)
        return text


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
            "ca_url",
            "ca_username",
            "ssl_verify",
            "client_cert_path",
            "client_key_path",
            "ca_file_path",
            "dhcp6",
            "dhcp4",
        )
        default_columns = ("pk", "name", "ca_url", "dhcp6", "dhcp4")


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
        except TypeError as exc:
            if "unexpected keyword argument 'user'" not in str(exc):
                raise
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
        order_by="_subnet_sort_key",
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
        template_code=(
            '<span class="d-flex flex-column gap-1 align-items-start">'
            "{% for pool in record.pools %}"
            '<span class="d-inline-flex align-items-center gap-1 font-monospace small'
            ' border rounded px-2 py-0 bg-body-secondary text-nowrap">'
            "{{ pool }}"
            "{% if record.server_pk and record.id and record.can_change %}"
            "{% if record.dhcp_version == 4 %}"
            '<a href="{% url "plugins:netbox_kea:server_subnet4_pool_delete"'
            ' record.server_pk record.id pool %}"'
            ' class="text-danger ms-1 lh-1" aria-label="Delete pool {{ pool }}">'
            '<i class="mdi mdi-close" style="font-size:0.8em" aria-hidden="true"></i>'
            "</a>"
            "{% else %}"
            '<a href="{% url "plugins:netbox_kea:server_subnet6_pool_delete"'
            ' record.server_pk record.id pool %}"'
            ' class="text-danger ms-1 lh-1" aria-label="Delete pool {{ pool }}">'
            '<i class="mdi mdi-close" style="font-size:0.8em" aria-hidden="true"></i>'
            "</a>"
            "{% endif %}{% endif %}"
            "</span>"
            "{% empty %}—"
            "{% endfor %}"
            "{% if record.server_pk and record.id and record.can_change %}"
            "{% if record.dhcp_version == 4 %}"
            '<a href="{% url "plugins:netbox_kea:server_subnet4_pool_add" record.server_pk record.id %}"'
            ' class="text-muted small text-decoration-none" aria-label="Add pool">'
            '<i class="mdi mdi-plus-circle-outline" aria-hidden="true"></i> Add pool'
            "</a>"
            "{% else %}"
            '<a href="{% url "plugins:netbox_kea:server_subnet6_pool_add" record.server_pk record.id %}"'
            ' class="text-muted small text-decoration-none" aria-label="Add pool">'
            '<i class="mdi mdi-plus-circle-outline" aria-hidden="true"></i> Add pool'
            "</a>"
            "{% endif %}{% endif %}"
            "</span>"
        ),
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
{% if opts or record.ddns_qualifying_suffix %}
<span>
{% if opts.gateway %}<span title="Gateway">GW: {{ opts.gateway }}</span>{% endif %}
{% if opts.dns_servers %} <span data-bs-toggle="tooltip" title="DNS: {{ opts.dns_servers }}">
  <abbr>DNS{% if opts.domain_name %}: {{ opts.domain_name }}{% endif %}</abbr>
</span>{% endif %}
{% if opts.ntp_servers %} <span data-bs-toggle="tooltip" title="NTP">NTP: {{ opts.ntp_servers }}</span>{% endif %}
{% if record.ddns_qualifying_suffix %} <span data-bs-toggle="tooltip" title="DDNS qualifying suffix: {{ record.ddns_qualifying_suffix }}">DDNS: {{ record.ddns_qualifying_suffix }}</span>{% endif %}
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
    ip_address = tables.Column(verbose_name="IP Address", order_by="_ip_sort_key")
    hostname = tables.Column(verbose_name="Hostname")
    subnet_id = tables.Column(verbose_name="Subnet ID")
    hw_address = MonospaceColumn(verbose_name="Hardware Address")
    valid_lft = DurationColumn(verbose_name="Valid Lifetime")
    cltt = columns.DateTimeColumn(verbose_name="Client Last Transaction Time")
    expires_at = columns.DateTimeColumn(verbose_name="Expires At")
    expires_in = ExpiryDurationColumn(verbose_name="Expires In")
    state_label = tables.TemplateColumn(
        verbose_name="State",
        orderable=False,
        template_code=(
            "{% if record.state_label == 'Active' %}"
            '<span class="badge text-bg-success">Active</span>'
            "{% elif record.state_label == 'Declined' %}"
            '<span class="badge text-bg-warning">Declined</span>'
            "{% elif record.state_label == 'Expired' %}"
            '<span class="badge text-bg-danger">Expired</span>'
            "{% else %}"
            '<span class="badge text-bg-secondary">{{ record.state_label }}</span>'
            "{% endif %}"
        ),
    )
    reserved = tables.TemplateColumn(
        verbose_name="Reserved",
        orderable=False,
        template_code=(
            "{% if record.is_reserved %}"
            "{% if record.can_change_reservation and record.reservation_url %}"
            '<a href="{{ record.reservation_url }}" class="badge text-bg-success text-decoration-none">'
            "Reserved</a>"
            "{% else %}"
            '<span class="badge text-bg-success">Reserved</span>'
            "{% endif %}"
            "{% if record.stale_mac %}"
            ' <span class="badge text-bg-warning"'
            ' title="Lease MAC ({{ record.stale_lease_mac }}) ≠ Reservation MAC ({{ record.reservation_mac }})'
            ' — delete this lease to force the old device off this IP">'
            "&#9888; MAC?</span>"
            "{% if record.can_delete %}"
            ' <button type="button"'
            ' class="badge text-bg-danger border-0 ms-1"'
            ' style="cursor:pointer"'
            ' aria-label="Delete lease {{ record.ip_address|escapejs }} held by {{ record.stale_lease_mac|escapejs }}"'
            ' hx-post="{{ record.delete_lease_url }}"'
            ' hx-confirm="Delete lease {{ record.ip_address|escapejs }} held by {{ record.stale_lease_mac|escapejs }}?'
            ' The old device must re-request this IP via DORA."'
            ' hx-vals=\'{"pk":"{{ record.ip_address|escapejs }}","_confirm":"1"}\'>'
            '<i class="mdi mdi-delete-outline" aria-hidden="true"></i></button>'
            "{% endif %}"
            "{% endif %}"
            "{% elif record.pending_ip_change %}"
            '<span class="badge text-bg-info"'
            ' title="This device has a reservation at {{ record.pending_reservation_ip }}'
            ' — lease will move on next renewal">'
            '<i class="mdi mdi-arrow-right-bold" aria-hidden="true"></i>'
            " Pending {{ record.pending_reservation_ip }}</span>"
            "{% if record.reservation_url %}"
            "{% if record.can_change_reservation %}"
            ' <a href="{{ record.reservation_url }}" class="badge text-bg-success text-decoration-none ms-1">'
            "View</a>"
            "{% else %}"
            ' <span class="badge text-bg-success ms-1">View</span>'
            "{% endif %}"
            "{% endif %}"
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
            "state_label",
            "valid_lft",
            "cltt",
            "expires_at",
            "expires_in",
            "reserved",
            "netbox_ip",
            "actions",
        )
        default_columns = ("ip_address", "hostname", "state_label", "reserved", "netbox_ip")


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

# Reservation edit/delete URLs are precomputed by the view
# (``views.reservations._attach_reservation_action_urls``) rather than reversed here.
# A reservation that reserves no address has no address-keyed URL, and ``{% url %}``
# with an empty ``ip_address`` raised NoReverseMatch, taking the whole table down
# (issue #110). ``can_change`` is already folded into the precomputed values.
RESERVATION_ACTIONS = """
{% if record.edit_url or record.delete_url %}
<span class="btn-group">
  {% if record.edit_url %}
  <a href="{{ record.edit_url }}"
     class="btn btn-sm btn-warning" aria-label="Edit reservation {{ record.ip_address|default:record.identifier }}"><i class="mdi mdi-pencil" aria-hidden="true"></i></a>
  {% endif %}
  {% if record.delete_url %}
  <a href="{{ record.delete_url }}"
     class="btn btn-sm btn-danger" aria-label="Delete reservation {{ record.ip_address|default:record.identifier }}"><i class="mdi mdi-trash-can-outline" aria-hidden="true"></i></a>
  {% endif %}
</span>
{% endif %}
"""


#: The view resolves the search that finds the matched lease, because an addressless or
#: multi-address Reservation has no single address the template could link to.
_LEASE_STATUS_LINK = (
    "{% if record.has_active_lease is not None %}"
    "{% if not record.has_active_lease %}"
    '<span class="badge text-bg-secondary">No Lease</span>'
    "{% elif record.lease_url %}"
    '<a href="{{ record.lease_url }}" class="badge text-bg-success text-decoration-none">Active Lease</a>'
    "{% else %}"
    '<span class="badge text-bg-success">Active Lease</span>'
    "{% endif %}"
    "{% endif %}"
)


#: Identifier cell: the value plus the Kea key it came from.  Without the type a
#: flex-id is indistinguishable from a hardware address.
_IDENTIFIER_CELL = (
    "{% if record.identifier %}"
    '<span class="font-monospace">{{ record.identifier }}</span>'
    '<br><span class="text-muted small">{{ record.identifier_type }}</span>'
    "{% endif %}"
)

#: A reservation row carries one ``identifier`` plus its ``identifier_type``, so the
#: protocol-specific columns must stay empty for every other identifier type.
_HW_ADDRESS_CELL = (
    '{% if record.identifier_type == "hw-address" %}'
    '<span class="font-monospace">{{ record.identifier }}</span>'
    "{% endif %}"
)
_DUID_CELL = (
    '{% if record.identifier_type == "duid" %}'
    '<span class="font-monospace text-break">{{ record.identifier }}</span>'
    "{% endif %}"
)

#: Address cell for reservations that may reserve no address at all.
_RESERVATION_ADDRESS_CELL = (
    "{% if record.ip_address %}"
    "{{ record.ip_address }}"
    "{% else %}"
    '<span class="badge text-bg-secondary">No address</span>'
    "{% endif %}"
)

#: DHCPv6 counterpart: the reserved addresses, or the same badge when there are none.
_RESERVATION_ADDRESSES_CELL = (
    "{% if value %}"
    "{% for address in value %}{{ address }}{% if not forloop.last %}<br>{% endif %}{% endfor %}"
    "{% else %}"
    '<span class="badge text-bg-secondary">No address</span>'
    "{% endif %}"
)

_RESERVATION_PREFIXES_CELL = (
    "{% for prefix in record.prefixes %}"
    '<span class="font-monospace">{{ prefix }}</span>{% if not forloop.last %}<br>{% endif %}'
    "{% endfor %}"
)

_RESERVATION_SCOPE_CELL = (
    "{% if record.scope_kind == 'global' %}"
    '<span class="badge text-bg-primary">Global</span>'
    "{% else %}"
    '<span class="font-monospace">{{ record.subnet_cidr }}</span>'
    '<br><span class="text-muted small">Subnet ID {{ record.subnet_id }}</span>'
    "{% endif %}"
)

_RESERVATION_OPTIONS_CELL = (
    "{% for option in record.options %}"
    "{% if option.space %}"
    '<span class="font-monospace text-muted">{{ option.space }}</span> / '
    "{% endif %}"
    "{% if option.name %}"
    '<span class="font-monospace">{{ option.name }}</span>'
    '{% if option.code is not None %} <span class="text-muted">(Code {{ option.code }})</span>{% endif %}'
    "{% else %}"
    '<span class="font-monospace">Code {{ option.code }}</span>'
    "{% endif %}"
    ": {{ option.data }}"
    "{% if option.csv_format is not None or option.always_send is not None or option.never_send is not None %}"
    '<br><span class="text-muted small">'
    "{% if option.csv_format is not None %}"
    "<span class=\"me-2\">CSV format: {{ option.csv_format|yesno:'Yes,No' }}</span>"
    "{% endif %}"
    "{% if option.always_send is not None %}"
    "<span class=\"me-2\">Always send: {{ option.always_send|yesno:'Yes,No' }}</span>"
    "{% endif %}"
    "{% if option.never_send is not None %}"
    "<span>Never send: {{ option.never_send|yesno:'Yes,No' }}</span>"
    "{% endif %}"
    "</span>"
    "{% endif %}"
    "{% if not forloop.last %}<br>{% endif %}"
    "{% empty %}"
    '<span class="text-muted">None</span>'
    "{% endfor %}"
)

# Branch on the stable state code, the same key the HTMX sync response compares in
# inc/reservation_sync_badge.html, so a changed display label cannot split the two.
_RESERVATION_SYNC_CELL = (
    "{% if record.sync_state.code == 'synchronized' %}"
    '<a href="{{ record.netbox_ip_url }}" class="badge text-bg-success text-decoration-none"'
    ' title="{{ record.sync_synchronized }} of {{ record.sync_total }} addresses synchronized">'
    "{{ record.sync_state.label }} {{ record.sync_synchronized }}/{{ record.sync_total }}</a>"
    "{% elif record.sync_state.code == 'partially-synchronized' %}"
    '<span class="badge text-bg-warning">{{ record.sync_state.label }} '
    "{{ record.sync_synchronized }}/{{ record.sync_total }}</span> "
    "{% elif record.sync_state.code == 'not-synchronized' %}"
    '<span class="badge text-bg-secondary">{{ record.sync_state.label }} 0/{{ record.sync_total }}</span> '
    "{% elif record.sync_state.code == 'not-applicable' %}"
    '<span class="badge text-bg-secondary" title="{{ record.sync_reason }}">{{ record.sync_state.label }}</span>'
    "{% else %}"
    '<span class="badge text-bg-warning" title="{{ record.sync_reason }}">Unknown</span>'
    "{% endif %}"
    "{% if record.sync_url %}"
    '<button type="button" hx-post="{{ record.sync_url }}" hx-target="closest td" hx-swap="innerHTML"'
    ' class="badge text-bg-primary border-0 ms-1" style="cursor:pointer">'
    '<i class="mdi mdi-sync"></i> Sync all</button>'
    "{% endif %}"
)


class ReservationTable4(GenericTable):
    """Table for DHCPv4 host reservations returned from the Kea API."""

    scope = tables.TemplateColumn(verbose_name="Scope", orderable=False, template_code=_RESERVATION_SCOPE_CELL)
    subnet_id = tables.Column(verbose_name="Subnet ID")
    hw_address = tables.TemplateColumn(
        verbose_name="Hardware Address",
        order_by="identifier",
        template_code=_HW_ADDRESS_CELL,
    )
    identifier = tables.TemplateColumn(
        verbose_name="Identifier",
        orderable=False,
        template_code=_IDENTIFIER_CELL,
    )
    ip_address = tables.TemplateColumn(
        verbose_name="IP Address",
        order_by="_ip_sort_key",
        template_code=_RESERVATION_ADDRESS_CELL,
    )
    hostname = tables.Column(verbose_name="Hostname")
    options = tables.TemplateColumn(
        verbose_name="DHCP Options", orderable=False, template_code=_RESERVATION_OPTIONS_CELL
    )
    lease_status = tables.TemplateColumn(
        verbose_name="Lease",
        orderable=False,
        template_code=_LEASE_STATUS_LINK,
    )
    netbox_ip = tables.TemplateColumn(
        verbose_name="NetBox IP",
        orderable=False,
        template_code=_RESERVATION_SYNC_CELL,
    )
    actions = ActionsColumn(RESERVATION_ACTIONS)

    class Meta(GenericTable.Meta):
        empty_text = "No reservations found."
        fields = (
            "scope",
            "subnet_id",
            "hw_address",
            "identifier",
            "ip_address",
            "hostname",
            "options",
            "lease_status",
            "netbox_ip",
            "actions",
        )
        # ``identifier`` replaces ``hw_address`` by default: it shows the same value
        # for a MAC-keyed host and an actual value for every other identifier type.
        default_columns = (
            "scope",
            "identifier",
            "ip_address",
            "hostname",
            "options",
            "lease_status",
            "netbox_ip",
            "actions",
        )


class ReservationTable6(GenericTable):
    """Table for DHCPv6 host reservations returned from the Kea API."""

    scope = tables.TemplateColumn(verbose_name="Scope", orderable=False, template_code=_RESERVATION_SCOPE_CELL)
    subnet_id = tables.Column(verbose_name="Subnet ID")
    duid = tables.TemplateColumn(
        verbose_name="DUID",
        order_by="identifier",
        template_code=_DUID_CELL,
    )
    identifier = tables.TemplateColumn(
        verbose_name="Identifier",
        orderable=False,
        template_code=_IDENTIFIER_CELL,
    )
    ip_addresses = tables.TemplateColumn(
        verbose_name="IPv6 Addresses",
        accessor="ip_addresses",
        order_by="_ip_sort_key",
        template_code=_RESERVATION_ADDRESSES_CELL,
    )
    prefixes = tables.TemplateColumn(
        verbose_name="Delegated Prefixes",
        orderable=False,
        template_code=_RESERVATION_PREFIXES_CELL,
    )
    hostname = tables.Column(verbose_name="Hostname")
    options = tables.TemplateColumn(
        verbose_name="DHCP Options", orderable=False, template_code=_RESERVATION_OPTIONS_CELL
    )
    lease_status = tables.TemplateColumn(
        verbose_name="Lease",
        orderable=False,
        template_code=_LEASE_STATUS_LINK,
    )
    netbox_ip = tables.TemplateColumn(
        verbose_name="NetBox IP",
        orderable=False,
        template_code=_RESERVATION_SYNC_CELL,
    )
    actions = ActionsColumn(RESERVATION_ACTIONS)

    class Meta(GenericTable.Meta):
        empty_text = "No reservations found."
        fields = (
            "scope",
            "subnet_id",
            "duid",
            "identifier",
            "ip_addresses",
            "prefixes",
            "hostname",
            "options",
            "lease_status",
            "netbox_ip",
            "actions",
        )
        # ``identifier`` replaces ``duid`` by default for the same reason as v4, and
        # ``prefixes`` is the only thing a prefix-delegation-only host reserves.
        default_columns = (
            "scope",
            "identifier",
            "ip_addresses",
            "prefixes",
            "hostname",
            "options",
            "lease_status",
            "netbox_ip",
            "actions",
        )


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


class GlobalReservationTable4(ReservationTable4):
    """DHCPv4 reservation table aggregated across multiple servers.

    Extends the per-server table with a prepended *Server* column so that rows
    from different servers can be distinguished in the combined view.
    """

    server = _server_column()

    class Meta(ReservationTable4.Meta):
        fields = ("server", *ReservationTable4.Meta.fields)
        default_columns = ("server", *ReservationTable4.Meta.default_columns)


class GlobalReservationTable6(ReservationTable6):
    """DHCPv6 reservation table aggregated across multiple servers.

    Extends the per-server table with a prepended *Server* column so that rows
    from different servers can be distinguished in the combined view.
    """

    server = _server_column()

    class Meta(ReservationTable6.Meta):
        fields = ("server", *ReservationTable6.Meta.fields)
        default_columns = ("server", *ReservationTable6.Meta.default_columns)


class GlobalLeaseTable4(LeaseTable4):
    """DHCPv4 lease table aggregated across multiple servers.

    Extends the per-server table with a prepended *Server* column.
    """

    server = _server_column()

    class Meta(LeaseTable4.Meta):
        fields = ("server", *LeaseTable4.Meta.fields)
        default_columns = (
            "server",
            "ip_address",
            "hostname",
            "hw_address",
            "subnet_id",
            "state_label",
            "reserved",
            "netbox_ip",
        )


class GlobalLeaseTable6(LeaseTable6):
    """DHCPv6 lease table aggregated across multiple servers.

    Extends the per-server table with a prepended *Server* column.
    """

    server = _server_column()

    class Meta(LeaseTable6.Meta):
        fields = ("server", *LeaseTable6.Meta.fields)
        default_columns = (
            "server",
            "ip_address",
            "hostname",
            "duid",
            "subnet_id",
            "state_label",
            "reserved",
            "netbox_ip",
        )


class GlobalSubnetTable4(SubnetTable):
    """DHCPv4 subnet table aggregated across multiple servers.

    Extends the per-server table with a prepended *Server* column. All column
    definitions (pools, options, utilization, subnet linkify) are inherited from
    SubnetTable to avoid drift.
    """

    server = _server_column()

    class Meta(SubnetTable.Meta):
        empty_text = "No subnets found."
        fields = ("server", *SubnetTable.Meta.fields)
        default_columns = ("server", *SubnetTable.Meta.default_columns)


class GlobalSubnetTable6(GlobalSubnetTable4):
    """DHCPv6 subnet table aggregated across multiple servers."""

    class Meta(GlobalSubnetTable4.Meta):
        pass


class SharedNetworkTable(GenericTable):
    """Read-only table listing shared networks with their constituent subnets."""

    name = tables.Column(verbose_name="Name")
    description = tables.Column(verbose_name="Description", default="—")
    subnet_count = tables.Column(verbose_name="Subnets", orderable=False)
    subnets = tables.TemplateColumn(
        verbose_name="Subnet CIDRs",
        orderable=False,
        template_code=(
            "{% for item in record.subnet_links %}"
            '<a href="{{ item.url }}" class="badge text-bg-primary me-1">{{ item.cidr }}</a>'
            "{% empty %}—{% endfor %}"
        ),
    )
    actions = tables.TemplateColumn(
        verbose_name="",
        orderable=False,
        template_code=(
            "{% if record.can_change %}"
            "{% if record.dhcp_version == 4 %}"
            '<a href="{% url "plugins:netbox_kea:server_shared_network4_edit" record.server_pk record.name %}"'
            ' class="btn btn-sm btn-warning me-1" aria-label="Edit {{ record.name }}">'
            '<i class="mdi mdi-pencil" aria-hidden="true"></i></a>'
            '<a href="{% url "plugins:netbox_kea:server_shared_network4_delete" record.server_pk record.name %}"'
            ' class="btn btn-sm btn-danger" aria-label="Delete {{ record.name }}">'
            '<i class="mdi mdi-delete" aria-hidden="true"></i></a>'
            "{% else %}"
            '<a href="{% url "plugins:netbox_kea:server_shared_network6_edit" record.server_pk record.name %}"'
            ' class="btn btn-sm btn-warning me-1" aria-label="Edit {{ record.name }}">'
            '<i class="mdi mdi-pencil" aria-hidden="true"></i></a>'
            '<a href="{% url "plugins:netbox_kea:server_shared_network6_delete" record.server_pk record.name %}"'
            ' class="btn btn-sm btn-danger" aria-label="Delete {{ record.name }}">'
            '<i class="mdi mdi-delete" aria-hidden="true"></i></a>'
            "{% endif %}"
            "{% endif %}"
        ),
    )

    class Meta(GenericTable.Meta):
        empty_text = "No shared networks configured."
        fields = ("name", "description", "subnet_count", "subnets", "actions")
        default_columns = ("name", "description", "subnet_count", "subnets", "actions")


class GlobalSharedNetworkTable(SharedNetworkTable):
    """Shared network table aggregated across multiple servers.

    Extends the per-server table with a prepended *Server* column so that rows
    from different servers can be distinguished in the combined view.
    """

    server = _server_column()

    class Meta(SharedNetworkTable.Meta):
        fields = ("server", *SharedNetworkTable.Meta.fields)
        default_columns = ("server", *SharedNetworkTable.Meta.default_columns)

from typing import Any, Literal

from django import forms
from django.core.exceptions import ValidationError
from netaddr import EUI, AddrFormatError, IPAddress, IPNetwork, IPRange, mac_unix_expanded
from netbox.forms import NetBoxModelBulkEditForm, NetBoxModelFilterSetForm, NetBoxModelForm, NetBoxModelImportForm
from utilities.forms import BOOLEAN_WITH_BLANK_CHOICES
from utilities.forms.fields import TagFilterField

from . import constants
from .models import Server
from .utilities import is_hex_string


class ServerForm(NetBoxModelForm):
    """NetBox model form for creating and editing Kea Server objects."""

    class Meta:
        model = Server
        fields = (
            "name",
            "server_url",
            "username",
            "password",
            "ssl_verify",
            "client_cert_path",
            "client_key_path",
            "ca_file_path",
            "dhcp6",
            "dhcp4",
            "dhcp4_url",
            "dhcp6_url",
            "has_control_agent",
            "tags",
        )
        widgets = {
            "password": forms.PasswordInput(),
        }


class VeryHiddenInput(forms.HiddenInput):
    """Returns an empty string on render."""

    input_type = "hidden"
    template_name = ""

    def render(self, name: str, value: Any, attrs: Any, renderer: Any) -> str:
        """Return an empty string, suppressing all HTML output."""
        return ""


class ServerFilterForm(NetBoxModelFilterSetForm):
    """Filter form for the Server list view."""

    model = Server
    tag = TagFilterField(model)
    name = forms.CharField(
        label="Name",
        required=False,
        help_text="Case-insensitive substring match",
    )
    server_url = forms.CharField(
        label="Server URL",
        required=False,
        help_text="Case-insensitive substring match",
    )
    has_control_agent = forms.NullBooleanField(
        label="Has Control Agent",
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )
    dhcp4 = forms.NullBooleanField(
        label="DHCPv4",
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )
    dhcp6 = forms.NullBooleanField(
        label="DHCPv6",
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )


class ServerBulkEditForm(NetBoxModelBulkEditForm):
    """Bulk-edit form for Kea Server objects."""

    has_control_agent = forms.NullBooleanField(
        label="Has Control Agent",
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )
    dhcp4 = forms.NullBooleanField(
        label="DHCPv4 enabled",
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )
    dhcp6 = forms.NullBooleanField(
        label="DHCPv6 enabled",
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )

    model = Server
    nullable_fields: list[str] = []


class ServerImportForm(NetBoxModelImportForm):
    """CSV/YAML bulk-import form for Server objects."""

    class Meta:
        model = Server
        fields = (
            "name",
            "server_url",
            "username",
            "password",
            "ssl_verify",
            "dhcp4",
            "dhcp6",
            "dhcp4_url",
            "dhcp6_url",
            "has_control_agent",
        )


class BaseLeasesSarchForm(forms.Form):
    """Base search form for DHCP lease queries; subclassed per IP version."""

    q = forms.CharField(label="Search", required=False)
    page = forms.CharField(required=False, widget=VeryHiddenInput)
    state = forms.ChoiceField(
        label="State",
        required=False,
        choices=constants.LEASE_STATE_CHOICES,
        help_text="Filter results by lease state.",
    )

    def clean(self) -> dict[str, Any] | None:
        """Validate and normalise search fields according to the selected search type."""
        ip_version = self.Meta.ip_version
        cleaned_data = super().clean()
        q = cleaned_data.get("q")
        by = cleaned_data.get("by")

        if q and not by:
            raise ValidationError({"by": "Search attribute is empty."})
        if by and not q:
            raise ValidationError({"q": "Search value is empty."})

        if by == constants.BY_SUBNET:
            try:
                if "/" not in q:
                    raise ValidationError({"q": "CIDR mask is required"})
                net = IPNetwork(q, version=ip_version)
                if net.ip != net.cidr.ip:
                    raise ValidationError({"q": f"{net} is not a valid prefix. Did you mean {net.cidr}?"})
                cleaned_data["q"] = net
            except (AddrFormatError, TypeError, ValueError) as e:
                raise ValidationError({"q": f"Invalid IPv{ip_version} subnet."}) from e
        elif by == constants.BY_SUBNET_ID:
            try:
                i = int(q)
                if i <= 0:
                    raise ValidationError({"q": "Invalid subnet ID."})
                cleaned_data["q"] = i
            except ValueError as e:
                raise ValidationError({"q": "Subnet ID must be an integer."}) from e
        elif by == constants.BY_IP:
            try:
                # use IPAddress to normalize values
                cleaned_data["q"] = str(IPAddress(q, version=ip_version))
            except (AddrFormatError, TypeError, ValueError) as e:
                raise ValidationError({"q": f"Invalid IPv{ip_version} address."}) from e
        elif by == constants.BY_HW_ADDRESS:
            try:
                cleaned_data["q"] = str(EUI(q, version=48, dialect=mac_unix_expanded))
            except (AddrFormatError, TypeError, ValueError) as e:
                raise ValidationError({"q": "Invalid hardware address."}) from e
        elif by == constants.BY_DUID:
            if not is_hex_string(q, constants.DUID_MIN_OCTETS, constants.DUID_MAX_OCTETS):
                raise ValidationError({"q": "Invalid DUID."})
            cleaned_data["q"] = q.replace("-", "")
        elif by == constants.BY_CLIENT_ID:
            if not is_hex_string(q, constants.CLIENT_ID_MIN_OCTETS, constants.CLIENT_ID_MAX_OCTETS):
                raise ValidationError({"q": "Invalid client ID."})
            cleaned_data["q"] = q.replace("-", "")

        # Convert state to int or None for the view to use.
        state_str = cleaned_data.get("state", "")
        cleaned_data["state"] = int(state_str) if state_str != "" else None

        page = cleaned_data["page"]
        if page:
            if by != constants.BY_SUBNET:
                raise ValidationError({"page": "page is only supported with subnet."})
            try:
                page_ip = IPAddress(page, version=ip_version)
                if page_ip not in cleaned_data["q"]:
                    raise ValidationError({"page": "page is not in the given subnet"})

                cleaned_data["page"] = str(page_ip)
            except AddrFormatError as e:
                raise ValidationError({"page": "Invalid IP."}) from e

        return cleaned_data


class Leases4SearchForm(BaseLeasesSarchForm):
    """Search form for DHCPv4 leases."""

    by = forms.ChoiceField(
        label="Attribute",
        choices=(
            ("", "— All Leases —"),
            (constants.BY_IP, "IP Address"),
            (constants.BY_HOSTNAME, "Hostname"),
            (constants.BY_HW_ADDRESS, "Hardware Address"),
            (constants.BY_CLIENT_ID, "Client ID"),
            (constants.BY_SUBNET, "Subnet"),
            (constants.BY_SUBNET_ID, "Subnet ID"),
        ),
        required=False,
    )

    class Meta:
        ip_version = 4


class Leases6SearchForm(BaseLeasesSarchForm):
    """Search form for DHCPv6 leases."""

    by = forms.ChoiceField(
        label="Attribute",
        choices=(
            ("", "— All Leases —"),
            (constants.BY_IP, "IP Address"),
            (constants.BY_HOSTNAME, "Hostname"),
            (constants.BY_DUID, "DUID"),
            (constants.BY_SUBNET, "Subnet"),
            (constants.BY_SUBNET_ID, "Subnet ID"),
        ),
        required=False,
    )

    class Meta:
        ip_version = 6


class CombinedLeases4SearchForm(Leases4SearchForm):
    """Lease search form for the combined multi-server view (q and by are optional).

    When only *state* is provided the view falls back to ``lease4-get-page``
    enumeration instead of a targeted search.
    """

    q = forms.CharField(label="Search", required=False)


class CombinedLeases6SearchForm(Leases6SearchForm):
    """Lease search form for the combined multi-server view (q and by are optional).

    When only *state* is provided the view falls back to ``lease6-get-page``
    enumeration instead of a targeted search.
    """

    q = forms.CharField(label="Search", required=False)


class MultipleIPField(forms.MultipleChoiceField):
    """Form field accepting a list of IP addresses validated against a specific IP version."""

    def __init__(self, version: Literal[6, 4], *args, **kwargs) -> None:
        """Initialise with the required IP *version* (4 or 6)."""
        self._version = version
        super().__init__(*args, widget=forms.MultipleHiddenInput, **kwargs)

    def clean(self, value: Any) -> Any:
        """Validate and normalise each IP address in the list."""
        if not isinstance(value, list):
            raise forms.ValidationError(f"Expected a list, got {type(value)}.")

        if len(value) == 0:
            raise forms.ValidationError("IP address list is empty.")

        try:
            return [str(IPAddress(ip, version=self._version)) for ip in value]
        except (AddrFormatError, ValueError) as e:
            raise forms.ValidationError("Invalid IP address.") from e


class BaseLeaseDeleteForm(forms.Form):
    """Base form for confirming bulk deletion of DHCP leases."""

    # NetBox v4.4 requires a background_job field for the bulk_delete.html
    # template.
    background_job = forms.CharField(required=False, widget=VeryHiddenInput, label="background_job")
    return_url = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )


class Lease6DeleteForm(BaseLeaseDeleteForm):
    """Delete form for DHCPv6 leases; validates a list of IPv6 addresses."""

    pk = MultipleIPField(6)


class Lease4DeleteForm(BaseLeaseDeleteForm):
    """Delete form for DHCPv4 leases; validates a list of IPv4 addresses."""

    pk = MultipleIPField(4)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Reservation Management forms
# ─────────────────────────────────────────────────────────────────────────────

_IDENTIFIER_TYPE_CHOICES_V4 = [
    ("hw-address", "Hardware Address"),
    ("client-id", "Client ID"),
    ("circuit-id", "Circuit ID"),
    ("flex-id", "Flex ID"),
]

_IDENTIFIER_TYPE_CHOICES_V6 = [
    ("duid", "DUID"),
    ("hw-address", "Hardware Address"),
    ("client-id", "Client ID"),
    ("flex-id", "Flex ID"),
]


class Reservation4Form(forms.Form):
    """Form for creating or editing a DHCPv4 host reservation."""

    subnet_id = forms.IntegerField(
        label="Subnet ID",
        min_value=1,
        help_text="Kea subnet ID the reservation belongs to.",
    )
    ip_address = forms.CharField(
        label="IP Address",
        help_text="Fixed IPv4 address to assign to this reservation.",
    )
    identifier_type = forms.ChoiceField(
        label="Identifier Type",
        choices=_IDENTIFIER_TYPE_CHOICES_V4,
        help_text="Method used to identify the DHCP client.",
    )
    identifier = forms.CharField(
        label="Identifier",
        help_text="Client identifier value (e.g. hw-address: aa:bb:cc:dd:ee:ff).",
    )
    hostname = forms.CharField(
        label="Hostname",
        required=False,
        help_text="Optional hostname to assign with this reservation.",
    )
    sync_to_netbox = forms.BooleanField(
        label="Sync to NetBox IPAM",
        required=False,
        help_text="Create or update an IPAddress in NetBox with status=reserved.",
    )

    def clean_ip_address(self) -> str:
        """Validate that the value is a valid IPv4 address."""
        val = self.cleaned_data["ip_address"]
        try:
            addr = IPAddress(val)
        except (AddrFormatError, ValueError) as exc:
            raise ValidationError("Enter a valid IPv4 address.") from exc
        if addr.version != 4:
            raise ValidationError("Must be an IPv4 address.")
        return str(addr)

    def clean(self) -> dict[str, Any] | None:
        """Cross-validate identifier value against identifier_type."""
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        id_type = cleaned.get("identifier_type")
        identifier = cleaned.get("identifier", "").strip()
        if not identifier:
            return cleaned
        if id_type == "hw-address":
            try:
                EUI(identifier, version=48)
            except (AddrFormatError, ValueError):
                self.add_error("identifier", "Enter a valid hardware address (e.g. aa:bb:cc:dd:ee:ff).")
        elif id_type == "client-id":
            if not is_hex_string(identifier, constants.CLIENT_ID_MIN_OCTETS, constants.CLIENT_ID_MAX_OCTETS):
                self.add_error(
                    "identifier", "Enter a valid client-id as colon-separated hex octets (e.g. 01:aa:bb:cc:dd:ee:ff)."
                )
        return cleaned


class Reservation6Form(forms.Form):
    """Form for creating or editing a DHCPv6 host reservation."""

    subnet_id = forms.IntegerField(
        label="Subnet ID",
        min_value=1,
        help_text="Kea subnet ID the reservation belongs to.",
    )
    ip_addresses = forms.CharField(
        label="IPv6 Addresses",
        help_text="Comma-separated list of IPv6 addresses to assign.",
    )
    identifier_type = forms.ChoiceField(
        label="Identifier Type",
        choices=_IDENTIFIER_TYPE_CHOICES_V6,
        help_text="Method used to identify the DHCP client.",
    )
    identifier = forms.CharField(
        label="Identifier",
        help_text="Client identifier value (e.g. duid: 00:01:02:03:04:05:06:07).",
    )
    hostname = forms.CharField(
        label="Hostname",
        required=False,
        help_text="Optional hostname to assign with this reservation.",
    )
    sync_to_netbox = forms.BooleanField(
        label="Sync to NetBox IPAM",
        required=False,
        help_text="Create or update an IPAddress in NetBox with status=reserved.",
    )

    def clean_ip_addresses(self) -> str:
        """Validate that all values are valid IPv6 addresses."""
        val = self.cleaned_data["ip_addresses"]
        cleaned: list[str] = []
        for raw in val.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                addr = IPAddress(raw)
            except (AddrFormatError, ValueError) as exc:
                raise ValidationError(f"'{raw}' is not a valid IP address.") from exc
            if addr.version != 6:
                raise ValidationError(f"'{raw}' is not a valid IPv6 address.")
            cleaned.append(str(addr))
        if not cleaned:
            raise ValidationError("Enter at least one valid IPv6 address.")
        return ",".join(cleaned)

    def clean(self) -> dict[str, Any] | None:
        """Cross-validate identifier value against identifier_type."""
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        id_type = cleaned.get("identifier_type")
        identifier = cleaned.get("identifier", "").strip()
        if not identifier:
            return cleaned
        if id_type == "duid":
            if not is_hex_string(identifier, constants.DUID_MIN_OCTETS, constants.DUID_MAX_OCTETS):
                self.add_error("identifier", "Enter a valid DUID as colon-separated hex octets.")
        elif id_type == "hw-address":
            try:
                EUI(identifier, version=48)
            except (AddrFormatError, ValueError):
                self.add_error("identifier", "Enter a valid hardware address (e.g. aa:bb:cc:dd:ee:ff).")
        elif id_type == "client-id":
            if not is_hex_string(identifier, constants.CLIENT_ID_MIN_OCTETS, constants.CLIENT_ID_MAX_OCTETS):
                self.add_error(
                    "identifier", "Enter a valid client-id as colon-separated hex octets (e.g. 01:aa:bb:cc:dd:ee:ff)."
                )
        return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: Global multi-server filter forms
# ─────────────────────────────────────────────────────────────────────────────


class GlobalServer4FilterForm(forms.Form):
    """Server multi-select for the global DHCPv4 views."""

    server = forms.ModelMultipleChoiceField(
        queryset=Server.objects.none(),
        required=False,
        label="Servers",
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave blank to query all DHCPv4-enabled servers.",
    )

    def __init__(self, *args, **kwargs):
        """Evaluate queryset at instantiation time, not class definition time."""
        super().__init__(*args, **kwargs)
        self.fields["server"].queryset = Server.objects.filter(dhcp4=True)


class GlobalServer6FilterForm(forms.Form):
    """Server multi-select for the global DHCPv6 views."""

    server = forms.ModelMultipleChoiceField(
        queryset=Server.objects.none(),
        required=False,
        label="Servers",
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave blank to query all DHCPv6-enabled servers.",
    )

    def __init__(self, *args, **kwargs):
        """Evaluate queryset at instantiation time, not class definition time."""
        super().__init__(*args, **kwargs)
        self.fields["server"].queryset = Server.objects.filter(dhcp6=True)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 10: Pool management forms
# ─────────────────────────────────────────────────────────────────────────────


def _validate_pool_string(pool: str) -> None:
    """Validate a single pool string (range or CIDR).

    Raises:
        ValidationError: If the pool string is not a valid IP range or CIDR.

    """
    if "-" in pool and "/" not in pool:
        parts = pool.split("-", 1)
        if len(parts) != 2:
            raise forms.ValidationError(f"Invalid pool range '{pool}': expected 'start-end' format.")
        try:
            IPRange(parts[0].strip(), parts[1].strip())
        except (AddrFormatError, ValueError) as exc:
            raise forms.ValidationError(f"Invalid pool range '{pool}': {exc}") from exc
    elif "/" in pool:
        try:
            IPNetwork(pool, implicit_prefix=False)
        except (AddrFormatError, ValueError) as exc:
            raise forms.ValidationError(f"Invalid pool CIDR '{pool}': {exc}") from exc
    else:
        raise forms.ValidationError(
            f"Invalid pool format '{pool}': use range (e.g. 10.0.0.1-10.0.0.50) or CIDR (e.g. 10.0.0.0/28)."
        )


class PoolAddForm(forms.Form):
    """Form for adding a DHCP pool to an existing subnet."""

    pool = forms.CharField(
        label="Pool",
        help_text=("Pool range (e.g. <code>10.0.0.50-10.0.0.99</code>) or CIDR (e.g. <code>10.0.0.0/28</code>)."),
        max_length=255,
    )

    def clean_pool(self) -> str:  # noqa: D102
        value = self.cleaned_data["pool"].strip()
        _validate_pool_string(value)
        if "-" in value and "/" not in value:
            start, end = value.split("-", 1)
            return f"{start.strip()}-{end.strip()}"
        return value


class _SubnetBaseForm(forms.Form):
    """Shared fields and validators for subnet add and edit forms.

    Subclasses add identity fields (subnet CIDR / ID for add; hidden CIDR for edit).
    """

    pools = forms.CharField(
        label="Pools",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One pool per line, e.g. <code>10.0.0.100-10.0.0.200</code>",
    )
    gateway = forms.CharField(
        label="Default gateway",
        required=False,
        max_length=50,
        help_text="IP address of the default gateway (option <code>routers</code>).",
    )
    dns_servers = forms.CharField(
        label="DNS servers",
        required=False,
        max_length=255,
        help_text="Comma-separated IP addresses.",
    )
    ntp_servers = forms.CharField(
        label="NTP servers",
        required=False,
        max_length=255,
        help_text="Comma-separated IP addresses or hostnames.",
    )

    def clean_pools(self) -> list[str]:  # noqa: D102
        value = self.cleaned_data["pools"].strip()
        if not value:
            return []
        pools = [p.strip() for p in value.splitlines() if p.strip()]
        normalized = []
        for pool in pools:
            _validate_pool_string(pool)
            if "-" in pool and "/" not in pool:
                start, end = pool.split("-", 1)
                normalized.append(f"{start.strip()}-{end.strip()}")
            else:
                normalized.append(pool)
        return normalized

    def clean_gateway(self) -> str:  # noqa: D102
        import ipaddress

        value = self.cleaned_data["gateway"].strip()
        if not value:
            return ""
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise forms.ValidationError(f"Invalid gateway IP address: {exc}") from exc
        return value

    def clean_dns_servers(self) -> list[str]:  # noqa: D102
        import ipaddress

        value = self.cleaned_data["dns_servers"].strip()
        if not value:
            return []
        entries = [s.strip() for s in value.split(",") if s.strip()]
        for entry in entries:
            try:
                ipaddress.ip_address(entry)
            except ValueError as exc:  # noqa: PERF203
                raise forms.ValidationError(f"Invalid DNS server IP address '{entry}': {exc}") from exc
        return entries

    def clean_ntp_servers(self) -> list[str]:  # noqa: D102
        value = self.cleaned_data["ntp_servers"].strip()
        if not value:
            return []
        return [s.strip() for s in value.split(",") if s.strip()]


class SubnetAddForm(_SubnetBaseForm):
    """Form for adding a new DHCP subnet to Kea."""

    subnet = forms.CharField(
        label="Subnet CIDR",
        max_length=50,
        help_text="e.g. <code>10.0.0.0/24</code> or <code>2001:db8::/48</code>",
    )
    subnet_id = forms.IntegerField(
        label="Subnet ID",
        required=False,
        min_value=1,
        help_text="Leave blank for Kea to auto-assign.",
    )

    def clean_subnet(self) -> str:  # noqa: D102
        import ipaddress

        value = self.cleaned_data["subnet"].strip()
        try:
            ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise forms.ValidationError(f"Invalid subnet CIDR: {exc}") from exc
        return value

    def clean(self) -> dict[str, Any] | None:
        """Validate that gateway and DNS servers belong to the same IP family as the subnet."""
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        import ipaddress

        subnet_str = cleaned.get("subnet", "")
        try:
            subnet_net = ipaddress.ip_network(subnet_str, strict=False)
        except ValueError:
            return cleaned

        subnet_version = subnet_net.version

        gateway = cleaned.get("gateway", "")
        if gateway:
            try:
                gw_version = ipaddress.ip_address(gateway).version
            except ValueError:
                gw_version = None
            if gw_version and gw_version != subnet_version:
                self.add_error(
                    "gateway",
                    f"Gateway must be an IPv{subnet_version} address to match the subnet family.",
                )

        dns_servers = cleaned.get("dns_servers") or []
        if isinstance(dns_servers, list):
            for dns in dns_servers:
                try:
                    dns_version = ipaddress.ip_address(dns).version
                except ValueError:
                    continue
                if dns_version != subnet_version:
                    self.add_error(
                        "dns_servers",
                        f"DNS server '{dns}' must be an IPv{subnet_version} address to match the subnet family.",
                    )
                    break

        return cleaned


class SubnetEditForm(_SubnetBaseForm):
    """Form for editing an existing DHCP subnet in Kea.

    The subnet CIDR and ID are immutable — they are passed as a hidden field and
    used by the view to call ``subnet{v}-update``.  All other fields are optional;
    leaving a field blank means "clear that option".

    ``shared_network`` choices are set dynamically by the view at render time.
    ``current_network`` is a hidden field tracking the network before any change.
    """

    subnet_cidr = forms.CharField(widget=forms.HiddenInput())
    valid_lft = forms.IntegerField(
        label="Valid lifetime (s)",
        required=False,
        min_value=1,
        help_text="Preferred lease lifetime in seconds.",
    )
    min_valid_lft = forms.IntegerField(
        label="Min valid lifetime (s)",
        required=False,
        min_value=1,
        help_text="Minimum lease lifetime in seconds.",
    )
    max_valid_lft = forms.IntegerField(
        label="Max valid lifetime (s)",
        required=False,
        min_value=1,
        help_text="Maximum lease lifetime in seconds.",
    )
    shared_network = forms.ChoiceField(
        label="Shared Network",
        required=False,
        choices=[],
        help_text="Assign this subnet to a shared network, or leave blank to use the global address pool.",
    )
    current_network = forms.CharField(widget=forms.HiddenInput(), required=False)


# ─────────────────────────────────────────────────────────────────────────────
# Reservation search / filter
# ─────────────────────────────────────────────────────────────────────────────


class ReservationSearchForm(forms.Form):
    """Search form for filtering reservations on the per-server reservation tabs.

    All fields are optional — submitting an empty form shows all reservations.
    Client-side filtering is applied to the already-fetched reservation list.
    """

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(attrs={"placeholder": "IP, hostname, or identifier"}),
        help_text="Case-insensitive search across IP address, hostname, and hardware address / DUID.",
    )
    subnet_id = forms.IntegerField(
        required=False,
        label="Subnet ID",
        min_value=1,
        help_text="Filter to a specific Kea subnet ID.",
    )


class SubnetSearchForm(forms.Form):
    """Search form for filtering the combined subnets view.

    All fields are optional — submitting an empty form shows all subnets.
    Client-side filtering is applied to the already-fetched subnet list.
    """

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(attrs={"placeholder": "CIDR prefix (e.g. 10.0 or 2001:db8)"}),
        help_text="Case-insensitive substring match on the subnet CIDR.",
    )
    subnet_id = forms.IntegerField(
        required=False,
        label="Subnet ID",
        min_value=1,
        help_text="Filter to a specific Kea subnet ID.",
    )


class DHCPDisableForm(forms.Form):
    """Confirmation form for disabling a Kea DHCP service.

    *max_period* is optional.  When supplied, Kea automatically re-enables the
    service after that many seconds.  When omitted the service stays disabled
    until an explicit ``dhcp-enable`` call is made.
    """

    max_period = forms.IntegerField(
        required=False,
        min_value=1,
        label="Max period (seconds)",
        help_text=(
            "How long DHCP processing should remain disabled. Leave blank to keep disabled until manually re-enabled."
        ),
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "e.g. 300"}),
    )
    confirm = forms.BooleanField(
        required=True,
        widget=forms.HiddenInput(),
        initial=True,
    )


class _BaseBulkReservationImportForm(forms.Form):
    """Base class for bulk reservation CSV import forms."""

    csv_file = forms.FileField(
        label="CSV file",
        help_text="Upload a UTF-8 CSV file. Lines starting with '#' are skipped.",
    )


class Reservation4BulkImportForm(_BaseBulkReservationImportForm):
    """Bulk import form for DHCPv4 reservations.

    Expected columns: ``ip-address``, ``hw-address``, ``hostname`` (optional), ``subnet-id``.
    """


class Reservation6BulkImportForm(_BaseBulkReservationImportForm):
    """Bulk import form for DHCPv6 reservations.

    Expected columns: ``ip-addresses``, ``duid``, ``hostname`` (optional), ``subnet-id``.
    Separate multiple IPv6 addresses per host with a semicolon inside the ``ip-addresses`` cell.
    """


class _BaseBulkLeaseImportForm(forms.Form):
    """Base class for bulk lease CSV import forms."""

    csv_file = forms.FileField(
        label="CSV file",
        help_text="Upload a UTF-8 CSV file. Lines starting with '#' are skipped.",
    )


class Lease4BulkImportForm(_BaseBulkLeaseImportForm):
    """Bulk import form for DHCPv4 leases.

    Required column: ``ip-address``.
    Optional: ``hw-address``, ``subnet-id``, ``valid-lft``, ``hostname``.
    """


class Lease6BulkImportForm(_BaseBulkLeaseImportForm):
    """Bulk import form for DHCPv6 leases.

    Required columns: ``ip-address``, ``duid``, ``iaid``.
    Optional: ``subnet-id``, ``valid-lft``, ``hostname``.
    """


class SubnetOptionsForm(forms.Form):
    """A single subnet option-data row (name/data/always_send)."""

    name = forms.CharField(
        max_length=128,
        help_text="Kea option name (e.g. routers, domain-name-servers).",
    )
    data = forms.CharField(
        max_length=512,
        help_text="Option value (e.g. 10.0.0.1 or 8.8.8.8, 8.8.4.4).",
    )
    always_send = forms.BooleanField(
        required=False,
        help_text="Send option even when not requested by the client.",
    )


SubnetOptionsFormSet = forms.formset_factory(SubnetOptionsForm, extra=1, can_delete=True)


class Lease4EditForm(forms.Form):
    """Form for editing a DHCPv4 lease in-place."""

    hostname = forms.CharField(
        max_length=255,
        required=False,
        help_text="Client hostname (leave blank to keep current).",
    )
    hw_address = forms.CharField(
        max_length=17,
        required=False,
        label="Hardware address",
        help_text="MAC address in xx:xx:xx:xx:xx:xx format (leave blank to keep current).",
    )
    valid_lft = forms.IntegerField(
        min_value=0,
        required=False,
        label="Valid lifetime (s)",
        help_text="Lease lifetime in seconds (leave blank to keep current).",
    )

    def clean_hw_address(self) -> str:  # noqa: D102
        value = self.cleaned_data.get("hw_address", "").strip()
        if not value:
            return value
        try:
            EUI(value, version=48)
        except (AddrFormatError, ValueError) as exc:
            raise ValidationError("Enter a valid MAC address (e.g. aa:bb:cc:dd:ee:ff).") from exc
        return value


class Lease6EditForm(forms.Form):
    """Form for editing a DHCPv6 lease in-place."""

    hostname = forms.CharField(
        max_length=255,
        required=False,
        help_text="Client hostname (leave blank to keep current).",
    )
    duid = forms.CharField(
        max_length=255,
        required=False,
        label="DUID",
        help_text="Client DUID in hex (leave blank to keep current).",
    )
    valid_lft = forms.IntegerField(
        min_value=0,
        required=False,
        label="Valid lifetime (s)",
        help_text="Lease lifetime in seconds (leave blank to keep current).",
    )

    def clean_duid(self) -> str:  # noqa: D102
        value = self.cleaned_data.get("duid", "").strip()
        if not value:
            return value
        if not is_hex_string(value, constants.DUID_MIN_OCTETS, constants.DUID_MAX_OCTETS):
            raise ValidationError("Enter a valid DUID as colon-separated hex octets.")
        return value


class Lease4AddForm(forms.Form):
    """Form for manually creating a new DHCPv4 lease."""

    ip_address = forms.CharField(
        label="IP Address",
        help_text="IPv4 address to assign.",
    )
    subnet_id = forms.IntegerField(
        label="Subnet ID",
        min_value=1,
        required=False,
        help_text="Kea subnet ID (optional; Kea will infer from IP if omitted).",
    )
    hw_address = forms.CharField(
        max_length=17,
        label="Hardware address",
        required=False,
        help_text="Client MAC address in xx:xx:xx:xx:xx:xx format.",
    )
    valid_lft = forms.IntegerField(
        min_value=0,
        label="Valid lifetime (s)",
        required=False,
        help_text="Lease lifetime in seconds.",
    )
    hostname = forms.CharField(
        max_length=255,
        required=False,
        help_text="Client hostname (optional).",
    )
    sync_to_netbox = forms.BooleanField(
        label="Sync to NetBox IPAM",
        required=False,
        help_text="Create/update IPAddress in NetBox with status=active.",
    )

    def clean_ip_address(self) -> str:
        """Validate that the value is a valid IPv4 address."""
        val = self.cleaned_data["ip_address"]
        try:
            addr = IPAddress(val)
        except (AddrFormatError, ValueError) as exc:
            raise ValidationError("Enter a valid IPv4 address.") from exc
        if addr.version != 4:
            raise ValidationError("Must be an IPv4 address.")
        return str(addr)

    def clean_hw_address(self) -> str:  # noqa: D102
        value = self.cleaned_data.get("hw_address", "").strip()
        if not value:
            return value
        try:
            EUI(value, version=48)
        except (AddrFormatError, ValueError) as exc:
            raise ValidationError("Enter a valid MAC address (e.g. aa:bb:cc:dd:ee:ff).") from exc
        return value


class Lease6AddForm(forms.Form):
    """Form for manually creating a new DHCPv6 lease."""

    ip_address = forms.CharField(
        label="IPv6 Address",
        help_text="IPv6 address to assign.",
    )
    duid = forms.CharField(
        label="DUID",
        help_text="Client DUID in colon-separated hex (e.g. 00:01:02:03).",
    )
    iaid = forms.IntegerField(
        label="IAID",
        min_value=0,
        max_value=4294967295,
        help_text="Identity Association ID (32-bit unsigned integer).",
    )
    subnet_id = forms.IntegerField(
        label="Subnet ID",
        min_value=1,
        required=False,
        help_text="Kea subnet ID (optional; Kea will infer from IP if omitted).",
    )
    valid_lft = forms.IntegerField(
        min_value=0,
        label="Valid lifetime (s)",
        required=False,
        help_text="Lease lifetime in seconds.",
    )
    hostname = forms.CharField(
        max_length=255,
        required=False,
        help_text="Client hostname (optional).",
    )
    sync_to_netbox = forms.BooleanField(
        label="Sync to NetBox IPAM",
        required=False,
        help_text="Create/update IPAddress in NetBox with status=active.",
    )

    def clean_ip_address(self) -> str:
        """Validate that the value is a valid IPv6 address."""
        val = self.cleaned_data["ip_address"]
        try:
            addr = IPAddress(val)
        except (AddrFormatError, ValueError) as exc:
            raise ValidationError("Enter a valid IPv6 address.") from exc
        if addr.version != 6:
            raise ValidationError("Must be an IPv6 address.")
        return str(addr)

    def clean_duid(self) -> str:  # noqa: D102
        value = self.cleaned_data.get("duid", "").strip()
        if not value:
            return value
        if not is_hex_string(value, constants.DUID_MIN_OCTETS, constants.DUID_MAX_OCTETS):
            raise ValidationError("Enter a valid DUID as colon-separated hex octets.")
        return value


class SharedNetworkForm(forms.Form):
    """Form for adding a new Kea shared network."""

    name = forms.CharField(
        max_length=128,
        label="Network name",
        help_text="Unique name for the shared network (letters, digits, hyphens and underscores only).",
    )

    def clean_name(self) -> str:
        """Validate that the name contains no whitespace or forbidden characters."""
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        import re

        if not re.match(r"^[\w-]+$", name):
            raise forms.ValidationError("Name may only contain letters, digits, hyphens and underscores.")
        return name


# ---------------------------------------------------------------------------
# Option-def form
# ---------------------------------------------------------------------------

_KEA_OPTION_TYPES = [
    ("binary", "binary"),
    ("boolean", "boolean"),
    ("empty", "empty"),
    ("fqdn", "fqdn"),
    ("ipv4-address", "ipv4-address"),
    ("ipv6-address", "ipv6-address"),
    ("ipv6-prefix", "ipv6-prefix"),
    ("psid", "psid"),
    ("record", "record"),
    ("string", "string"),
    ("tuple", "tuple"),
    ("uint8", "uint8"),
    ("uint16", "uint16"),
    ("uint32", "uint32"),
    ("uint64", "uint64"),
    ("int8", "int8"),
    ("int16", "int16"),
    ("int32", "int32"),
]


class OptionDefForm(forms.Form):
    """Form for adding a custom DHCP option definition."""

    name = forms.CharField(
        max_length=128,
        label="Name",
        help_text="Option name (letters, digits, hyphens and underscores only).",
    )
    code = forms.IntegerField(
        min_value=1,
        max_value=65535,
        label="Code",
        help_text="Option code number (1–254 for DHCPv4; 1–65535 for DHCPv6).",
    )
    type = forms.ChoiceField(
        choices=_KEA_OPTION_TYPES,
        label="Type",
        help_text="Data type for this option.",
    )
    space = forms.CharField(
        max_length=64,
        label="Space",
        help_text="Option space (e.g. 'dhcp4' or 'dhcp6').",
    )
    array = forms.BooleanField(
        required=False,
        label="Array",
        help_text="If checked, the option carries multiple values of the given type.",
    )

    def clean_name(self) -> str:
        """Validate option name format."""
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        import re

        if not re.match(r"^[\w-]+$", name):
            raise forms.ValidationError("Name may only contain letters, digits, hyphens and underscores.")
        return name

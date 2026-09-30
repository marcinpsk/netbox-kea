import copy
import ipaddress
from typing import Any, Generic, TypeVar, cast

from django import forms
from django.core.exceptions import ValidationError
from ipam.models import VRF
from netaddr import EUI, AddrFormatError, IPAddress, IPNetwork, mac_unix_expanded
from netbox.forms import NetBoxModelBulkEditForm, NetBoxModelFilterSetForm, NetBoxModelForm, NetBoxModelImportForm
from utilities.forms import BOOLEAN_WITH_BLANK_CHOICES
from utilities.forms.fields import CSVModelChoiceField, TagFilterField
from utilities.forms.rendering import FieldSet

from . import constants
from .config_write import SUBNET_LIST_UNCONFIRMED, subnet_changed
from .constants import Family, IPNetworkValue
from .dhcp_options import InvalidAddress, address_list, parse_dhcp_option
from .kea import SharedNetworkEdit, SubnetEdit, SubnetFields, description_as_shown, subnet_network
from .models import Server
from .pools import Pool, parse_pool
from .reservation_transfer import MAX_DOCUMENT_BYTES as MAX_TRANSFER_DOCUMENT_BYTES
from .reservations import (
    ReservationCapabilities,
    ReservationIdentity,
    reservation_identifier_choices,
    reservation_identifier_types,
)
from .subnet_catalogue import MAX_SUBNET_ID, MIN_SUBNET_ID, VerifiedSubnet
from .utilities import is_hex_string, parse_delegated_prefixes


class _AddressListField(forms.CharField):
    """A comma-separated address list, cleaned to a list of canonical addresses."""

    def __init__(self, *, invalid: str, **kwargs: Any) -> None:
        """Keep *invalid*, the message for an entry that is not an address, with an ``{entry}`` placeholder."""
        super().__init__(**kwargs)
        self.invalid = invalid

    def clean(self, value: Any) -> list[str]:
        """Return the canonical addresses, and reject an entry that is not an address."""
        try:
            return list(address_list(super().clean(value)))
        except InvalidAddress as exc:
            raise forms.ValidationError(self.invalid.format(entry=exc.entry)) from exc


class _GatewayField(forms.CharField):
    """One gateway address, cleaned to its canonical form. Blank means no gateway."""

    def clean(self, value: Any) -> str:
        """Return the canonical address, or an empty string."""
        text = super().clean(value)
        if not text:
            return ""
        try:
            return str(ipaddress.ip_address(text))
        except ValueError as exc:
            raise forms.ValidationError(f"Invalid gateway IP address: {exc}") from exc


class _PoolLinesField(forms.CharField):
    """Pool lines. The form parses them inside the Subnet, because a Pool needs the Subnet network."""

    def clean(self, value: Any) -> list[str]:
        """Return one entry for each non-empty line."""
        return [line.strip() for line in super().clean(value).splitlines() if line.strip()]


class _DescriptionField(forms.CharField):
    """A description, cleaned as a text input shows it."""

    def clean(self, value: Any) -> str:
        """Remove the line breaks, as a text input does."""
        return description_as_shown(super().clean(value))


_INVALID_DNS = "Invalid DNS server IP address: '{entry}'"
_INVALID_NTP = "Invalid NTP server IP address: '{entry}'"
_SHOWN = "shown_"
_SHOWN_INVALID = "The values that the page showed are not valid. Reload the page and try again."
EditT = TypeVar("EditT")


class _ShownValuesForm(forms.Form, Generic[EditT]):
    """A change form with a hidden ``shown_`` copy of each field in ``shown_names``, with the value that the page showed.

    Each copy is the visible field with a hidden widget, so it cleans the same way. A change acts only while the live
    values are the shown values.
    """

    shown_names: tuple[str, ...]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Add the hidden copies."""
        super().__init__(*args, **kwargs)
        for name in self.shown_names:
            self.fields[_SHOWN + name] = _hidden_copy(self.fields[name])

    @staticmethod
    def edit_from(data: dict[str, Any]) -> EditT:
        """Return the edit of the cleaned values *data*, keyed by field name."""
        raise NotImplementedError

    @staticmethod
    def form_values(edit: EditT) -> dict[str, Any]:
        """Return the value of each field in ``shown_names`` for *edit*."""
        raise NotImplementedError

    @classmethod
    def initial_for(cls, shown: EditT) -> dict[str, Any]:
        """Return the initial value of each field and of its hidden copy, from the values *shown*."""
        values = cls.form_values(shown)
        return {**values, **{_SHOWN + name: values[name] for name in cls.shown_names}}

    @property
    def shown_fields(self) -> list[forms.BoundField]:
        """Return the hidden fields with the values that the page showed."""
        return [self[_SHOWN + name] for name in self.shown_names]

    def clean(self) -> dict[str, Any] | None:
        """Refuse shown values that do not clean, because an error on a hidden field has no place on the page."""
        cleaned = super().clean()
        if any(_SHOWN + name in self.errors for name in self.shown_names):
            self.add_error(None, _SHOWN_INVALID)
        return cleaned

    def to_edit(self) -> EditT:
        """Return the edit of the valid form."""
        return self.edit_from(self.cleaned_data)

    def shown(self) -> EditT:
        """Return the values that the page showed, with the type of the edit."""
        return self.edit_from({name: self.cleaned_data[_SHOWN + name] for name in self.shown_names})


def _hidden_copy(field: forms.Field) -> forms.Field:
    """Return *field* with a hidden widget. It keeps the cleaning, but not the limits for input, such as a minimum."""
    hidden = copy.deepcopy(field)
    hidden.required = False
    hidden.widget = forms.HiddenInput()
    hidden.validators = []
    return hidden


def _validate_subnet_cidr(value: str, *, strict: bool) -> str:
    """Validate a subnet CIDR and return it stripped, never normalised."""
    value = value.strip()
    try:
        ipaddress.ip_network(value, strict=strict)
    except ValueError as exc:
        raise forms.ValidationError(f"Invalid subnet CIDR: {exc}") from exc
    return value


def _validate_ip(value: str, version: Family) -> str:
    """Validate that *value* is a single IP address matching *version* (4 or 6)."""
    try:
        addr = IPAddress(value)
    except (AddrFormatError, ValueError) as exc:
        raise ValidationError(f"Enter a valid IPv{version} address.") from exc
    if addr.version != version:
        raise ValidationError(f"Must be an IPv{version} address.")
    return str(addr)


class ServerForm(NetBoxModelForm):
    """NetBox model form for creating and editing Kea Server objects."""

    fieldsets = (
        FieldSet("name", "tags", name="General"),
        FieldSet(
            "ca_url",
            "ca_username",
            "ca_password",
            "has_control_agent",
            "ssl_verify",
            "client_cert_path",
            "client_key_path",
            "ca_file_path",
            name="Server Connection (Control Agent or DHCP daemon)",
        ),
        FieldSet(
            "dhcp4",
            "dhcp4_url",
            "dhcp4_username",
            "dhcp4_password",
            name="DHCPv4",
        ),
        FieldSet(
            "dhcp6",
            "dhcp6_url",
            "dhcp6_username",
            "dhcp6_password",
            name="DHCPv6",
        ),
        FieldSet(
            "sync_enabled",
            "sync_leases_enabled",
            "sync_reservations_enabled",
            "sync_prefixes_enabled",
            "sync_ip_ranges_enabled",
            "sync_vrf",
            name="IPAM Sync",
        ),
        FieldSet("sync_dhcp_plugin_enabled", name="DHCP Plugin"),
        FieldSet("persist_config", name="Configuration"),
    )

    class Meta:
        model = Server
        fields = (
            "name",
            "ca_url",
            "ca_username",
            "ca_password",
            "dhcp4_username",
            "dhcp4_password",
            "dhcp6_username",
            "dhcp6_password",
            "ssl_verify",
            "client_cert_path",
            "client_key_path",
            "ca_file_path",
            "dhcp6",
            "dhcp4",
            "dhcp4_url",
            "dhcp6_url",
            "has_control_agent",
            "sync_enabled",
            "sync_leases_enabled",
            "sync_reservations_enabled",
            "sync_prefixes_enabled",
            "sync_ip_ranges_enabled",
            "sync_vrf",
            "sync_dhcp_plugin_enabled",
            "persist_config",
            "tags",
        )
        widgets = {
            "ca_password": forms.PasswordInput(),
            "dhcp4_password": forms.PasswordInput(),
            "dhcp6_password": forms.PasswordInput(),
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
    ca_url = forms.CharField(
        label="CA / Server URL",
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


class CSVDefaultedBooleanField(forms.BooleanField):
    """A CSV boolean whose absent or blank column leaves the model default alone.

    An unchecked HTML checkbox sends nothing, so Django's checkbox widget reports
    every absent value as False and ``construct_instance`` writes that False over
    the model default. A plain widget reports an absent column as omitted instead.
    """

    widget = forms.TextInput

    def __init__(self, **kwargs):
        super().__init__(required=False, **kwargs)

    def to_python(self, value):
        """Return None for an absent or blank column; otherwise parse as Django does."""
        if value in self.empty_values:
            return None
        return super().to_python(value)


class ServerImportForm(NetBoxModelImportForm):
    """CSV/YAML bulk-import form for Server objects."""

    #: Booleans an omitted column must leave alone. See CSVDefaultedBooleanField.
    DEFAULTED_BOOLEANS = (
        "ssl_verify",
        "dhcp4",
        "dhcp6",
        "has_control_agent",
        "sync_enabled",
        "sync_leases_enabled",
        "sync_reservations_enabled",
        "sync_prefixes_enabled",
        "sync_ip_ranges_enabled",
        "sync_dhcp_plugin_enabled",
        "persist_config",
    )

    sync_vrf = CSVModelChoiceField(
        label="Sync VRF",
        queryset=VRF.objects.all(),
        to_field_name="name",
        required=False,
        help_text="VRF to assign to synced Prefixes and IP Ranges, by name.",
    )

    def __init__(self, *args, **kwargs):
        """Retype the defaulted booleans so an omitted column stays omitted."""
        super().__init__(*args, **kwargs)
        for name in self.DEFAULTED_BOOLEANS:
            field = self.fields[name]
            self.fields[name] = CSVDefaultedBooleanField(label=field.label, help_text=field.help_text)

    def clean(self):
        """Drop every unset defaulted boolean so ``construct_instance`` skips it."""
        cleaned_data = super().clean()
        for name in self.DEFAULTED_BOOLEANS:
            if cleaned_data.get(name) is None:
                cleaned_data.pop(name, None)
        return cleaned_data

    class Meta:
        model = Server
        fields = (
            "name",
            "ca_url",
            "ca_username",
            "ca_password",
            "dhcp4_username",
            "dhcp4_password",
            "dhcp6_username",
            "dhcp6_password",
            "ssl_verify",
            "client_cert_path",
            "client_key_path",
            "ca_file_path",
            "dhcp4",
            "dhcp6",
            "dhcp4_url",
            "dhcp6_url",
            "has_control_agent",
            "sync_enabled",
            "sync_leases_enabled",
            "sync_reservations_enabled",
            "sync_prefixes_enabled",
            "sync_ip_ranges_enabled",
            "sync_dhcp_plugin_enabled",
            "sync_vrf",
            "persist_config",
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
    # Visual order: Search value, State, Attribute selector.
    field_order = ["q", "state", "by"]

    def __init__(
        self,
        *args,
        subnet_choices: tuple[tuple[str, int], ...] = (),
        subnet_cmds_available: bool = True,
        subnet_diagnostics: tuple[str, ...] = (),
        subnet_catalogue_unavailable: bool = False,
        **kwargs,
    ) -> None:
        """Stash the configured-subnet list so the template can build the Search combobox.

        The choices drive an editable ``<datalist>`` on the Search field (``q``)
        that the template wires up only when the selected attribute is *Subnet*
        or *Subnet ID* — there is no separate subnet selector field.
        ``subnet_cmds_available`` is False when the hook that supplies those choices is
        not loaded, which the template reports instead of showing an empty combobox.
        ``subnet_diagnostics`` are the Subnet Catalogue messages the template shows inline,
        as an error when ``subnet_catalogue_unavailable`` and as a warning otherwise.
        """
        super().__init__(*args, **kwargs)
        self.subnet_choices = subnet_choices
        self.subnet_cmds_available = subnet_cmds_available
        self.subnet_diagnostics = subnet_diagnostics
        self.subnet_catalogue_unavailable = subnet_catalogue_unavailable

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
            if by == "":
                try:
                    page_ip = IPAddress(page, version=ip_version)
                    cleaned_data["page"] = str(page_ip)
                except (AddrFormatError, TypeError, ValueError) as e:
                    raise ValidationError({"page": "Invalid IP."}) from e
            else:
                try:
                    page_number = int(page)
                except (TypeError, ValueError) as e:
                    raise ValidationError({"page": "Page must be a positive integer."}) from e
                if page_number < 1:
                    raise ValidationError({"page": "Page must be a positive integer."})
                cleaned_data["page"] = page_number

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


class MultipleIPField(forms.MultipleChoiceField):
    """Form field accepting a list of IP addresses validated against a specific IP version."""

    def __init__(self, version: Family, *args, **kwargs) -> None:
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


def _identifier_type_choices(version: Family) -> list[tuple[str, str]]:
    """Offer exactly the identifier types the Reservation domain accepts."""
    return list(reservation_identifier_choices(version))


_IDENTIFIER_TYPE_CHOICES_V4 = _identifier_type_choices(4)
_IDENTIFIER_TYPE_CHOICES_V6 = _identifier_type_choices(6)


class ReservationIdentifierSelect(forms.Select):
    """Disable identifier choices that the live Kea configuration cannot use."""

    def __init__(self, *args, unavailable: dict[str, str] | None = None, **kwargs):
        self.unavailable = unavailable or {}
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        """Add disabled state and a concise reason to unavailable options."""
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value in self.unavailable:
            option["attrs"]["disabled"] = True
            option["attrs"]["title"] = self.unavailable[value]
        return option


def _configure_identifier_capabilities(
    form: "Reservation4Form | Reservation6Form",
    capabilities: ReservationCapabilities | None,
    version: Family,
) -> None:
    """Disable identifier choices the live Kea configuration cannot use."""
    unavailable: dict[str, str] = (
        {str(identifier): reason for identifier, reason in capabilities.unavailable_identifiers}
        if capabilities is not None
        else dict.fromkeys(reservation_identifier_types(version), "Kea capability discovery failed.")
    )
    identifier_field = cast(forms.ChoiceField, form.fields["identifier_type"])
    identifier_field.widget = ReservationIdentifierSelect(
        choices=identifier_field.choices,
        unavailable=unavailable,
    )
    form.reservation_capabilities = capabilities


class Reservation4Form(forms.Form):
    """Form for creating or editing a DHCPv4 host reservation."""

    reservation_capabilities: ReservationCapabilities | None = None

    subnet_cidr = forms.CharField(
        label="Subnet CIDR",
        max_length=50,
        help_text="Subnet the reservation belongs to (e.g. <code>10.0.0.0/24</code>).",
        widget=forms.TextInput(attrs={"list": constants.RESERVATION_SUBNET_DATALIST_ID}),
    )
    ip_address = forms.CharField(
        label="IP Address",
        required=False,
        help_text=(
            "Fixed IPv4 address to assign to this reservation. Leave blank to reserve"
            " only a hostname, options or client classes for this client."
        ),
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
    managed_fingerprint = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        """Apply live DHCPv4 Reservation identifier capabilities."""
        capabilities = kwargs.pop("capabilities", None)
        super().__init__(*args, **kwargs)
        _configure_identifier_capabilities(self, capabilities, 4)

    def clean_subnet_cidr(self) -> str:
        """Validate the value is a valid IPv4 subnet CIDR.

        Skipped when the field is disabled: the edit views seed it from a
        server-side CIDR lookup that falls back to the raw Kea subnet ID
        (e.g. "1") when that lookup fails, which isn't a CIDR and would
        otherwise fail this check even though the value is never user input.
        """
        value = self.cleaned_data.get("subnet_cidr", "").strip()
        if self.fields["subnet_cidr"].disabled:
            return value
        if "/" not in value:
            raise forms.ValidationError("Enter a valid IPv4 subnet CIDR (e.g. 10.0.0.0/24).")
        try:
            network = ipaddress.IPv4Network(value, strict=True)
        except ValueError as exc:
            raise forms.ValidationError("Enter a valid IPv4 subnet CIDR (e.g. 10.0.0.0/24).") from exc
        # Use the canonical CIDR form accepted by configured_subnet_id_from_cidr().
        return str(network)

    def clean_ip_address(self) -> str:
        """Validate the value is a valid IPv4 address, or blank for no fixed address."""
        value = (self.cleaned_data.get("ip_address") or "").strip()
        if not value:
            return ""
        return _validate_ip(value, version=4)

    def clean(self) -> dict[str, Any] | None:
        """Cross-validate identifier value against identifier_type."""
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        capabilities = self.reservation_capabilities
        if capabilities is None or not capabilities.mutation_available:
            raise forms.ValidationError("Reservation mutation capabilities are unavailable.")
        identifier_type = cleaned.get("identifier_type")
        if not identifier_type:
            return cleaned
        if identifier_type not in capabilities.identifiers:
            self.add_error("identifier_type", "This identifier is not enabled in the live Kea configuration.")
        identifier = cleaned.get("identifier", "").strip()
        if not identifier:
            return cleaned
        try:
            cleaned["identifier"] = ReservationIdentity(identifier_type, identifier).value
        except ValueError as exc:
            self.add_error("identifier", str(exc))
        return cleaned


class Reservation6Form(forms.Form):
    """Form for creating or editing a DHCPv6 host reservation."""

    reservation_capabilities: ReservationCapabilities | None = None

    subnet_cidr = forms.CharField(
        label="Subnet CIDR",
        max_length=50,
        help_text="Subnet the reservation belongs to (e.g. <code>2001:db8::/48</code>).",
        widget=forms.TextInput(attrs={"list": constants.RESERVATION_SUBNET_DATALIST_ID}),
    )
    ip_addresses = forms.CharField(
        label="IPv6 Addresses",
        required=False,
        help_text="Comma-separated list of IPv6 addresses to assign. Leave blank to reserve none.",
    )
    prefixes = forms.CharField(
        label="Delegated Prefixes",
        required=False,
        help_text=(
            "Comma-separated IPv6 prefixes to delegate (e.g. <code>2001:db8:1::/64</code>)."
            " Leave blank to remove any that are set."
        ),
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
    managed_fingerprint = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        """Apply live DHCPv6 Reservation identifier capabilities."""
        capabilities = kwargs.pop("capabilities", None)
        super().__init__(*args, **kwargs)
        _configure_identifier_capabilities(self, capabilities, 6)

    def clean_subnet_cidr(self) -> str:
        """Validate the value is a valid IPv6 subnet CIDR.

        Skipped when the field is disabled: the edit views seed it from a
        server-side CIDR lookup that falls back to the raw Kea subnet ID
        (e.g. "1") when that lookup fails, which isn't a CIDR and would
        otherwise fail this check even though the value is never user input.
        """
        value = self.cleaned_data.get("subnet_cidr", "").strip()
        if self.fields["subnet_cidr"].disabled:
            return value
        if "/" not in value:
            raise forms.ValidationError("Enter a valid IPv6 subnet CIDR (e.g. 2001:db8::/48).")
        try:
            network = ipaddress.IPv6Network(value, strict=True)
        except ValueError as exc:
            raise forms.ValidationError("Enter a valid IPv6 subnet CIDR (e.g. 2001:db8::/48).") from exc
        # Use the canonical CIDR form accepted by configured_subnet_id_from_cidr().
        return str(network)

    def clean_ip_addresses(self) -> str:
        """Validate every entry is a valid IPv6 address; blank reserves no address."""
        val = self.cleaned_data.get("ip_addresses") or ""
        cleaned: list[str] = []
        for entry in val.split(","):
            raw = entry.strip()
            if not raw:
                continue
            try:
                addr = IPAddress(raw)
            except (AddrFormatError, ValueError) as exc:
                raise ValidationError(f"'{raw}' is not a valid IP address.") from exc
            if addr.version != 6:
                raise ValidationError(f"'{raw}' is not a valid IPv6 address.")
            cleaned.append(str(addr))
        return ",".join(cleaned)

    def clean_prefixes(self) -> str:
        """Validate the delegated-prefix list with the shared domain validator."""
        try:
            prefixes = parse_delegated_prefixes(self.cleaned_data.get("prefixes") or "")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return ",".join(prefixes)

    def clean(self) -> dict[str, Any] | None:
        """Cross-validate identifier value against identifier_type."""
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        capabilities = self.reservation_capabilities
        if capabilities is None or not capabilities.mutation_available:
            raise forms.ValidationError("Reservation mutation capabilities are unavailable.")
        identifier_type = cleaned.get("identifier_type")
        if not identifier_type:
            return cleaned
        if identifier_type not in capabilities.identifiers:
            self.add_error("identifier_type", "This identifier is not enabled in the live Kea configuration.")
        identifier = cleaned.get("identifier", "").strip()
        if not identifier:
            return cleaned
        try:
            cleaned["identifier"] = ReservationIdentity(identifier_type, identifier).value
        except ValueError as exc:
            self.add_error("identifier", str(exc))
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
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        qs = Server.objects.filter(dhcp4=True)
        if user is not None:
            qs = qs.restrict(user, "view")
        self.fields["server"].queryset = qs


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
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        qs = Server.objects.filter(dhcp6=True)
        if user is not None:
            qs = qs.restrict(user, "view")
        self.fields["server"].queryset = qs


# ─────────────────────────────────────────────────────────────────────────────
# Phase 10: Pool management forms
# ─────────────────────────────────────────────────────────────────────────────


def _seen_network(value: str, family: Family) -> IPNetworkValue:
    """Parse the CIDR that a Subnet change page showed. Accept host bits: the page shows the CIDR that Kea declares."""
    try:
        return subnet_network(value, family)
    except ValueError as exc:
        raise forms.ValidationError(f"Invalid subnet CIDR: {exc}") from exc


class SubnetConfirmForm(forms.Form):
    """The CIDR that a Subnet change page showed, so that the change reaches only that Subnet."""

    subnet_cidr = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args: Any, family: Family, **kwargs: Any) -> None:
        """Keep the family that the CIDR must match."""
        super().__init__(*args, **kwargs)
        self.family = family

    def clean_subnet_cidr(self) -> str:
        """Require a CIDR of the family."""
        value: str = self.cleaned_data["subnet_cidr"]
        _seen_network(value, self.family)
        return value


class PoolAddForm(forms.Form):
    """Form for adding a DHCP pool to an existing Verified Subnet."""

    # Not required: a missing value is a non-field error in clean(), which the page shows.
    subnet_cidr = forms.CharField(required=False, widget=forms.HiddenInput)
    pool = forms.CharField(
        label="Pool",
        help_text=("Pool range (e.g. <code>10.0.0.50-10.0.0.99</code>) or CIDR (e.g. <code>10.0.0.0/28</code>)."),
        max_length=255,
    )

    def __init__(self, *args: Any, subnet: VerifiedSubnet | None, absence_confirmed: bool, **kwargs: Any) -> None:
        """Keep the Verified Subnet that the Pool must fit, and whether a missing Subnet is absent from Kea."""
        super().__init__(*args, **kwargs)
        self.subnet = subnet
        self.absence_confirmed = absence_confirmed

    def clean(self) -> dict[str, Any]:
        """Require the Subnet that the page showed, then parse the Pool inside it."""
        cleaned = self.cleaned_data
        subnet = self._subnet_as_seen(cleaned.get("subnet_cidr", ""))
        if "pool" in cleaned:
            try:
                cleaned["pool"] = self._pool_in(subnet, cleaned["pool"])
            except forms.ValidationError as exc:
                self.add_error("pool", exc)
        return cleaned

    def _subnet_as_seen(self, cidr: str) -> VerifiedSubnet:
        if self.subnet is None:
            if self.absence_confirmed:
                raise forms.ValidationError(
                    "This Subnet is not in the current Subnet Catalogue. Reload the Subnets page and try again."
                )
            raise forms.ValidationError(SUBNET_LIST_UNCONFIRMED)
        if _seen_network(cidr, self.subnet.network.version) != self.subnet.network:
            raise forms.ValidationError(subnet_changed(self.subnet.subnet_id, cidr))
        return self.subnet

    @staticmethod
    def _pool_in(subnet: VerifiedSubnet, text: str) -> Pool:
        """Parse the Pool inside *subnet* and reject an overlap with an existing Pool."""
        try:
            pool = parse_pool(text, subnet.network)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        # Without configuration facts the existing Pools are unknown, so Kea decides.
        existing = subnet.configuration.pools if subnet.configuration is not None else ()
        overlapping = next((other for other in existing if other.overlaps(pool)), None)
        if overlapping is not None:
            raise forms.ValidationError(f"Pool {pool.range} overlaps existing Pool {overlapping.range}.")
        return pool


class _SubnetBaseForm(forms.Form):
    """Shared fields and validators for subnet add and edit forms.

    Subclasses add identity fields (subnet CIDR / ID for add; hidden CIDR for edit).
    """

    subnet_field: str
    shared_network_help: str

    # A free name: config_write checks that the Shared Network exists. The view sets the offered names.
    shared_network = forms.CharField(label="Shared Network", widget=forms.Select, required=False, strip=False)
    # False: the page could not load the Shared Networks, so its disabled select posts no name.
    shared_networks_complete = forms.BooleanField(widget=forms.HiddenInput, required=False)
    pools = _PoolLinesField(
        label="Pools",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One pool per line, e.g. <code>10.0.0.100-10.0.0.200</code>",
    )
    gateway = _GatewayField(
        label="Default gateway",
        required=False,
        max_length=50,
        help_text="IP address of the default gateway (option <code>routers</code>).",
    )
    dns_servers = _AddressListField(
        label="DNS servers",
        required=False,
        max_length=255,
        help_text="Comma-separated IP addresses.",
        invalid=_INVALID_DNS,
    )
    ntp_servers = _AddressListField(
        label="NTP servers",
        required=False,
        max_length=255,
        help_text="Comma-separated IP addresses.",
        invalid=_INVALID_NTP,
    )
    ddns_qualifying_suffix = forms.CharField(
        label="DDNS qualifying suffix",
        required=False,
        max_length=255,
        help_text="Domain suffix appended to hostnames before sending DDNS updates (e.g. example.com.).",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["shared_network"].help_text = self.shared_network_help

    def clean_shared_network(self) -> str:
        """Refuse a name of only white space. Keep a real name as Kea holds it."""
        name: str = self.cleaned_data["shared_network"]
        if name and not name.strip():
            raise forms.ValidationError("Enter a Shared Network name, or choose none.")
        return name

    def clean(self) -> dict[str, Any] | None:
        """Refuse a page without the Shared Network list, and match gateway, DNS and NTP servers to the family."""
        cleaned = super().clean()
        if not self.cleaned_data.get("shared_networks_complete"):
            self.add_error(
                None,
                "NetBox could not load the Shared Networks from Kea when it showed the page. "
                "Reload the page and try again.",
            )
        if not cleaned:
            return cleaned
        subnet_str = cleaned.get(self.subnet_field, "")
        try:
            subnet_net = ipaddress.ip_network(subnet_str, strict=False)
        except ValueError:
            return cleaned

        # The edit form also has the hidden copy of the Pools that the page showed.
        for field in ("pools", "shown_pools"):
            if field in cleaned:
                self._clean_pools_in(subnet_net, cleaned, field)
        subnet_version = subnet_net.version

        gateway = cleaned.get("gateway", "")
        if gateway:
            if subnet_version == 6:
                self.add_error("gateway", "Gateway is not allowed for IPv6 subnets.")
            else:
                try:
                    gw_version = ipaddress.ip_address(gateway).version
                except ValueError:
                    gw_version = None
                if gw_version and gw_version != subnet_version:
                    self.add_error(
                        "gateway",
                        f"Gateway must be an IPv{subnet_version} address to match the subnet family.",
                    )

        # Both options are address arrays of the subnet's own family, so Kea rejects a
        # mismatch at apply time.
        for field, label in (("dns_servers", "DNS server"), ("ntp_servers", "NTP server")):
            addresses = cleaned.get(field) or []
            if not isinstance(addresses, list):
                continue
            for address in addresses:
                try:
                    version = ipaddress.ip_address(address).version
                except ValueError:
                    continue
                if version != subnet_version:
                    self.add_error(
                        field,
                        f"{label} '{address}' must be an IPv{subnet_version} address to match the subnet family.",
                    )
                    break

        return cleaned

    def to_fields(self) -> SubnetFields:
        """Return the Subnet fields of the valid form."""
        return _subnet_fields(self.cleaned_data)

    def _clean_pools_in(self, subnet: IPNetworkValue, cleaned: dict[str, Any], field: str) -> None:
        """Parse each Pool line of *field* inside *subnet*; the Pools must not overlap each other."""
        pools: list[Pool] = []
        errors: list[str] = []
        for line in cleaned[field]:
            try:
                pool = parse_pool(line, subnet)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            overlapping = next((other for other in pools if other.overlaps(pool)), None)
            if overlapping is not None:
                errors.append(f"Pool {pool.range} overlaps Pool {overlapping.range}.")
                continue
            pools.append(pool)
        if errors:
            self.add_error(field, errors)
            return
        cleaned[field] = pools


def _subnet_fields(data: dict[str, Any]) -> SubnetFields:
    return SubnetFields(
        pools=tuple(pool.range for pool in data["pools"]),
        gateway=data["gateway"],
        dns_servers=tuple(data["dns_servers"]),
        ntp_servers=tuple(data["ntp_servers"]),
        ddns_qualifying_suffix=data["ddns_qualifying_suffix"],
    )


class SubnetAddForm(_SubnetBaseForm):
    """Form for adding a new DHCP subnet to Kea."""

    subnet_field = "subnet"
    shared_network_help = "Assign this subnet to a shared network immediately after creation."

    subnet = forms.CharField(
        label="Subnet CIDR",
        max_length=50,
        help_text="e.g. <code>10.0.0.0/24</code> or <code>2001:db8::/48</code>",
    )
    subnet_id = forms.IntegerField(
        label="Subnet ID",
        required=False,
        min_value=MIN_SUBNET_ID,
        max_value=MAX_SUBNET_ID,
        help_text="Leave blank to use the highest existing subnet ID plus one.",
    )
    field_order = [
        "subnet",
        "subnet_id",
        "shared_network",
        "pools",
        "gateway",
        "dns_servers",
        "ntp_servers",
        "ddns_qualifying_suffix",
    ]

    def clean_subnet(self) -> str:
        """Reject host bits here: this CIDR is typed by the user for a subnet being created."""
        return _validate_subnet_cidr(self.cleaned_data["subnet"], strict=True)


class SubnetEditForm(_ShownValuesForm[SubnetEdit], _SubnetBaseForm):
    """Form for editing an existing DHCP subnet in Kea.

    The subnet CIDR and ID are immutable. The CIDR, the Shared Network and the values that the page showed are
    hidden fields, so the change acts only on the Subnet that the page showed. All other fields are optional. A blank
    Pool, option or DDNS field removes that value. A blank lifetime or timer keeps the live value.
    """

    subnet_field = "subnet_cidr"
    shared_network_help = "Assign this subnet to a shared network, or leave blank to use the global address pool."
    shown_names = (
        "pools",
        "gateway",
        "dns_servers",
        "ntp_servers",
        "ddns_qualifying_suffix",
        "valid_lft",
        "min_valid_lft",
        "max_valid_lft",
        "renew_timer",
        "rebind_timer",
    )

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
    renew_timer = forms.IntegerField(
        label="Renew timer / T1 (s)",
        required=False,
        min_value=1,
        help_text="Time (seconds) after which client should renew. Kea parameter: renew-timer.",
    )
    rebind_timer = forms.IntegerField(
        label="Rebind timer / T2 (s)",
        required=False,
        min_value=1,
        help_text="Time (seconds) after which client should rebind. Kea parameter: rebind-timer.",
    )
    # The Shared Network of the Subnet when the page loaded, as Kea names it; blank means none. Not a shown_ copy of
    # shared_network: edit_subnet checks the membership on its own, before the field values.
    original_network = forms.CharField(widget=forms.HiddenInput, required=False, strip=False)
    # False: the page could not confirm the Shared Network, so the form cannot be saved.
    original_network_confirmed = forms.BooleanField(widget=forms.HiddenInput, required=False)

    field_order = [
        "subnet_cidr",
        "original_network",
        "original_network_confirmed",
        "shared_networks_complete",
        "shared_network",
        "pools",
        "gateway",
        "dns_servers",
        "ntp_servers",
        "valid_lft",
        "min_valid_lft",
        "max_valid_lft",
        "renew_timer",
        "rebind_timer",
        "ddns_qualifying_suffix",
    ]

    @staticmethod
    def edit_from(data: dict[str, Any]) -> SubnetEdit:
        """Return the Subnet edit of the cleaned values *data*."""
        return SubnetEdit(
            fields=_subnet_fields(data),
            valid_lifetime=data["valid_lft"],
            min_valid_lifetime=data["min_valid_lft"],
            max_valid_lifetime=data["max_valid_lft"],
            renew_timer=data["renew_timer"],
            rebind_timer=data["rebind_timer"],
        )

    @staticmethod
    def form_values(edit: SubnetEdit) -> dict[str, Any]:
        """Return the value of each managed field for *edit*."""
        fields = edit.fields
        return {
            "pools": "\n".join(fields.pools),
            "gateway": fields.gateway,
            "dns_servers": ", ".join(fields.dns_servers),
            "ntp_servers": ", ".join(fields.ntp_servers),
            "ddns_qualifying_suffix": fields.ddns_qualifying_suffix,
            "valid_lft": edit.valid_lifetime,
            "min_valid_lft": edit.min_valid_lifetime,
            "max_valid_lft": edit.max_valid_lifetime,
            "renew_timer": edit.renew_timer,
            "rebind_timer": edit.rebind_timer,
        }

    def clean_subnet_cidr(self) -> str:
        """Accept host bits here: Kea allows them, so this CIDR is echoed back from its config."""
        return _validate_subnet_cidr(self.cleaned_data["subnet_cidr"], strict=False)

    def clean(self) -> dict[str, Any] | None:
        """Refuse a page that could not confirm the Shared Network, because a save would guess the membership."""
        cleaned = super().clean()
        if not self.cleaned_data.get("original_network_confirmed"):
            raise forms.ValidationError(
                "NetBox could not confirm the Shared Network of this Subnet when it showed the page. "
                "Reload the page and try again."
            )
        return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Reservation search / filter
# ─────────────────────────────────────────────────────────────────────────────


class ReservationSearchForm(forms.Form):
    """Search form for filtering reservations on the per-server reservation tabs.

    All fields are optional — submitting an empty form shows all reservations.
    Filtered searches scan bounded batches and retain a cursor for continuation.
    """

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(attrs={"placeholder": "IP, hostname, or identifier"}),
        help_text=(
            "Case-insensitive search across addresses, hostname, Reservation Identity, and DHCP Option name or data."
        ),
    )
    subnet_id = forms.IntegerField(
        required=False,
        label="Subnet ID",
        min_value=1,
        help_text="Filter to a specific Kea subnet ID.",
    )
    scope = forms.ChoiceField(
        required=False,
        label="Scope",
        choices=(("", "All scopes"), ("global", "Global"), ("in-subnet", "In-Subnet")),
        help_text="Search a bounded batch by Reservation Scope. Continue with Next page if more records remain.",
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
    """Accept one explicit YAML or JSON Reservation document."""

    # Django's DATA_UPLOAD_MAX_MEMORY_SIZE does not cover file uploads, so reject an
    # oversized upload here instead of reading all of it into memory for the parser.
    MAX_DOCUMENT_BYTES = MAX_TRANSFER_DOCUMENT_BYTES

    format = forms.ChoiceField(
        choices=(("yaml", "YAML"), ("json", "JSON")),
        initial="yaml",
        help_text="Select the document syntax. Format detection is not automatic.",
    )
    document = forms.CharField(
        required=False,
        label="Document",
        widget=forms.Textarea(attrs={"rows": 16, "spellcheck": "false"}),
        help_text="Paste one complete Reservation transfer document.",
    )
    document_file = forms.FileField(
        required=False,
        label="Document file",
        help_text="Upload one UTF-8 YAML or JSON document.",
    )

    def clean(self):
        """Require exactly one document source and decode uploads as UTF-8."""
        cleaned_data = super().clean()
        document = cleaned_data.get("document")
        document_file = cleaned_data.get("document_file")
        if document and document_file:
            raise forms.ValidationError("Paste a document or upload a document file, but do not use both.")
        if not document and not document_file:
            raise forms.ValidationError("Paste a document or upload a document file.")
        if document_file is not None:
            if document_file.size > self.MAX_DOCUMENT_BYTES:
                raise forms.ValidationError(
                    f"The document file must not exceed {self.MAX_DOCUMENT_BYTES // (1024 * 1024)} MB."
                )
            try:
                document = document_file.read().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise forms.ValidationError("The document file must use UTF-8 encoding.") from exc
        cleaned_data["document"] = document
        return cleaned_data


class Reservation4ImportForm(_BaseBulkReservationImportForm):
    """Import normalized DHCPv4 Reservation documents."""


class Reservation6ImportForm(_BaseBulkReservationImportForm):
    """Import normalized DHCPv6 Reservation documents."""


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
        help_text="Kea option name (e.g. routers, domain-name-servers). Pick a standard option or type any name.",
        # `list` links the input to the <datalist id="kea-option-names"> rendered
        # by the {% kea_option_datalist %} tag — an editable combobox: standard
        # options are suggested while free-form names remain allowed.
        widget=forms.TextInput(attrs={"list": "kea-option-names", "autocomplete": "off"}),
    )
    data = forms.CharField(
        max_length=512,
        help_text="Option value (e.g. 10.0.0.1 or 8.8.8.8, 8.8.4.4).",
    )
    always_send = forms.BooleanField(
        required=False,
        help_text="Send option even when not requested by the client.",
    )


class ConfigurationOptionsForm(SubnetOptionsForm):
    """Edit a configuration option while retaining its original identity and the value that the operator saw."""

    original_option = forms.JSONField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        """Allow code-only rows and suppression entries to omit displayed values."""
        super().__init__(*args, **kwargs)
        self.fields["name"].required = False
        self.fields["data"].required = False

    def clean_original_option(self):
        """Validate the original identity with the shared DHCP Option parser."""
        identity = self.cleaned_data.get("original_option")
        if identity is not None:
            try:
                parse_dhcp_option(identity)
            except ValueError as exc:
                raise forms.ValidationError("Invalid original DHCP Option identity.") from exc
        return identity

    def clean(self):
        """Require an option name for a new row or an existing name-only row."""
        data = super().clean()
        identity = data.get("original_option")
        if not data.get("name") and (not isinstance(identity, dict) or identity.get("code") is None):
            self.add_error("name", "An option name is required unless the existing option has a code.")
        if identity is None and not data.get("data"):
            self.add_error("data", "This field is required.")
        return data


SubnetOptionsFormSet = forms.formset_factory(ConfigurationOptionsForm, extra=1, can_delete=True)


class ReservationOptionsForm(SubnetOptionsForm):
    """One Reservation option row with its stable source position."""

    original_index = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)


ReservationOptionsFormSet = forms.formset_factory(ReservationOptionsForm, extra=1, can_delete=True)


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
        max_length=3 * constants.DUID_MAX_OCTETS - 1,
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
        return _validate_ip(self.cleaned_data["ip_address"], version=4)

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
        max_length=3 * constants.DUID_MAX_OCTETS - 1,
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
        return _validate_ip(self.cleaned_data["ip_address"], version=6)

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


class SharedNetworkEditForm(_ShownValuesForm[SharedNetworkEdit]):
    """Form for editing an existing Kea shared network (description, interface, relay, options).

    Each managed field has a hidden copy with the value that the page showed, so the change acts only while the live
    values are the values that the operator saw.
    """

    shown_names = ("description", "interface", "relay_addresses", "dns_servers", "ntp_servers")

    name = forms.CharField(widget=forms.HiddenInput())
    description = _DescriptionField(required=False, label="Description")
    interface = forms.CharField(
        max_length=128,
        required=False,
        label="Interface",
        help_text="Bind this network to a specific server NIC (optional, e.g. eth0).",
    )
    relay_addresses = _AddressListField(
        required=False,
        label="Relay agent addresses",
        help_text="Comma-separated relay agent IP addresses (leave blank to clear).",
        invalid="'{entry}' is not a valid IP address.",
    )
    dns_servers = _AddressListField(
        required=False,
        label="DNS servers",
        help_text="Comma-separated DNS server IP addresses (option 6 / domain-name-servers).",
        invalid=_INVALID_DNS,
    )
    ntp_servers = _AddressListField(
        required=False,
        label="NTP servers",
        help_text="Comma-separated NTP server addresses (option 42 / ntp-servers).",
        invalid=_INVALID_NTP,
    )

    @staticmethod
    def edit_from(data: dict[str, Any]) -> SharedNetworkEdit:
        """Return the Shared Network edit of the cleaned values *data*."""
        return SharedNetworkEdit(
            description=data["description"],
            interface=data["interface"],
            relay_addresses=tuple(data["relay_addresses"]),
            dns_servers=tuple(data["dns_servers"]),
            ntp_servers=tuple(data["ntp_servers"]),
        )

    @staticmethod
    def form_values(edit: SharedNetworkEdit) -> dict[str, Any]:
        """Return the value of each managed field for *edit*."""
        return {
            "description": edit.description,
            "interface": edit.interface,
            "relay_addresses": ", ".join(edit.relay_addresses),
            "dns_servers": ", ".join(edit.dns_servers),
            "ntp_servers": ", ".join(edit.ntp_servers),
        }


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


class SyncConfigForm(forms.Form):
    """Form for editing the global SyncConfig (interval + kill-switch + per-type toggles)."""

    interval_minutes = forms.IntegerField(
        label="Sync Interval (minutes)",
        min_value=1,
        max_value=1440,
        initial=5,
        help_text="How often the background sync job runs. Minimum 1 minute.",
    )
    sync_enabled = forms.BooleanField(
        label="Sync Enabled (global)",
        required=False,
        help_text="Global kill-switch. Uncheck to pause sync for all servers.",
    )
    sync_leases_enabled = forms.BooleanField(
        label="Sync Leases",
        required=False,
        help_text="Sync active Kea leases to NetBox IPAM as IP addresses.",
    )
    sync_reservations_enabled = forms.BooleanField(
        label="Sync Reservations",
        required=False,
        help_text="Sync Kea reservations to NetBox IPAM as reserved IP addresses.",
    )
    sync_prefixes_enabled = forms.BooleanField(
        label="Sync Prefixes",
        required=False,
        help_text="Sync Kea subnets to NetBox IPAM as IP Prefixes.",
    )
    sync_ip_ranges_enabled = forms.BooleanField(
        label="Sync IP Ranges",
        required=False,
        help_text="Sync Kea pools to NetBox IPAM as IP Ranges.",
    )

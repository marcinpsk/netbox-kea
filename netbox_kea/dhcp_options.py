# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Kea standard option definitions (name, code) for the dhcp4 and dhcp6 spaces, which also back the option-name
# suggestion list. Source: isc-projects/kea src/lib/dhcp/std_option_defs.h at tag Kea-3.2.0.
KEA_DHCP4_STD_OPTIONS: list[tuple[str, int]] = [
    ("subnet-mask", 1),
    ("time-offset", 2),
    ("routers", 3),
    ("time-servers", 4),
    ("name-servers", 5),
    ("domain-name-servers", 6),
    ("log-servers", 7),
    ("cookie-servers", 8),
    ("lpr-servers", 9),
    ("impress-servers", 10),
    ("resource-location-servers", 11),
    ("host-name", 12),
    ("boot-size", 13),
    ("merit-dump", 14),
    ("domain-name", 15),
    ("swap-server", 16),
    ("root-path", 17),
    ("extensions-path", 18),
    ("ip-forwarding", 19),
    ("non-local-source-routing", 20),
    ("policy-filter", 21),
    ("max-dgram-reassembly", 22),
    ("default-ip-ttl", 23),
    ("path-mtu-aging-timeout", 24),
    ("path-mtu-plateau-table", 25),
    ("interface-mtu", 26),
    ("all-subnets-local", 27),
    ("broadcast-address", 28),
    ("perform-mask-discovery", 29),
    ("mask-supplier", 30),
    ("router-discovery", 31),
    ("router-solicitation-address", 32),
    ("static-routes", 33),
    ("trailer-encapsulation", 34),
    ("arp-cache-timeout", 35),
    ("ieee802-3-encapsulation", 36),
    ("default-tcp-ttl", 37),
    ("tcp-keepalive-interval", 38),
    ("tcp-keepalive-garbage", 39),
    ("nis-domain", 40),
    ("nis-servers", 41),
    ("ntp-servers", 42),
    ("vendor-encapsulated-options", 43),
    ("netbios-name-servers", 44),
    ("netbios-dd-server", 45),
    ("netbios-node-type", 46),
    ("netbios-scope", 47),
    ("font-servers", 48),
    ("x-display-manager", 49),
    ("dhcp-requested-address", 50),
    ("dhcp-lease-time", 51),
    ("dhcp-option-overload", 52),
    ("dhcp-message-type", 53),
    ("dhcp-server-identifier", 54),
    ("dhcp-parameter-request-list", 55),
    ("dhcp-message", 56),
    ("dhcp-max-message-size", 57),
    ("dhcp-renewal-time", 58),
    ("dhcp-rebinding-time", 59),
    ("vendor-class-identifier", 60),
    ("dhcp-client-identifier", 61),
    ("nwip-domain-name", 62),
    ("nwip-suboptions", 63),
    ("nisplus-domain-name", 64),
    ("nisplus-servers", 65),
    ("tftp-server-name", 66),
    ("boot-file-name", 67),
    ("mobile-ip-home-agent", 68),
    ("smtp-server", 69),
    ("pop-server", 70),
    ("nntp-server", 71),
    ("www-server", 72),
    ("finger-server", 73),
    ("irc-server", 74),
    ("streettalk-server", 75),
    ("streettalk-directory-assistance-server", 76),
    ("user-class", 77),
    ("slp-directory-agent", 78),
    ("slp-service-scope", 79),
    ("fqdn", 81),
    ("dhcp-agent-options", 82),
    ("nds-servers", 85),
    ("nds-tree-name", 86),
    ("nds-context", 87),
    ("bcms-controller-names", 88),
    ("bcms-controller-address", 89),
    ("authenticate", 90),
    ("client-last-transaction-time", 91),
    ("associated-ip", 92),
    ("client-system", 93),
    ("client-ndi", 94),
    ("uuid-guid", 97),
    ("uap-servers", 98),
    ("geoconf-civic", 99),
    ("pcode", 100),
    ("tcode", 101),
    ("v6-only-preferred", 108),
    ("netinfo-server-address", 112),
    ("netinfo-server-tag", 113),
    ("v4-captive-portal", 114),
    ("auto-config", 116),
    ("name-service-search", 117),
    ("subnet-selection", 118),
    ("domain-search", 119),
    ("classless-static-route", 121),
    ("cablelabs-client-conf", 122),
    ("vivco-suboptions", 124),
    ("vivso-suboptions", 125),
    ("pana-agent", 136),
    ("v4-lost", 137),
    ("capwap-ac-v4", 138),
    ("sip-ua-cs-domains", 141),
    ("v4-sztp-redirect", 143),
    ("rdnss-selection", 146),
    ("status-code", 151),
    ("base-time", 152),
    ("start-time-of-state", 153),
    ("query-start-time", 154),
    ("query-end-time", 155),
    ("dhcp-state", 156),
    ("data-source", 157),
    ("v4-portparams", 159),
    ("v4-dnr", 162),
    ("option-6rd", 212),
    ("v4-access-domain", 213),
]

KEA_DHCP6_STD_OPTIONS: list[tuple[str, int]] = [
    ("clientid", 1),
    ("serverid", 2),
    ("ia-na", 3),
    ("ia-ta", 4),
    ("iaaddr", 5),
    ("oro", 6),
    ("preference", 7),
    ("elapsed-time", 8),
    ("relay-msg", 9),
    ("auth", 11),
    ("unicast", 12),
    ("status-code", 13),
    ("rapid-commit", 14),
    ("user-class", 15),
    ("vendor-class", 16),
    ("vendor-opts", 17),
    ("interface-id", 18),
    ("reconf-msg", 19),
    ("reconf-accept", 20),
    ("sip-server-dns", 21),
    ("sip-server-addr", 22),
    ("dns-servers", 23),
    ("domain-search", 24),
    ("ia-pd", 25),
    ("iaprefix", 26),
    ("nis-servers", 27),
    ("nisp-servers", 28),
    ("nis-domain-name", 29),
    ("nisp-domain-name", 30),
    ("sntp-servers", 31),
    ("information-refresh-time", 32),
    ("bcmcs-server-dns", 33),
    ("bcmcs-server-addr", 34),
    ("geoconf-civic", 36),
    ("remote-id", 37),
    ("subscriber-id", 38),
    ("client-fqdn", 39),
    ("pana-agent", 40),
    ("new-posix-timezone", 41),
    ("new-tzdb-timezone", 42),
    ("ero", 43),
    ("lq-query", 44),
    ("client-data", 45),
    ("clt-time", 46),
    ("lq-relay-data", 47),
    ("lq-client-link", 48),
    ("v6-lost", 51),
    ("capwap-ac-v6", 52),
    ("relay-id", 53),
    ("ntp-server", 56),
    ("v6-access-domain", 57),
    ("sip-ua-cs-list", 58),
    ("bootfile-url", 59),
    ("bootfile-param", 60),
    ("client-arch-type", 61),
    ("nii", 62),
    ("aftr-name", 64),
    ("erp-local-domain-name", 65),
    ("rsoo", 66),
    ("pd-exclude", 67),
    ("rdnss-selection", 74),
    ("client-linklayer-addr", 79),
    ("link-address", 80),
    ("solmax-rt", 82),
    ("inf-max-rt", 83),
    ("dhcpv4-message", 87),
    ("dhcp4o6-server-addr", 88),
    ("s46-cont-mape", 94),
    ("s46-cont-mapt", 95),
    ("s46-cont-lw", 96),
    ("v6-captive-portal", 103),
    ("relay-source-port", 135),
    ("v6-sztp-redirect", 136),
    ("ipv6-address-andsf", 143),
    ("v6-dnr", 144),
    ("addr-reg-enable", 148),
]


def kea_std_options(version: int) -> list[tuple[str, int]]:
    """Return the standard option (name, code) list for the given DHCP version."""
    return KEA_DHCP6_STD_OPTIONS if version == 6 else KEA_DHCP4_STD_OPTIONS


class DHCPOptionConflict(ValueError):
    """An existing DHCP Option cannot be identified safely for an edit."""


class DHCPOptionNameChange(ValueError):
    """A name edit would change the identity of a coded DHCP Option."""


@dataclass(frozen=True)
class DHCPOption:
    """One immutable Kea DHCP Option value."""

    code: int | None
    name: str | None
    space: str | None
    data: str
    csv_format: bool | None
    always_send: bool | None
    never_send: bool | None
    client_classes: tuple[str, ...] = ()

    @property
    def match_key(self) -> tuple[str | None, int | str | None]:
        """Return the option definition identity within its containing configuration."""
        return self.space, self.code if self.code is not None else self.name

    @property
    def assignment_key(self) -> tuple[tuple[str | None, int | str | None], frozenset[str]]:
        """Identify one assignment of an option to a set of client classes."""
        return self.match_key, frozenset(self.client_classes)

    def form_initial(self) -> dict[str, Any]:
        """Return editable values, and the identity and value that the operator saw for this existing row."""
        original: dict[str, Any] = {
            key: value
            for key, value in {
                "space": self.space,
                "code": self.code,
                "name": self.name,
                "data": self.data,
                "always-send": self.always_send,
            }.items()
            if value is not None
        }
        if self.client_classes:
            original["client-classes"] = list(self.client_classes)
        return {
            "name": self.name or "",
            "data": self.data,
            "always_send": bool(self.always_send),
            "original_option": original,
        }

    def same_value(self, other: DHCPOption) -> bool:
        """Return whether both options send the same data with the same always-send flag."""
        return (self.data, self.always_send) == (other.data, other.always_send)

    def matches_intent(self, intended: DHCPOption, *, exact_space: bool = False) -> bool:
        """Return whether this resolved Option is the target of a submitted intent.

        With *exact_space* the two spaces must be equal. Otherwise a space missing on
        either side matches any space, which is only safe once no exact match remains.
        """
        if exact_space:
            same_space = self.space == intended.space
        else:
            same_space = self.space is None or intended.space is None or self.space == intended.space
        if self.code is not None and intended.code is not None:
            return same_space and self.code == intended.code
        return same_space and self.name is not None and self.name == intended.name


_STANDARD_CODES: dict[int, dict[str, int]] = {version: dict(kea_std_options(version)) for version in (4, 6)}


def standard_option_code(version: int, name: str) -> int | None:
    """Return the code of the standard DHCP Option *name* of the family *version*, or None when Kea has none."""
    return _STANDARD_CODES[version].get(name)


_STANDARD_NAMES: dict[int, dict[int, str]] = {
    version: {code: name for name, code in codes.items()} for version, codes in _STANDARD_CODES.items()
}


def option_name(option: DHCPOption, version: int) -> str | None:
    """Return the name of *option*. A default-space entry with a code only takes the standard name of its code."""
    if option.name is not None or option.code is None or option.space not in (None, f"dhcp{version}"):
        return option.name
    return _STANDARD_NAMES[version].get(option.code)


@dataclass(frozen=True)
class FormManagedOption:
    """One default-space standard DHCP Option that a Subnet or Shared Network form field edits."""

    field: str
    name: str
    family: int

    @property
    def code(self) -> int:
        """Return the code that the Kea standard option definitions of the family give the option."""
        return _STANDARD_CODES[self.family][self.name]


# The form reader and the kea.py writer both use this table, so the two cannot disagree.
_FORM_MANAGED_OPTIONS: dict[int, tuple[FormManagedOption, ...]] = {
    4: (
        FormManagedOption("gateway", "routers", 4),
        FormManagedOption("dns_servers", "domain-name-servers", 4),
        FormManagedOption("ntp_servers", "ntp-servers", 4),
    ),
    6: (
        FormManagedOption("dns_servers", "dns-servers", 6),
        FormManagedOption("ntp_servers", "sntp-servers", 6),
    ),
}
# The Subnet table also shows the domain name, which no form edits. DHCPv6 has no domain-name option.
_SHOWN_OPTIONS: dict[int, tuple[FormManagedOption, ...]] = {
    4: (*_FORM_MANAGED_OPTIONS[4], FormManagedOption("domain_name", "domain-name", 4)),
    6: _FORM_MANAGED_OPTIONS[6],
}


def form_managed_options(version: int) -> dict[str, FormManagedOption]:
    """Return the form-edited DHCP Options of one family, keyed by form field."""
    return {option.field: option for option in _FORM_MANAGED_OPTIONS[version]}


class AmbiguousFormOption(ValueError):
    """More than one DHCP Option entry fits one form field, so the form cannot tell which entry it manages."""


def form_managed_entry(options: Sequence[DHCPOption], version: int, field: str) -> int | None:
    """Return the index of the entry that the form field *field* manages, or None when no entry fits.

    The entry is in the default space, has no class tag, and has the code of the field (or its name, without a
    code). The display and the save both select the entry here, so they cannot manage different entries.

    Raises:
        AmbiguousFormOption: If more than one entry fits.

    """
    fits = _fitting_entries(options, version, form_managed_options(version)[field])
    if len(fits) > 1:
        raise AmbiguousFormOption(f"More than one DHCP Option entry fits the {field} field.")
    return fits[0] if fits else None


def _fitting_entries(options: Sequence[DHCPOption], version: int, managed: FormManagedOption) -> list[int]:
    """Return the index of each default-space entry with no class tag and the code (or name) of *managed*."""
    return [
        index
        for index, option in enumerate(options)
        if option.space in (None, f"dhcp{version}")
        and not option.client_classes
        and (option.code == managed.code if option.code is not None else option.name == managed.name)
    ]


@dataclass(frozen=True)
class ShownOption:
    """What the Subnet table shows for one field: the data of its entry, or that more than one entry fits."""

    data: str
    ambiguous: bool


def shown_options(options: Sequence[DHCPOption], version: int) -> dict[str, ShownOption]:
    """Return what the Subnet table shows for each field that has a fitting entry, keyed by field.

    The rule is the one of ``form_managed_entry``. The table shows data that the form cannot edit, such as binary
    data or a router list, because ``form_shows`` controls editing only.
    """
    shown: dict[str, ShownOption] = {}
    for managed in _SHOWN_OPTIONS[version]:
        fits = _fitting_entries(options, version, managed)
        if len(fits) > 1:
            shown[managed.field] = ShownOption(data="", ambiguous=True)
        elif fits and options[fits[0]].data:
            shown[managed.field] = ShownOption(data=options[fits[0]].data, ambiguous=False)
    return shown


def form_shows(option: DHCPOption, field: str) -> bool:
    """Return whether the form field *field* can show the data of its entry *option*.

    The form has no way to enter binary data, and the gateway field holds one address, not a router list.
    """
    return option.csv_format is not False and not (field == "gateway" and "," in option.data)


def form_option_fields(options: Sequence[DHCPOption], version: int) -> dict[str, str]:
    """Return the data that each Subnet or Shared Network form field shows, keyed by form field.

    Raises:
        AmbiguousFormOption: If more than one entry fits a field.

    """
    fields: dict[str, str] = {}
    for field in form_managed_options(version):
        index = form_managed_entry(options, version, field)
        if index is not None and form_shows(options[index], field):
            fields[field] = options[index].data
    return fields


class InvalidAddress(ValueError):
    """An entry of an address list that is not an IP address."""

    def __init__(self, entry: str) -> None:
        """Keep the *entry* for the message of the form."""
        super().__init__(f"{entry!r} is not an IP address.")
        self.entry = entry


def address_list(text: str) -> tuple[str, ...]:
    """Return each address of the comma-separated *text* in its canonical form, without the empty entries.

    Raises:
        InvalidAddress: For the first entry that is not an IP address.

    """
    addresses: list[str] = []
    for entry in (entry.strip() for entry in text.split(",")):
        if not entry:
            continue
        try:
            addresses.append(str(ipaddress.ip_address(entry)))
        except ValueError as exc:
            raise InvalidAddress(entry) from exc
    return tuple(addresses)


def address_list_data(addresses: Sequence[str]) -> str:
    """Return the option data of *addresses*: each address joined with a comma and a space, as in the Kea ARM."""
    return ", ".join(addresses)


def parse_dhcp_option(entry: Any) -> DHCPOption:
    """Parse one raw Kea option-data entry.

    Raises:
        ValueError: If the entry is not a complete, valid DHCP Option value.

    """
    if not isinstance(entry, dict):
        raise ValueError("A DHCP Option must be an object.")
    code = entry.get("code")
    if code is not None and (isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 65_535):
        raise ValueError("A DHCP Option code must be an integer from 0 through 65535.")
    name = entry.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise ValueError("A DHCP Option name must be a non-empty string.")
    if code is None and name is None:
        raise ValueError("A DHCP Option requires a code or name.")
    space = entry.get("space")
    if space is not None and (not isinstance(space, str) or not space):
        raise ValueError("A DHCP Option space must be a non-empty string.")
    data = entry.get("data", "")
    if not isinstance(data, str):
        raise ValueError("A DHCP Option data value must be a string.")
    flags = (entry.get("csv-format"), entry.get("always-send"), entry.get("never-send"))
    if any(flag is not None and not isinstance(flag, bool) for flag in flags):
        raise ValueError("DHCP Option delivery flags must be Boolean values.")
    client_classes = entry.get("client-classes", [])
    if not isinstance(client_classes, list) or not all(isinstance(tag, str) and tag for tag in client_classes):
        raise ValueError("DHCP Option class tags must be a list of non-empty strings.")
    return DHCPOption(
        code=code,
        name=name,
        space=space,
        data=data,
        csv_format=flags[0],
        always_send=flags[1],
        never_send=flags[2],
        client_classes=tuple(client_classes),
    )


def parse_dhcp_options(entries: Any) -> tuple[DHCPOption, ...]:
    """Parse an ordered raw Kea option-data collection."""
    if not isinstance(entries, list):
        raise ValueError("DHCP Options must be a list.")
    return tuple(parse_dhcp_option(entry) for entry in entries)


def merge_option_form_rows(rows: list[dict[str, Any]], existing: Any) -> list[dict[str, Any]]:
    """Merge exposed edits onto fresh raw options selected by their typed identity.

    Raises:
        DHCPOptionConflict: If a row's option is missing, ambiguous, submitted twice, or has a changed live value.
        DHCPOptionNameChange: If a row renames a coded DHCP Option.

    """
    parsed = parse_dhcp_options(existing)
    used: set[tuple[tuple[str | None, int | str | None], frozenset[str]]] = set()
    result = []
    for row in rows:
        original = row.get("original_option")
        if original is not None:
            seen = parse_dhcp_option(original)
            key = seen.assignment_key
            matches = [index for index, option in enumerate(parsed) if option.assignment_key == key]
            if len(matches) != 1 or key in used:
                raise DHCPOptionConflict("An existing DHCP Option is missing, ambiguous, or submitted twice.")
            if not parsed[matches[0]].same_value(seen):
                raise DHCPOptionConflict("The live value of a DHCP Option changed. Reload the form before saving.")
            used.add(key)
            option = dict(existing[matches[0]])
        else:
            option = {}
        if row.get("DELETE"):
            continue
        name = row["name"]
        if option.get("code") is not None and name and name != option.get("name"):
            raise DHCPOptionNameChange("An existing coded DHCP Option cannot be renamed.")
        if name:
            option["name"] = name
        else:
            option.pop("name", None)
        if "data" in option or row["data"]:
            option["data"] = row["data"]
        if "always-send" in option or row.get("always_send"):
            option["always-send"] = bool(row.get("always_send"))
        parse_dhcp_option(option)
        result.append(option)
    if len(used) != len(parsed):
        raise DHCPOptionConflict("The live DHCP Option list changed. Reload the form before saving.")
    return result

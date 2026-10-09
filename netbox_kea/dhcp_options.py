# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .constants import kea_std_options


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

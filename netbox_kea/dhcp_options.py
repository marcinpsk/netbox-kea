from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    @property
    def match_key(self) -> tuple[str | None, int | str | None]:
        """Return the option identity within its containing configuration."""
        return self.space, self.code if self.code is not None else self.name


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
    return DHCPOption(
        code=code,
        name=name,
        space=space,
        data=data,
        csv_format=flags[0],
        always_send=flags[1],
        never_send=flags[2],
    )


def parse_dhcp_options(entries: Any) -> tuple[DHCPOption, ...]:
    """Parse an ordered raw Kea option-data collection."""
    if not isinstance(entries, list):
        raise ValueError("DHCP Options must be a list.")
    return tuple(parse_dhcp_option(entry) for entry in entries)

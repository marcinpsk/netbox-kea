# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Subnet Settings and the Kea key of each scalar field, which the reader and the writers share."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .constants import IPAddressValue


@dataclass(frozen=True)
class SubnetSettings:
    """Effective typed DHCP settings that the repository currently consumes."""

    valid_lifetime: int | None = None
    min_valid_lifetime: int | None = None
    max_valid_lifetime: int | None = None
    preferred_lifetime: int | None = None
    min_preferred_lifetime: int | None = None
    max_preferred_lifetime: int | None = None
    offer_lifetime: int | None = None
    renew_timer: int | None = None
    rebind_timer: int | None = None
    allocator: str | None = None
    pd_allocator: str | None = None
    ddns_qualifying_suffix: str | None = None
    interface_id: str | None = None
    relay_addresses: tuple[IPAddressValue, ...] = ()
    client_classes: tuple[str, ...] = ()
    require_client_classes: tuple[str, ...] = ()


# integer: a non-negative integer. string: a non-empty string. string_or_empty: a string that can be empty.
ValueKind = Literal["integer", "string", "string_or_empty"]


@dataclass(frozen=True)
class SettingKey:
    """The Kea key of one scalar Subnet Settings field and the kind of its value."""

    key: str
    kind: ValueKind


# Each scalar field of SubnetSettings. The relay and class keys have their own shapes, so their code is by hand.
SETTING_KEYS: dict[str, SettingKey] = {
    "valid_lifetime": SettingKey("valid-lifetime", "integer"),
    "min_valid_lifetime": SettingKey("min-valid-lifetime", "integer"),
    "max_valid_lifetime": SettingKey("max-valid-lifetime", "integer"),
    "preferred_lifetime": SettingKey("preferred-lifetime", "integer"),
    "min_preferred_lifetime": SettingKey("min-preferred-lifetime", "integer"),
    "max_preferred_lifetime": SettingKey("max-preferred-lifetime", "integer"),
    "offer_lifetime": SettingKey("offer-lifetime", "integer"),
    "renew_timer": SettingKey("renew-timer", "integer"),
    "rebind_timer": SettingKey("rebind-timer", "integer"),
    "allocator": SettingKey("allocator", "string"),
    "pd_allocator": SettingKey("pd-allocator", "string"),
    "ddns_qualifying_suffix": SettingKey("ddns-qualifying-suffix", "string_or_empty"),
    "interface_id": SettingKey("interface-id", "string"),
}


def setting_key(field: str) -> str:
    """Return the Kea key of the scalar Subnet Settings field *field*."""
    return SETTING_KEYS[field].key

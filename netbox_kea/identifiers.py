# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Normalize the hexadecimal client identifiers that Reservations and Leases carry."""

from __future__ import annotations

import re


def normalize_hex(value: str, octets: tuple[int, int]) -> str:
    """Return *value* as lowercase colon-separated octets, within the inclusive *octets* bounds.

    The value can use colon, dash or dot separators, or none.

    Raises:
        ValueError: If *value* is not hexadecimal octets within the bounds.

    """
    compact = re.sub(r"[:.-]", "", value)
    if not compact or len(compact) % 2 or re.fullmatch(r"[0-9A-Fa-f]+", compact) is None:
        raise ValueError("The identifier is not hexadecimal octets.")
    minimum, maximum = octets
    if not minimum <= len(compact) // 2 <= maximum:
        raise ValueError("The identifier has an unsupported number of octets.")
    return ":".join(compact[index : index + 2].lower() for index in range(0, len(compact), 2))

# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Parse the unsigned decimal numbers that request text carries."""

from __future__ import annotations

import re


def parse_decimal(text: str) -> int:
    """Return *text* as an integer when it contains only the ASCII digits 0 to 9.

    ``int()`` alone also accepts other Unicode decimal digits and surrounding whitespace.

    Raises:
        ValueError: If *text* is not ASCII decimal digits.

    """
    if re.fullmatch(r"[0-9]+", text) is None:
        raise ValueError("The value is not ASCII decimal digits.")
    return int(text)

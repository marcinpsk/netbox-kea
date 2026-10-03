# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Validate live connectivity when a submission creates or changes a Server connection."""

import json
import logging
from typing import TYPE_CHECKING

import requests
from django.core.exceptions import ValidationError

from .constants import Family
from .kea import KeaCommand, KeaException

if TYPE_CHECKING:
    from .models import Server

logger = logging.getLogger(__name__)

CONNECTION_FIELDS = (
    "ca_url",
    "ca_username",
    "ca_password",
    "dhcp4_url",
    "dhcp4_username",
    "dhcp4_password",
    "dhcp6_url",
    "dhcp6_username",
    "dhcp6_password",
    "ssl_verify",
    "ca_file_path",
    "client_cert_path",
    "client_key_path",
    "has_control_agent",
    "dhcp4",
    "dhcp6",
)
ConnectionValues = tuple[str | bool, ...]


def connection_values(server: "Server") -> ConnectionValues:
    """Capture the normalized connection values before native submission validation mutates the Server."""
    return tuple(getattr(server, field) for field in CONNECTION_FIELDS)


def validate_connection_change(server: "Server", before: ConnectionValues | None) -> None:
    """Probe enabled services on creation or an actual connection change. None means creation."""
    if before is not None and connection_values(server) == before:
        return
    services: tuple[tuple[str, Family], ...] = (("dhcp6", 6), ("dhcp4", 4))
    for field, family in services:
        if not getattr(server, field):
            continue
        try:
            server.get_client(version=family).command(KeaCommand.VERSION_GET, family)
        except json.JSONDecodeError as exc:
            logger.exception("Malformed response during DHCPv%s connectivity check", family)
            raise ValidationError({field: "An internal error occurred."}) from exc
        except (KeaException, requests.RequestException, ValueError) as exc:
            logger.exception("DHCPv%s connectivity check failed during Server submission", family)
            raise ValidationError({field: f"Unable to reach the Kea DHCPv{family} service."}) from exc

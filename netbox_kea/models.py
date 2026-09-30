# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2023 Devon Mar <devon-mar@users.noreply.github.com>
# SPDX-License-Identifier: Apache-2.0
import json
import logging
from functools import reduce
from operator import or_
from pathlib import Path
from typing import get_args

import requests
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import connection, models
from django.urls import reverse
from netbox.constants import CENSOR_TOKEN, CENSOR_TOKEN_CHANGED
from netbox.models import NetBoxModel
from netbox.models.features import JobsMixin

from . import branching
from .constants import Family
from .kea import KeaClient, KeaCommand, KeaException
from .reservations import MAX_IDENTITY_LENGTH

logger = logging.getLogger(__name__)


def _get_kea_timeout(default: int = 30) -> int:
    """Return kea_timeout from PLUGINS_CONFIG, coerced to int with a safe fallback."""
    plugins_config = getattr(settings, "PLUGINS_CONFIG", {})
    if not isinstance(plugins_config, dict):
        return default
    raw = (plugins_config.get("netbox_kea") or {}).get("kea_timeout", default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _get_max_unpaged_leases(default: int = 1000) -> int | None:
    """Return the Subnet lease-query guard limit, or ``None`` when disabled."""
    plugins_config = getattr(settings, "PLUGINS_CONFIG", {})
    if not isinstance(plugins_config, dict):
        return default
    plugin_config = plugins_config.get("netbox_kea") or {}
    if not isinstance(plugin_config, dict):
        return default
    raw = plugin_config.get("lease_query_max_unpaged_leases", default)
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            return default
    else:
        return default
    if value == 0:
        return None
    return value if value > 0 else default


class Server(JobsMixin, NetBoxModel):
    """A Kea DHCP server instance managed through the Kea Control API."""

    name = models.CharField(unique=True, max_length=255)
    ca_url = models.CharField(
        verbose_name="CA / Server URL",
        max_length=255,
        help_text="Default endpoint URL (Kea Control Agent or single DHCP daemon).",
    )
    ca_username = models.CharField(
        blank=True,
        default="",
        max_length=255,
        verbose_name="CA Username",
        help_text="Username for the Kea Control Agent (or default for all daemons).",
    )
    ca_password = models.CharField(
        blank=True,
        default="",
        max_length=255,
        verbose_name="CA Password",
        help_text="Password for the Kea Control Agent (or default for all daemons).",
    )
    dhcp4_username = models.CharField(
        blank=True,
        default="",
        max_length=255,
        verbose_name="DHCPv4 Username",
        help_text="Username for the DHCPv4 daemon. Only used when DHCPv4 URL is configured; falls back to CA credentials otherwise.",
    )
    dhcp4_password = models.CharField(
        blank=True,
        default="",
        max_length=255,
        verbose_name="DHCPv4 Password",
        help_text="Password for the DHCPv4 daemon. Only used when DHCPv4 URL is configured; falls back to CA credentials otherwise.",
    )
    dhcp6_username = models.CharField(
        blank=True,
        default="",
        max_length=255,
        verbose_name="DHCPv6 Username",
        help_text="Username for the DHCPv6 daemon. Only used when DHCPv6 URL is configured; falls back to CA credentials otherwise.",
    )
    dhcp6_password = models.CharField(
        blank=True,
        default="",
        max_length=255,
        verbose_name="DHCPv6 Password",
        help_text="Password for the DHCPv6 daemon. Only used when DHCPv6 URL is configured; falls back to CA credentials otherwise.",
    )
    ssl_verify = models.BooleanField(
        default=True,
        verbose_name="SSL Verification",
        help_text="Enable SSL certificate verification. Disable with caution!",
    )
    client_cert_path = models.CharField(
        max_length=4096,
        blank=True,
        default="",
        verbose_name="Client Certificate",
        help_text="Optional client certificate.",
    )
    client_key_path = models.CharField(
        max_length=4096,
        blank=True,
        default="",
        verbose_name="Private Key",
        help_text="Optional client key.",
    )
    ca_file_path = models.CharField(
        max_length=4096,
        blank=True,
        default="",
        verbose_name="CA File Path",
        help_text="The specific CA certificate file to use for SSL verification.",
    )
    dhcp6 = models.BooleanField(verbose_name="DHCPv6", default=True)
    dhcp4 = models.BooleanField(verbose_name="DHCPv4", default=True)
    dhcp4_url = models.CharField(
        verbose_name="DHCPv4 URL",
        max_length=255,
        blank=True,
        default="",
        help_text="Direct URL for the DHCPv4 daemon. Overrides Server URL for DHCPv4 connections.",
    )
    dhcp6_url = models.CharField(
        verbose_name="DHCPv6 URL",
        max_length=255,
        blank=True,
        default="",
        help_text="Direct URL for the DHCPv6 daemon. Overrides Server URL for DHCPv6 connections.",
    )
    has_control_agent = models.BooleanField(
        verbose_name="Has Control Agent",
        default=True,
        help_text=(
            "Enable if connecting via kea-ctrl-agent. Disable when connecting directly to DHCP daemon endpoints."
        ),
    )
    sync_enabled = models.BooleanField(
        verbose_name="IPAM Sync Enabled",
        default=True,
        help_text="Include this server in the periodic Kea→NetBox IPAM sync job.",
    )
    sync_leases_enabled = models.BooleanField(
        verbose_name="Sync Leases",
        default=True,
        help_text="Sync active DHCP leases as NetBox IP Addresses for this server.",
    )
    sync_reservations_enabled = models.BooleanField(
        verbose_name="Sync Reservations",
        default=True,
        help_text="Sync DHCP reservations as NetBox IP Addresses for this server.",
    )
    sync_prefixes_enabled = models.BooleanField(
        verbose_name="Sync Prefixes",
        default=True,
        help_text="Sync Kea subnets as NetBox IP Prefixes for this server.",
    )
    sync_ip_ranges_enabled = models.BooleanField(
        verbose_name="Sync IP Ranges",
        default=True,
        help_text="Sync Kea pools as NetBox IP Ranges for this server.",
    )
    sync_dhcp_plugin_enabled = models.BooleanField(
        verbose_name="Sync to DHCP plugin",
        default=False,
        help_text=(
            "When the NetBox DHCP plugin (netbox_dhcp) is installed, allow importing this "
            "server's Kea data tier (subnets, pools, host reservations) into the DHCP plugin's "
            "models. Note: Kea shared-networks are a different concept and are NOT imported as "
            "DHCP-plugin Shared Networks (their member subnets are imported individually); DHCP "
            "options are not imported. Has no effect if the plugin is absent."
        ),
    )
    persist_config = models.BooleanField(
        verbose_name="Persist configuration",
        default=True,
        help_text=(
            "When enabled, Kea's configuration file is automatically saved "
            "(config-write) after each change so modifications survive a "
            "Kea daemon restart. Disable if Kea configuration is managed externally "
            "(e.g. Ansible, Puppet) or if you manage persistence manually."
        ),
    )
    sync_vrf = models.ForeignKey(
        to="ipam.VRF",
        # PROTECT, not SET_NULL: from a branch, SET_NULL would null main's row (ADR 0007).
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Sync VRF",
        help_text="VRF to assign when syncing subnets as Prefixes and pools as IP Ranges. Leave blank for the global VRF.",
    )

    class Meta:
        ordering = ("name",)
        permissions = [
            ("bulk_delete_lease_from_server", "Can bulk delete DHCP leases from server"),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        """Return the detail URL for this server."""
        return reverse("plugins:netbox_kea:server", args=[self.pk])

    @property
    def docs_url(self) -> str | None:
        """Suppress NetBox's Help button, which this package ships no page for.

        NetBoxModel builds the URL from the app label unconditionally, so the button
        renders for a plugin model too and lands on a 404.
        """
        return None

    def get_client(self, version: Family | None = None) -> KeaClient:
        """Return a configured KeaClient, targeting the protocol-specific URL and credentials when available.

        The ``service`` command argument is sent only when this server is fronted by
        a Control Agent (``has_control_agent``), which routes commands to daemons by
        service. When connecting directly to a DHCP daemon (``has_control_agent`` is
        False) ``service`` is dropped, because Kea 3.2.0+ rejects a ``service`` that
        does not match the daemon the request lands on. ``has_control_agent`` is the
        single source of truth here: a protocol-specific ``dhcp{4,6}_url`` may itself
        point at a per-protocol Control Agent (Kea < 3.0) or a bare daemon socket
        (Kea 3.0+), so the routing decision follows the flag, not the URL.

        The client is bound to the branch that is active now, so it refuses each Kea write
        command in a branch, also from a clone in a worker thread (ADR 0007).

        Args:
            version: DHCP protocol version (4 or 6). When provided and a protocol-specific
                URL is configured, that URL and its corresponding per-protocol credentials
                (with per-field CA fallback) are used. If no protocol-specific URL is
                configured, ``ca_url`` and CA-level credentials are always used.

        """
        if version == 4 and self.dhcp4_url:
            url = self.dhcp4_url
            username = self.dhcp4_username or self.ca_username or None
            password = self.dhcp4_password or self.ca_password or None
        elif version == 6 and self.dhcp6_url:
            url = self.dhcp6_url
            username = self.dhcp6_username or self.ca_username or None
            password = self.dhcp6_password or self.ca_password or None
        else:
            url = self.ca_url
            username = self.ca_username or None
            password = self.ca_password or None

        if version in (4, 6):
            from .server_configuration import invalidate

            def on_config_change() -> None:
                invalidate(self, version)

        else:
            on_config_change = None

        return KeaClient(
            url=url,
            username=username,
            password=password,
            verify=self.ca_file_path or self.ssl_verify,
            client_cert=self.client_cert_path or None,
            client_key=self.client_key_path or None,
            timeout=_get_kea_timeout(),
            persist_config=self.persist_config,
            send_service=self.has_control_agent,
            max_unpaged_leases=_get_max_unpaged_leases(),
            on_config_change=on_config_change,
            write_guard=branching.bind(),
        )

    def clean(self) -> None:
        """Validate configuration and perform a live connectivity check against Kea."""
        super().clean()

        if self.dhcp4 is False and self.dhcp6 is False:
            raise ValidationError({"dhcp6": "At least one of DHCPv4 and DHCPv6 needs to be enabled."})

        if (self.client_cert_path and not self.client_key_path) or (not self.client_cert_path and self.client_key_path):
            raise ValidationError(
                {"client_cert_path": "Client certificate and client private key must be used together."}
            )

        if self.client_cert_path and not Path(self.client_cert_path).is_file():
            raise ValidationError({"client_cert_path": "Client certificate doesn't exist."})
        if self.client_key_path and not Path(self.client_key_path).is_file():
            raise ValidationError({"client_key_path": "Client private key doesn't exist."})

        if self.ca_file_path and not self.ssl_verify:
            raise ValidationError({"ca_file_path": "Cannot specify a CA file when SSL verification is disabled."})

        if self.dhcp6:
            try:
                self.get_client(version=6).command(KeaCommand.VERSION_GET, 6)
            except KeaException as e:
                logger.exception("DHCPv6 connectivity check failed during Server.clean()")
                raise ValidationError({"dhcp6": "Unable to reach the Kea DHCPv6 service."}) from e
            except json.JSONDecodeError as e:
                logger.exception("Malformed response during DHCPv6 connectivity check")
                raise ValidationError({"dhcp6": "An internal error occurred."}) from e
            except (requests.exceptions.RequestException, ValueError) as e:
                logger.exception("Unexpected error during DHCPv6 connectivity check")
                raise ValidationError({"dhcp6": "Unable to reach the Kea DHCPv6 service."}) from e
        if self.dhcp4:
            try:
                self.get_client(version=4).command(KeaCommand.VERSION_GET, 4)
            except KeaException as e:
                logger.exception("DHCPv4 connectivity check failed during Server.clean()")
                raise ValidationError({"dhcp4": "Unable to reach the Kea DHCPv4 service."}) from e
            except json.JSONDecodeError as e:
                logger.exception("Malformed response during DHCPv4 connectivity check")
                raise ValidationError({"dhcp4": "An internal error occurred."}) from e
            except (requests.exceptions.RequestException, ValueError) as e:
                logger.exception("Unexpected error during DHCPv4 connectivity check")
                raise ValidationError({"dhcp4": "Unable to reach the Kea DHCPv4 service."}) from e

    def to_objectchange(self, action: str):
        """Censor all password fields in NetBox change log entries."""
        objectchange = super().to_objectchange(action)

        password_fields = ("ca_password", "dhcp4_password", "dhcp6_password")

        prechange_data = objectchange.prechange_data or {}
        original_pre_passwords = {f: prechange_data.get(f) for f in password_fields}
        # Censor a set password only. An unset one is "" and must stay "", or the change
        # log tells an operator a password exists where none does.
        for field in password_fields:
            if prechange_data.get(field):
                prechange_data[field] = CENSOR_TOKEN

        if post_data := objectchange.postchange_data:
            for field in password_fields:
                post_password = post_data.get(field)
                if post_password:
                    post_data[field] = (
                        CENSOR_TOKEN_CHANGED if post_password != original_pre_passwords[field] else CENSOR_TOKEN
                    )

        return objectchange


class SyncConfig(models.Model):
    """Singleton configuration for the Kea→NetBox IPAM sync job.

    Stores the sync interval and global kill-switch in the database so
    operators can change them from the UI without restarting Django.
    Exactly one row exists (pk=1 always): a migration creates it, ``save()``
    keeps pk=1 and ``delete()`` is disabled.
    """

    interval_minutes = models.PositiveIntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(1440)],
        help_text="How often the background sync job runs (minutes). Range 1–1440.",
    )
    sync_enabled = models.BooleanField(
        default=True,
        help_text="Global kill-switch. When False, no servers are synced regardless of per-server settings.",
    )
    sync_leases_enabled = models.BooleanField(
        default=True,
        help_text="Sync active Kea leases to NetBox IPAM as IP addresses.",
    )
    sync_reservations_enabled = models.BooleanField(
        default=True,
        help_text="Sync Kea reservations to NetBox IPAM as reserved IP addresses.",
    )
    sync_prefixes_enabled = models.BooleanField(
        default=True,
        help_text="Sync Kea subnets to NetBox IPAM as IP Prefixes.",
    )
    sync_ip_ranges_enabled = models.BooleanField(
        default=True,
        help_text="Sync Kea pools to NetBox IPAM as IP Ranges.",
    )

    class Meta:
        app_label = "netbox_kea"
        verbose_name = "Sync Configuration"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(interval_minutes__gte=1) & models.Q(interval_minutes__lte=1440),
                name="syncconfig_interval_minutes_range",
            )
        ]

    def __str__(self) -> str:
        return "Sync Configuration"

    def save(self, *args, **kwargs) -> None:
        """Force pk=1 so only one row can ever exist."""
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Prevent deletion of the singleton row."""
        raise TypeError("SyncConfig singleton cannot be deleted.")

    @classmethod
    def get(cls) -> "SyncConfig":
        """Return the singleton row. Migration 0018 creates it, so a missing row raises ``DoesNotExist``."""
        return cls.objects.get(pk=1)


class KeaDhcpLink(models.Model):
    """Maps a Kea object identity to the netbox-plugin-dhcp (``netbox_dhcp``) record imported from it.

    Kea's ``subnet-id`` is unique only per ``(server, protocol)`` — overlaps across
    servers and between the v4/v6 daemons are normal — but ``netbox_dhcp``'s
    ``Subnet.subnet_id`` is a single *global* unique namespace (and its auto-allocator
    hands out a global ``max+1``).  Kea's id therefore cannot be stored there.  This
    link holds the authoritative ``(server, family, kea_subnet_id)`` identity and points
    — via a ``GenericForeignKey`` so there is no hard migration dependency on
    ``netbox_dhcp`` being installed — at the imported DHCP-plugin object.  It is the
    match key for idempotent re-import and the future home for accept/lock + drift state.

    A Global Reservation has no ``subnet-id``, and ``netbox_dhcp`` derives a
    ``HostReservation``'s family from its Subnet, so a Global row carries no family of
    its own.  ``kea_identity`` holds that missing half of the identity here, which keeps
    the DHCPv4 and DHCPv6 Reservations of one identifier on separate rows.

    Exactly one identity kind is set per row, so the two partial unique constraints
    together cover every link and no row can escape both.
    """

    server = models.ForeignKey(
        to="netbox_kea.Server",
        on_delete=models.CASCADE,
        related_name="dhcp_plugin_links",
    )
    family = models.PositiveSmallIntegerField(
        help_text="IP family of the linked object (4 or 6).",
    )
    kea_subnet_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Kea subnet-id for subnet links; null for objects without a Kea subnet-id.",
    )
    # NULL is a distinct state here, not "empty": it means Kea identifies this link by
    # subnet-id. keadhcplink_one_identity_kind requires NULL or a value greater than "",
    # so "" would satisfy neither branch. Do not collapse it to a blank default.
    kea_identity = models.CharField(  # noqa: DJ001
        max_length=MAX_IDENTITY_LENGTH,
        null=True,
        blank=True,
        help_text=(
            "Normalized 'identifier-type:value' for a Global Reservation link; "
            "null for objects Kea identifies by subnet-id."
        ),
    )
    object_type = models.ForeignKey(
        to="contenttypes.ContentType",
        on_delete=models.CASCADE,
        related_name="+",
    )
    object_id = models.PositiveBigIntegerField()
    sys4_object = GenericForeignKey("object_type", "object_id")
    created = models.DateTimeField(auto_now_add=True)
    last_synced = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "netbox_kea"
        verbose_name = "Kea DHCP-plugin link"
        constraints = [
            models.UniqueConstraint(
                fields=["object_type", "object_id"],
                name="keadhcplink_unique_sys4_object",
            ),
            models.UniqueConstraint(
                fields=["server", "family", "kea_subnet_id"],
                name="keadhcplink_unique_subnet_identity",
                condition=models.Q(kea_subnet_id__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["server", "family", "kea_identity"],
                name="keadhcplink_unique_reservation_identity",
                condition=models.Q(kea_identity__isnull=False),
            ),
            models.CheckConstraint(
                condition=models.Q(kea_subnet_id__isnull=False, kea_identity__isnull=True)
                | models.Q(kea_subnet_id__isnull=True, kea_identity__isnull=False, kea_identity__gt=""),
                name="keadhcplink_one_identity_kind",
            ),
        ]

    def __str__(self) -> str:
        key = f"subnet-id={self.kea_subnet_id}" if self.kea_identity is None else self.kea_identity
        return f"{self.server} v{self.family} {key} → {self.object_type_id}:{self.object_id}"


# One sequence gives every link confirmation and every phase cutoff number (ADR 0006); migration 0019 creates it.
CONFIRMATION_SEQUENCE = "netbox_kea_ipam_ownership_confirmation"


def next_confirmation_number() -> int:
    """Take the next number of the confirmation sequence, for a link confirmation or a phase cutoff."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [CONFIRMATION_SEQUENCE])
        (number,) = cursor.fetchone()
    return number


class IPAMOwnershipSource(models.TextChoices):
    """The Kea source through which a Server owns a NetBox object."""

    LEASE = "lease", "Lease"
    RESERVATION = "reservation", "Reservation"
    SUBNET = "subnet", "Subnet"
    POOL = "pool", "Pool"
    DELEGATED_PREFIX = "delegated-prefix", "Delegated prefix"


OWNED_OBJECT_KEYS = ("ip_address", "prefix", "ip_range")


def _only_key(key: str) -> models.Q:
    return models.Q(**{f"{other}__isnull": other != key for other in OWNED_OBJECT_KEYS})


class IPAMOwnershipLink(models.Model):
    """IPAM Ownership (ADR 0006): one Server and family synchronized one IP address, Prefix or IP Range from one source.

    The row stays in main (ADR 0007). In a branch, a delete that reaches a link is refused before any write.
    """

    server: models.ForeignKey = models.ForeignKey(
        to="netbox_kea.Server",
        on_delete=models.CASCADE,
        related_name="ipam_ownership_links",
    )
    family: models.PositiveSmallIntegerField = models.PositiveSmallIntegerField(
        choices=[(family, f"IPv{family}") for family in get_args(Family)]
    )
    source: models.CharField = models.CharField(max_length=16, choices=IPAMOwnershipSource.choices)
    ip_address: models.ForeignKey = models.ForeignKey(
        to="ipam.IPAddress",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="kea_ownership_links",
    )
    prefix: models.ForeignKey = models.ForeignKey(
        to="ipam.Prefix",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="kea_ownership_links",
    )
    ip_range: models.ForeignKey = models.ForeignKey(
        to="ipam.IPRange",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="kea_ownership_links",
    )
    facts: models.JSONField = models.JSONField(
        null=True,
        blank=True,
        help_text="The facts that the owner last reported for the object; null for a link without facts.",
    )
    confirmation: models.BigIntegerField = models.BigIntegerField(
        help_text="The confirmation sequence number that a run took when it last confirmed the link.",
    )
    stale_mark: models.BigIntegerField = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="The cutoff number of the cleanup that kept this last link as stale; null when it is not stale.",
    )

    class Meta:
        app_label = "netbox_kea"
        verbose_name = "IPAM ownership link"
        constraints = [
            models.CheckConstraint(
                condition=reduce(or_, (_only_key(key) for key in OWNED_OBJECT_KEYS)),
                name="ipamownershiplink_one_object",
            ),
            models.CheckConstraint(
                condition=models.Q(family__in=get_args(Family)),
                name="ipamownershiplink_family",
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=IPAMOwnershipSource.values),
                name="ipamownershiplink_source",
            ),
            *(
                models.UniqueConstraint(
                    fields=["server", "family", "source", key],
                    condition=models.Q(**{f"{key}__isnull": False}),
                    name=f"ipamownershiplink_unique_{key}",
                )
                for key in OWNED_OBJECT_KEYS
            ),
        ]

    def __str__(self) -> str:
        return f"{self.server} IPv{self.family} {self.source} → {self.owned_object}"

    @property
    def owned_object(self) -> models.Model:
        """Return the IP address, Prefix or IP Range that the link names."""
        return next(obj for key in OWNED_OBJECT_KEYS if (obj := getattr(self, key)) is not None)

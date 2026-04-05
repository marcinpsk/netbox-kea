import json
import logging
import os
from typing import Literal

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from netbox.constants import CENSOR_TOKEN, CENSOR_TOKEN_CHANGED
from netbox.models import NetBoxModel
from netbox.models.features import JobsMixin

from .kea import KeaClient, KeaException

logger = logging.getLogger(__name__)


class Server(JobsMixin, NetBoxModel):
    """A Kea DHCP server instance managed through the Kea Control API."""

    name = models.CharField(unique=True, max_length=255)
    ca_url = models.CharField(
        verbose_name="CA / Server URL",
        max_length=255,
        help_text="Default endpoint URL (Kea Control Agent or single DHCP daemon).",
    )
    ca_username = models.CharField(
        null=True,
        blank=True,
        max_length=255,
        verbose_name="CA Username",
        help_text="Username for the Kea Control Agent (or default for all daemons).",
    )
    ca_password = models.CharField(
        null=True,
        blank=True,
        max_length=255,
        verbose_name="CA Password",
        help_text="Password for the Kea Control Agent (or default for all daemons).",
    )
    dhcp4_username = models.CharField(
        null=True,
        blank=True,
        max_length=255,
        verbose_name="DHCPv4 Username",
        help_text="Username for the DHCPv4 daemon. Overrides CA credentials for DHCPv4 connections.",
    )
    dhcp4_password = models.CharField(
        null=True,
        blank=True,
        max_length=255,
        verbose_name="DHCPv4 Password",
        help_text="Password for the DHCPv4 daemon. Overrides CA credentials for DHCPv4 connections.",
    )
    dhcp6_username = models.CharField(
        null=True,
        blank=True,
        max_length=255,
        verbose_name="DHCPv6 Username",
        help_text="Username for the DHCPv6 daemon. Overrides CA credentials for DHCPv6 connections.",
    )
    dhcp6_password = models.CharField(
        null=True,
        blank=True,
        max_length=255,
        verbose_name="DHCPv6 Password",
        help_text="Password for the DHCPv6 daemon. Overrides CA credentials for DHCPv6 connections.",
    )
    ssl_verify = models.BooleanField(
        default=True,
        verbose_name="SSL Verification",
        help_text="Enable SSL certificate verification. Disable with caution!",
    )
    client_cert_path = models.CharField(
        max_length=4096,
        null=True,
        blank=True,
        verbose_name="Client Certificate",
        help_text="Optional client certificate.",
    )
    client_key_path = models.CharField(
        max_length=4096,
        null=True,
        blank=True,
        verbose_name="Private Key",
        help_text="Optional client key.",
    )
    ca_file_path = models.CharField(
        max_length=4096,
        null=True,
        blank=True,
        verbose_name="CA File Path",
        help_text="The specific CA certificate file to use for SSL verification.",
    )
    dhcp6 = models.BooleanField(verbose_name="DHCPv6", default=True)
    dhcp4 = models.BooleanField(verbose_name="DHCPv4", default=True)
    dhcp4_url = models.CharField(
        verbose_name="DHCPv4 URL",
        max_length=255,
        null=True,
        blank=True,
        help_text="Direct URL for the DHCPv4 daemon. Overrides Server URL for DHCPv4 connections.",
    )
    dhcp6_url = models.CharField(
        verbose_name="DHCPv6 URL",
        max_length=255,
        null=True,
        blank=True,
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

    def get_client(self, version: Literal[4, 6] | None = None) -> KeaClient:
        """Return a configured KeaClient, targeting the protocol-specific URL and credentials when available.

        Args:
            version: DHCP protocol version (4 or 6). When provided and a protocol-specific
                URL is configured, that URL is used instead of ``ca_url``.
                Per-protocol credentials (dhcp4_username/password or dhcp6_username/password)
                take precedence over CA-level credentials when set.

        """
        if version == 4 and self.dhcp4_url:
            url = self.dhcp4_url
        elif version == 6 and self.dhcp6_url:
            url = self.dhcp6_url
        else:
            url = self.ca_url

        if version == 4 and (self.dhcp4_username or self.dhcp4_password):
            username = self.dhcp4_username
            password = self.dhcp4_password
        elif version == 6 and (self.dhcp6_username or self.dhcp6_password):
            username = self.dhcp6_username
            password = self.dhcp6_password
        else:
            username = self.ca_username
            password = self.ca_password

        return KeaClient(
            url=url,
            username=username,
            password=password,
            verify=self.ca_file_path or self.ssl_verify,
            client_cert=self.client_cert_path or None,
            client_key=self.client_key_path or None,
            timeout=settings.PLUGINS_CONFIG["netbox_kea"]["kea_timeout"],
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

        if self.client_cert_path and not os.path.isfile(self.client_cert_path):
            raise ValidationError({"client_cert_path": "Client certificate doesn't exist."})
        if self.client_key_path and not os.path.isfile(self.client_key_path):
            raise ValidationError({"client_key_path": "Client private key doesn't exist."})

        if self.ca_file_path and not self.ssl_verify:
            raise ValidationError({"ca_file_path": "Cannot specify a CA file when SSL verification is disabled."})

        if self.dhcp6:
            try:
                self.get_client(version=6).command("version-get", service=["dhcp6"])
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
                self.get_client(version=4).command("version-get", service=["dhcp4"])
            except KeaException as e:
                logger.exception("DHCPv4 connectivity check failed during Server.clean()")
                raise ValidationError({"dhcp4": "Unable to reach the Kea DHCPv4 service."}) from e
            except json.JSONDecodeError as e:
                logger.exception("Malformed response during DHCPv4 connectivity check")
                raise ValidationError({"dhcp4": "An internal error occurred."}) from e
            except (requests.exceptions.RequestException, ValueError) as e:
                logger.exception("Unexpected error during DHCPv4 connectivity check")
                raise ValidationError({"dhcp4": "Unable to reach the Kea DHCPv4 service."}) from e

    def to_objectchange(self, action: str) -> None:
        """Censor all password fields in NetBox change log entries."""
        objectchange = super().to_objectchange(action)

        password_fields = ("ca_password", "dhcp4_password", "dhcp6_password")

        prechange_data = objectchange.prechange_data or {}
        original_pre_passwords = {f: prechange_data.get(f) for f in password_fields}
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
    Exactly one row exists (pk=1 always); ``save()`` enforces this and
    ``delete()`` is disabled.
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

    class Meta:
        app_label = "netbox_kea"
        verbose_name = "Sync Configuration"
        constraints = [
            models.CheckConstraint(
                check=models.Q(interval_minutes__gte=1) & models.Q(interval_minutes__lte=1440),
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
    def get(cls, default_interval: int = 5) -> "SyncConfig":
        """Return the singleton config row, creating it with defaults if absent.

        ``default_interval`` is used only when the row does not yet exist
        (i.e., on first boot before the operator has saved anything via the
        UI).  Pass the value from ``PLUGINS_CONFIG`` so the config file is
        honoured until the UI overrides it.
        """
        obj, _ = cls.objects.get_or_create(pk=1, defaults={"interval_minutes": default_interval})
        return obj

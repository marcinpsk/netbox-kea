import logging
import os
from typing import Literal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.constants import CENSOR_TOKEN, CENSOR_TOKEN_CHANGED
from netbox.models import NetBoxModel

from .kea import KeaClient, KeaException

logger = logging.getLogger(__name__)


class Server(NetBoxModel):
    """A Kea DHCP server instance managed through the Kea Control API."""

    name = models.CharField(unique=True, max_length=255)
    server_url = models.CharField(
        verbose_name="Server URL",
        max_length=255,
        help_text="Default endpoint URL (Kea Control Agent or single DHCP daemon).",
    )
    username = models.CharField(null=True, blank=True, max_length=255)
    password = models.CharField(null=True, blank=True, max_length=255)
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
        """Return a configured KeaClient, targeting the protocol-specific URL when available.

        Args:
            version: DHCP protocol version (4 or 6). When provided and a protocol-specific
                URL is configured, that URL is used instead of ``server_url``.

        """
        if version == 4 and self.dhcp4_url:
            url = self.dhcp4_url
        elif version == 6 and self.dhcp6_url:
            url = self.dhcp6_url
        else:
            url = self.server_url
        return KeaClient(
            url=url,
            username=self.username,
            password=self.password,
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
            except Exception as e:
                logger.exception("Unexpected error during DHCPv6 connectivity check")
                raise ValidationError({"dhcp6": "Unable to reach the Kea DHCPv6 service."}) from e
        if self.dhcp4:
            try:
                self.get_client(version=4).command("version-get", service=["dhcp4"])
            except KeaException as e:
                logger.exception("DHCPv4 connectivity check failed during Server.clean()")
                raise ValidationError({"dhcp4": "Unable to reach the Kea DHCPv4 service."}) from e
            except Exception as e:
                logger.exception("Unexpected error during DHCPv4 connectivity check")
                raise ValidationError({"dhcp4": "Unable to reach the Kea DHCPv4 service."}) from e

    def to_objectchange(self, action: str) -> None:
        """Censor password in NetBox change log entries."""
        objectchange = super().to_objectchange(action)

        prechange_data = objectchange.prechange_data or {}
        if prechange_data.get("password"):
            prechange_data["password"] = CENSOR_TOKEN

        if (post_data := objectchange.postchange_data) and (post_password := post_data.get("password")):
            post_data["password"] = (
                CENSOR_TOKEN_CHANGED if post_password != prechange_data.get("password") else CENSOR_TOKEN
            )

        return objectchange

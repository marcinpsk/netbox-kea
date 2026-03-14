# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add dhcp4_url, dhcp6_url, and has_control_agent fields to Server."""

    dependencies = [
        ("netbox_kea", "0002_alter_server_options_server_dhcp4_server_dhcp6"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="dhcp4_url",
            field=models.CharField(
                blank=True,
                help_text="Direct URL for the DHCPv4 daemon. Overrides Server URL for DHCPv4 connections.",
                max_length=255,
                null=True,
                verbose_name="DHCPv4 URL",
            ),
        ),
        migrations.AddField(
            model_name="server",
            name="dhcp6_url",
            field=models.CharField(
                blank=True,
                help_text="Direct URL for the DHCPv6 daemon. Overrides Server URL for DHCPv6 connections.",
                max_length=255,
                null=True,
                verbose_name="DHCPv6 URL",
            ),
        ),
        migrations.AddField(
            model_name="server",
            name="has_control_agent",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Enable if connecting via kea-ctrl-agent. "
                    "Disable when connecting directly to DHCP daemon endpoints."
                ),
                verbose_name="Has Control Agent",
            ),
        ),
    ]

# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Add prefix/range sync support: SyncConfig type toggles, Server sync_vrf FK, Server type toggles."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ipam", "0001_squashed"),
        ("netbox_kea", "0009_per_protocol_credentials"),
    ]

    operations = [
        # Per-type sync toggles on SyncConfig (global settings)
        migrations.AddField(
            model_name="syncconfig",
            name="sync_leases_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Sync active Kea leases to NetBox IPAM as IP addresses.",
            ),
        ),
        migrations.AddField(
            model_name="syncconfig",
            name="sync_reservations_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Sync Kea reservations to NetBox IPAM as reserved IP addresses.",
            ),
        ),
        migrations.AddField(
            model_name="syncconfig",
            name="sync_prefixes_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Sync Kea subnets to NetBox IPAM as IP Prefixes.",
            ),
        ),
        migrations.AddField(
            model_name="syncconfig",
            name="sync_ip_ranges_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Sync Kea pools to NetBox IPAM as IP Ranges.",
            ),
        ),
        # VRF assignment on Server
        migrations.AddField(
            model_name="server",
            name="sync_vrf",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="ipam.vrf",
                verbose_name="Sync VRF",
                help_text="VRF to assign when syncing subnets as Prefixes and pools as IP Ranges. Leave blank for the global VRF.",
            ),
        ),
        # Per-type sync toggle overrides on Server
        migrations.AddField(
            model_name="server",
            name="sync_leases_enabled",
            field=models.BooleanField(
                default=True,
                verbose_name="Sync Leases",
                help_text="Sync active DHCP leases as NetBox IP Addresses for this server.",
            ),
        ),
        migrations.AddField(
            model_name="server",
            name="sync_reservations_enabled",
            field=models.BooleanField(
                default=True,
                verbose_name="Sync Reservations",
                help_text="Sync DHCP reservations as NetBox IP Addresses for this server.",
            ),
        ),
        migrations.AddField(
            model_name="server",
            name="sync_prefixes_enabled",
            field=models.BooleanField(
                default=True,
                verbose_name="Sync Prefixes",
                help_text="Sync Kea subnets as NetBox IP Prefixes for this server.",
            ),
        ),
        migrations.AddField(
            model_name="server",
            name="sync_ip_ranges_enabled",
            field=models.BooleanField(
                default=True,
                verbose_name="Sync IP Ranges",
                help_text="Sync Kea pools as NetBox IP Ranges for this server.",
            ),
        ),
    ]

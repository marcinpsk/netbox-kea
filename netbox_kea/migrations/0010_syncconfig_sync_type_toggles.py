# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Add per-type sync toggle fields to SyncConfig."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0009_per_protocol_credentials"),
    ]

    operations = [
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
    ]

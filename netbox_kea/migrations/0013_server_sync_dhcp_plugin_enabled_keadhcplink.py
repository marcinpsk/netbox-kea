# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Add Server.sync_dhcp_plugin_enabled toggle and the KeaDhcpLink mapping model.

KeaDhcpLink maps a Kea ``(server, family, subnet-id)`` identity to the imported
netbox_dhcp object via a GenericForeignKey, so there is no migration dependency on
the optional netbox_dhcp plugin.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("netbox_kea", "0012_syncconfig_backfill_applied"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="sync_dhcp_plugin_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="KeaDhcpLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("family", models.PositiveSmallIntegerField()),
                ("kea_subnet_id", models.PositiveIntegerField(blank=True, null=True)),
                ("object_id", models.PositiveBigIntegerField()),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("last_synced", models.DateTimeField(auto_now=True)),
                (
                    "object_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="+", to="contenttypes.contenttype"
                    ),
                ),
                (
                    "server",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dhcp_plugin_links",
                        to="netbox_kea.server",
                    ),
                ),
            ],
            options={
                "verbose_name": "Kea DHCP-plugin link",
                "constraints": [
                    models.UniqueConstraint(fields=("object_type", "object_id"), name="keadhcplink_unique_sys4_object"),
                    models.UniqueConstraint(
                        condition=models.Q(("kea_subnet_id__isnull", False)),
                        fields=("server", "family", "kea_subnet_id"),
                        name="keadhcplink_unique_subnet_identity",
                    ),
                ],
            },
        ),
    ]

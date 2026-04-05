# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0004_alter_server_options"),
    ]

    operations = [
        migrations.CreateModel(
            name="SyncConfig",
            fields=[
                ("interval_minutes", models.PositiveIntegerField(default=5)),
                ("sync_enabled", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "Sync Configuration",
                "app_label": "netbox_kea",
            },
        ),
    ]

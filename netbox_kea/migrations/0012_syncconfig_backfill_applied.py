# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Add backfill_applied flag to SyncConfig to prevent repeated PLUGINS_CONFIG backfill."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0011_persist_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="syncconfig",
            name="backfill_applied",
            field=models.BooleanField(
                default=False,
                help_text="Internal flag: True once the one-time PLUGINS_CONFIG backfill has been applied.",
            ),
        ),
    ]

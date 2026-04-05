# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Add JobsMixin to Server (enables RQ job assignment) and fix SyncConfig migration state."""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_kea", "0007_syncconfig_constraints"),
    ]

    operations = [
        # SyncConfig.id already exists in the DB (Django auto-created it from migration 0005).
        # We only need to update the migration state so Django no longer detects a discrepancy.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="syncconfig",
                    name="id",
                    field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False),
                ),
            ],
            database_operations=[],
        ),
        migrations.AlterField(
            model_name="syncconfig",
            name="interval_minutes",
            field=models.PositiveIntegerField(
                default=5,
                validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(1440)],
            ),
        ),
    ]

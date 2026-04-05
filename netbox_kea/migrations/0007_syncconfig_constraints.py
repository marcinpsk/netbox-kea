# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0006_server_sync_enabled"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="syncconfig",
            constraint=models.CheckConstraint(
                check=models.Q(interval_minutes__gte=1) & models.Q(interval_minutes__lte=1440),
                name="syncconfig_interval_minutes_range",
            ),
        ),
    ]

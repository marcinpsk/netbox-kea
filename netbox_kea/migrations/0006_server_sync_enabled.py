# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from django.db import migrations, models


# netbox_kea tables stay in main, so branch migrate fakes this migration (ADR 0007).
fake_on_branch = True


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0005_add_syncconfig"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="sync_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Include this server in the periodic Kea→NetBox IPAM sync job.",
                verbose_name="IPAM Sync Enabled",
            ),
        ),
    ]

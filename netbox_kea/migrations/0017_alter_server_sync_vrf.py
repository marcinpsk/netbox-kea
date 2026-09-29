# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Protect a VRF that a Server syncs into, so a VRF delete cannot null the Server row (ADR 0007).

No ipam dependency: 0010 already orders this app after the ipam migration that creates VRF.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0016_charfield_blank_not_null"),
    ]

    operations = [
        migrations.AlterField(
            model_name="server",
            name="sync_vrf",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ipam.vrf",
            ),
        ),
    ]

# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Add the IPAM Ownership link and the confirmation sequence (ADR 0006). Only the schema: no data."""

import django.db.models.deletion
from django.db import migrations, models

# netbox_kea tables stay in main, so branch migrate fakes this migration (ADR 0007).
fake_on_branch = True

# models.CONFIRMATION_SEQUENCE names it; test_ipam_ownership_link.py checks that the two agree.
SEQUENCE = "netbox_kea_ipam_ownership_confirmation"


class Migration(migrations.Migration):

    dependencies = [
        # The first ipam migration that has IPAddress, Prefix and IPRange in every supported NetBox release.
        ('ipam', '0047_squashed_0053'),
        ('netbox_kea', '0018_seed_syncconfig'),
    ]

    operations = [
        migrations.CreateModel(
            name='IPAMOwnershipLink',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('family', models.PositiveSmallIntegerField()),
                ('source', models.CharField(max_length=16)),
                ('facts', models.JSONField(blank=True, null=True)),
                ('confirmation', models.BigIntegerField()),
                ('stale_mark', models.BigIntegerField(blank=True, null=True)),
                ('ip_address', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='kea_ownership_links', to='ipam.ipaddress')),
                ('ip_range', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='kea_ownership_links', to='ipam.iprange')),
                ('prefix', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='kea_ownership_links', to='ipam.prefix')),
                ('server', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ipam_ownership_links', to='netbox_kea.server')),
            ],
            options={
                'verbose_name': 'IPAM ownership link',
                'constraints': [models.CheckConstraint(condition=models.Q(models.Q(('ip_address__isnull', False), ('ip_range__isnull', True), ('prefix__isnull', True)), models.Q(('ip_address__isnull', True), ('ip_range__isnull', True), ('prefix__isnull', False)), models.Q(('ip_address__isnull', True), ('ip_range__isnull', False), ('prefix__isnull', True)), _connector='OR'), name='ipamownershiplink_one_object'), models.CheckConstraint(condition=models.Q(('family__in', (4, 6))), name='ipamownershiplink_family'), models.CheckConstraint(condition=models.Q(('source__in', ['lease', 'reservation', 'subnet', 'pool', 'delegated-prefix'])), name='ipamownershiplink_source'), models.UniqueConstraint(condition=models.Q(('ip_address__isnull', False)), fields=('server', 'family', 'source', 'ip_address'), name='ipamownershiplink_unique_ip_address'), models.UniqueConstraint(condition=models.Q(('prefix__isnull', False)), fields=('server', 'family', 'source', 'prefix'), name='ipamownershiplink_unique_prefix'), models.UniqueConstraint(condition=models.Q(('ip_range__isnull', False)), fields=('server', 'family', 'source', 'ip_range'), name='ipamownershiplink_unique_ip_range')],
            },
        ),
        # CACHE 1: each nextval() call takes the next value, so numbers follow the order of the calls.
        migrations.RunSQL(
            f"CREATE SEQUENCE {SEQUENCE} AS bigint CACHE 1",
            f"DROP SEQUENCE {SEQUENCE}",
        ),
    ]

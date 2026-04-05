# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0008_add_server_jobsmixin"),
    ]

    operations = [
        migrations.RenameField(
            model_name="server",
            old_name="server_url",
            new_name="ca_url",
        ),
        migrations.RenameField(
            model_name="server",
            old_name="username",
            new_name="ca_username",
        ),
        migrations.RenameField(
            model_name="server",
            old_name="password",
            new_name="ca_password",
        ),
        migrations.AddField(
            model_name="server",
            name="dhcp4_username",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=255,
                verbose_name="DHCPv4 Username",
                help_text="Username for the DHCPv4 daemon. Overrides CA credentials for DHCPv4 connections.",
            ),
        ),
        migrations.AddField(
            model_name="server",
            name="dhcp4_password",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=255,
                verbose_name="DHCPv4 Password",
                help_text="Password for the DHCPv4 daemon. Overrides CA credentials for DHCPv4 connections.",
            ),
        ),
        migrations.AddField(
            model_name="server",
            name="dhcp6_username",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=255,
                verbose_name="DHCPv6 Username",
                help_text="Username for the DHCPv6 daemon. Overrides CA credentials for DHCPv6 connections.",
            ),
        ),
        migrations.AddField(
            model_name="server",
            name="dhcp6_password",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=255,
                verbose_name="DHCPv6 Password",
                help_text="Password for the DHCPv6 daemon. Overrides CA credentials for DHCPv6 connections.",
            ),
        ),
        migrations.AlterField(
            model_name="server",
            name="ca_url",
            field=models.CharField(
                verbose_name="CA / Server URL",
                max_length=255,
                help_text="Default endpoint URL (Kea Control Agent or single DHCP daemon).",
            ),
        ),
        migrations.AlterField(
            model_name="server",
            name="ca_username",
            field=models.CharField(
                null=True,
                blank=True,
                max_length=255,
                verbose_name="CA Username",
                help_text="Username for the Kea Control Agent (or default for all daemons).",
            ),
        ),
        migrations.AlterField(
            model_name="server",
            name="ca_password",
            field=models.CharField(
                null=True,
                blank=True,
                max_length=255,
                verbose_name="CA Password",
                help_text="Password for the Kea Control Agent (or default for all daemons).",
            ),
        ),
    ]

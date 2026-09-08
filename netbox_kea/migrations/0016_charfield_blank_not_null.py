# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Make the optional Server text columns NOT NULL with a blank default.

These columns were nullable, so an unset value could be either NULL or "". Every
reader already collapsed the two with `or None` or a truthiness test, so the
distinction carried no meaning and only gave each field two ways to be empty.

KeaDhcpLink.kea_identity keeps null=True on purpose and is not touched here: NULL
means "identified by subnet-id" and is a state distinct from any string value.

Existing NULLs need no data migration: AlterField carries default="", so the schema
editor backfills them before applying NOT NULL. test_0016_replaces_null_credentials_
with_blanks writes real NULLs and migrates forward to hold that.
"""

from django.db import migrations, models

#: The Server columns that move from NULL-or-blank to blank only. Named here so the
#: test that writes real NULLs and migrates forward cannot drift from this list.
OPTIONAL_TEXT_FIELDS = (
    "ca_username",
    "ca_password",
    "dhcp4_username",
    "dhcp4_password",
    "dhcp6_username",
    "dhcp6_password",
    "client_cert_path",
    "client_key_path",
    "ca_file_path",
    "dhcp4_url",
    "dhcp6_url",
)



class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0015_keadhcplink_one_identity_kind"),
    ]

    operations = [
        migrations.AlterField(
            model_name="server",
            name="ca_username",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="server",
            name="ca_password",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="server",
            name="dhcp4_username",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="server",
            name="dhcp4_password",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="server",
            name="dhcp6_username",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="server",
            name="dhcp6_password",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="server",
            name="client_cert_path",
            field=models.CharField(blank=True, default="", max_length=4096),
        ),
        migrations.AlterField(
            model_name="server",
            name="client_key_path",
            field=models.CharField(blank=True, default="", max_length=4096),
        ),
        migrations.AlterField(
            model_name="server",
            name="ca_file_path",
            field=models.CharField(blank=True, default="", max_length=4096),
        ),
        migrations.AlterField(
            model_name="server",
            name="dhcp4_url",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="server",
            name="dhcp6_url",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]

# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Create the SyncConfig singleton and apply the one-time PLUGINS_CONFIG backfill, so a GET only reads it.

A missing row gets its values from PLUGINS_CONFIG. A row whose backfill has not run gets each type
toggle that PLUGINS_CONFIG disables set to False. A row whose backfill has run keeps its UI values.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import migrations


# netbox_kea tables stay in main, so branch migrate fakes this migration (ADR 0007).
fake_on_branch = True

TYPE_TOGGLES = ("sync_leases_enabled", "sync_reservations_enabled", "sync_prefixes_enabled", "sync_ip_ranges_enabled")


def configured_toggles() -> dict:
    """Return the type toggles that PLUGINS_CONFIG sets."""
    config = settings.PLUGINS_CONFIG.get("netbox_kea", {})
    return {toggle: config.get(toggle, True) for toggle in TYPE_TOGGLES}


def configured_values() -> dict:
    """Return the SyncConfig field values that PLUGINS_CONFIG sets for a new row."""
    config = settings.PLUGINS_CONFIG.get("netbox_kea", {})
    interval = config.get("sync_interval_minutes", 5)
    # The range is the 0007 constraint; bool is an int, and Django would truncate a float or coerce a string.
    if type(interval) is not int or not 1 <= interval <= 1440:
        raise ImproperlyConfigured(
            f"PLUGINS_CONFIG['netbox_kea']['sync_interval_minutes'] must be an integer from 1 to 1440, got {interval!r}."
        )
    return {"interval_minutes": interval, "sync_enabled": config.get("sync_enabled", True), **configured_toggles()}


def seed_sync_config(apps, schema_editor):
    rows = apps.get_model("netbox_kea", "SyncConfig").objects.using(schema_editor.connection.alias)
    row = rows.filter(pk=1).first()
    if row is None:
        rows.create(pk=1, **configured_values())
    elif not row.backfill_applied:
        disabled = {toggle: False for toggle, enabled in configured_toggles().items() if not enabled}
        rows.filter(pk=1).update(backfill_applied=True, **disabled)


def mark_backfill_applied(apps, schema_editor):
    rows = apps.get_model("netbox_kea", "SyncConfig").objects.using(schema_editor.connection.alias)
    rows.update(backfill_applied=True)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_kea", "0017_alter_server_sync_vrf"),
    ]

    operations = [
        migrations.RunPython(seed_sync_config, mark_backfill_applied),
        migrations.RemoveField(model_name="syncconfig", name="backfill_applied"),
    ]

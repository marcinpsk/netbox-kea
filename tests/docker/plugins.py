# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-FileCopyrightText: 2023 Devon Mar <devon-mar@users.noreply.github.com>
# SPDX-License-Identifier: Apache-2.0
PLUGINS = ["netbox_kea"]

# Pause the periodic Kea->NetBox IPAM sync for the browser suite. It is on by default
# and this harness runs an rqworker, so it writes the same IPAddress rows the fixtures
# create and delete. The on-demand Sync button is unaffected.
# Read only when the migration creates the SyncConfig row, so a reused postgres volume keeps
# the stored value; test_the_harness_pauses_the_periodic_ipam_sync reports that.
PLUGINS_CONFIG = {"netbox_kea": {"sync_enabled": False}}

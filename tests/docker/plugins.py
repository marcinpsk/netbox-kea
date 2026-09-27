PLUGINS = ["netbox_kea"]

# Pause the periodic Kea->NetBox IPAM sync for the browser suite. It is on by default
# and this harness runs an rqworker, so it writes the same IPAddress rows the fixtures
# create and delete. The on-demand Sync button is unaffected.
# Read only when SyncConfig creates its singleton, so a reused postgres volume keeps the
# stored value; test_the_harness_pauses_the_periodic_ipam_sync reports that.
PLUGINS_CONFIG = {"netbox_kea": {"sync_enabled": False}}

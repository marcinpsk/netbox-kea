# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# The netbox-branching variant (docker-compose.branching.yml) loads this file after plugins.py.
# PLUGINS_CONFIG, with the periodic sync kill-switch, still comes from plugins.py.
from netbox.configuration.configuration import DATABASES as _DATABASES
from netbox_branching.utilities import DynamicSchemaDict

# netbox-branching must be the last plugin, and it needs its schema dict and its router.
PLUGINS = ["netbox_kea", "netbox_branching"]
DATABASES = DynamicSchemaDict(_DATABASES)
DATABASE_ROUTERS = ["netbox_branching.database.BranchAwareRouter"]

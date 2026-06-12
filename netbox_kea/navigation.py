from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

_items = (
    PluginMenuItem(
        link="plugins:netbox_kea:combined",
        link_text="Combined View",
        permissions=["netbox_kea.view_server"],
    ),
    PluginMenuItem(
        link="plugins:netbox_kea:server_list",
        link_text="Servers",
        permissions=["netbox_kea.view_server"],
        buttons=(
            PluginMenuButton(
                link="plugins:netbox_kea:server_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
                permissions=["netbox_kea.add_server"],
            ),
        ),
    ),
    PluginMenuItem(
        link="plugins:netbox_kea:sync_jobs",
        link_text="Sync Jobs",
        permissions=["netbox_kea.view_server"],
    ),
)

# Top-level menu: surfaces a dedicated "DHCP Kea" section in the NetBox sidebar
# instead of nesting under the generic "Plugins" group. NetBox renders a
# ``menu`` (PluginMenu) in preference to ``menu_items`` when both are present.
menu = PluginMenu(
    label="DHCP Kea",
    icon_class="mdi mdi-server-network",
    groups=(("Management", _items),),
)

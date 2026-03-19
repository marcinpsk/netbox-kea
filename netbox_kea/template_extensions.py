# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""NetBox plugin template extensions for netbox-kea-ng.

Injects a Kea panel onto the NetBox IPAddress detail page, providing
quick links to create host reservations on configured Kea servers.
"""

from urllib.parse import urlencode

from django.urls import reverse
from netbox.plugins import PluginTemplateExtension

from .models import Server


class IPAddressKeaPanel(PluginTemplateExtension):
    """Renders a 'Kea Reservations' panel on the IPAddress detail page.

    Appears on the right side of the page, listing all Kea servers compatible
    with the IP's address family with pre-filled 'Create Reservation' links.
    """

    models = ["ipam.ipaddress"]

    def right_page(self):
        nb_ip = self.context.get("object")
        if nb_ip is None:
            return ""

        ip_str = str(nb_ip.address.ip)
        is_v6 = ":" in ip_str
        version = 6 if is_v6 else 4

        if version == 4:
            servers = Server.objects.filter(dhcp4=True)
            add_url_name = "plugins:netbox_kea:server_reservation4_add"
        else:
            servers = Server.objects.filter(dhcp6=True)
            add_url_name = "plugins:netbox_kea:server_reservation6_add"

        server_links = []
        for server in servers:
            base_url = reverse(add_url_name, args=[server.pk])
            params = urlencode({
                "ip_address": ip_str,
                "hostname": nb_ip.dns_name or "",
            })
            server_links.append({
                "server": server,
                "url": f"{base_url}?{params}",
            })

        return self.render(
            "netbox_kea/inc/ip_kea_panel.html",
            extra_context={
                "server_links": server_links,
                "version": version,
                "kea_page_url": reverse(
                    "plugins:netbox_kea:ipaddress_kea_reservations",
                    args=[nb_ip.pk],
                ),
            },
        )

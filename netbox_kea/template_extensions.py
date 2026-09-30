# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""NetBox plugin template extensions for netbox-kea-ng.

Injects a Kea panel onto the NetBox IPAddress detail page, providing
quick links to create host reservations on configured Kea servers, and
a banner on every plugin page while a netbox-branching branch is active.
"""

from urllib.parse import urlencode

from django.urls import reverse
from netbox.plugins import PluginTemplateExtension

from . import branching
from .models import Server


class BranchBanner(PluginTemplateExtension):
    """Says, on each plugin page in a branch, where the page's data comes from and that changes are refused."""

    def navbar(self):  # noqa: D102
        branch = branching.active_branch()
        match = getattr(self.context["request"], "resolver_match", None)
        if branch is None or match is None or not branching.plugin_owned(match.func):
            return ""
        return self.render("netbox_kea/inc/branch_banner.html", extra_context={"branch": branch})


class IPAddressKeaPanel(PluginTemplateExtension):
    """Renders a 'Kea Reservations' panel on the IPAddress detail page.

    Appears on the right side of the page, listing all Kea servers compatible
    with the IP's address family with pre-filled 'Create Reservation' links.
    """

    model = "ipam.ipaddress"  # NetBox <= 4.0
    models = ["ipam.ipaddress"]  # NetBox >= 4.1

    def right_page(self):  # noqa: D102
        nb_ip = self.context.get("object")
        if nb_ip is None:
            return ""
        if not nb_ip.address or not nb_ip.address.ip:
            return ""

        ip_str = str(nb_ip.address.ip)
        is_v6 = ":" in ip_str
        version = 6 if is_v6 else 4

        request = self.context["request"]
        if version == 4:
            servers = Server.objects.restrict(request.user, "view").filter(dhcp4=True)
            add_url_name = "plugins:netbox_kea:server_reservation4_add"
        else:
            servers = Server.objects.restrict(request.user, "view").filter(dhcp6=True)
            add_url_name = "plugins:netbox_kea:server_reservation6_add"

        # Server rows exist in main only, and a reservation add is refused in a branch.
        in_branch = branching.active_branch() is not None
        server_links = []
        for server in servers:
            url = None
            if not in_branch:
                ip_param = "ip_addresses" if version == 6 else "ip_address"
                params = urlencode({ip_param: ip_str, "hostname": nb_ip.dns_name or ""})
                url = f"{reverse(add_url_name, args=[server.pk])}?{params}"
            server_links.append({"server": server, "url": url})

        return self.render(
            "netbox_kea/inc/ip_kea_panel.html",
            extra_context={
                "server_links": server_links,
                "in_branch": in_branch,
                "version": version,
                "kea_page_url": reverse(
                    "plugins:netbox_kea:ipaddress_kea_reservations",
                    args=[nb_ip.pk],
                ),
            },
        )

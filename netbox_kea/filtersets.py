from netbox.filtersets import NetBoxModelFilterSet

from .models import Server


class ServerFilterSet(NetBoxModelFilterSet):
    """FilterSet for querying Server objects by name, URL, and DHCP version flags."""

    class Meta:
        model = Server
        fields = ("id", "name", "server_url", "dhcp4", "dhcp6")

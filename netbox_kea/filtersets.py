from django_filters import CharFilter
from netbox.filtersets import NetBoxModelFilterSet

from .models import Server


class ServerFilterSet(NetBoxModelFilterSet):
    """FilterSet for querying Server objects by name, URL, DHCP version flags, and control agent."""

    name = CharFilter(lookup_expr="icontains", label="Name contains")
    server_url = CharFilter(lookup_expr="icontains", label="Server URL contains")

    class Meta:
        model = Server
        fields = ("id", "name", "server_url", "dhcp4", "dhcp6", "has_control_agent")

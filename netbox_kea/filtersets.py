from django_filters import CharFilter
from netbox.filtersets import NetBoxModelFilterSet

from .models import Server


class ServerFilterSet(NetBoxModelFilterSet):
    """FilterSet for querying Server objects by name, URL, DHCP version flags, and control agent."""

    name = CharFilter(lookup_expr="icontains", label="Name contains")
    ca_url = CharFilter(lookup_expr="icontains", label="CA / Server URL contains")

    class Meta:
        model = Server
        fields = ("id", "name", "ca_url", "dhcp4", "dhcp6", "has_control_agent")

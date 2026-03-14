from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import Server


class ServerSerializer(NetBoxModelSerializer):
    """DRF serializer for the Server model."""

    url = serializers.HyperlinkedIdentityField(view_name="plugins-api:netbox_kea-api:server-detail")

    class Meta:
        model = Server
        fields = (
            "id",
            "name",
            "server_url",
            "username",
            "password",
            "ssl_verify",
            "client_cert_path",
            "client_key_path",
            "ca_file_path",
            "dhcp6",
            "dhcp4",
            "dhcp4_url",
            "dhcp6_url",
            "has_control_agent",
            "url",
            "display",
            "tags",
            "last_updated",
        )
        brief_fields = ("id", "url", "name", "server_url")
        extra_kwargs = {"password": {"write_only": True}}

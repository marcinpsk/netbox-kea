from django.http import QueryDict
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import Server

#: Columns migration 0016 moved from NULL-or-blank to blank only. Every released
#: version answered GET with null for these when unset.
_OPTIONAL_TEXT_FIELDS = frozenset(
    {
        "ca_username",
        "ca_password",
        "dhcp4_username",
        "dhcp4_password",
        "dhcp6_username",
        "dhcp6_password",
        "client_cert_path",
        "client_key_path",
        "ca_file_path",
        "dhcp4_url",
        "dhcp6_url",
    }
)


class ServerSerializer(NetBoxModelSerializer):
    """DRF serializer for the Server model."""

    url = serializers.HyperlinkedIdentityField(view_name="plugins-api:netbox_kea-api:server-detail")

    def to_internal_value(self, data):
        """Read an explicit null on an optional text field as "".

        A client that reads a Server, edits one field and writes the object back still
        sends the nulls a pre-0016 GET gave it. Those columns are NOT NULL now, so
        without this the round trip fails on fields the client never meant to change.
        """
        # QueryDict is a dict subclass, but rebuilding it as a plain dict loses the
        # multi-value access DRF uses to parse form-encoded lists such as tags. A form
        # body cannot carry a real null anyway, so only a JSON mapping needs coercing.
        if isinstance(data, dict) and not isinstance(data, QueryDict):
            data = {key: "" if value is None and key in _OPTIONAL_TEXT_FIELDS else value for key, value in data.items()}
        return super().to_internal_value(data)

    class Meta:
        model = Server
        fields = (
            "id",
            "name",
            "ca_url",
            "ca_username",
            "ca_password",
            "dhcp4_username",
            "dhcp4_password",
            "dhcp6_username",
            "dhcp6_password",
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
        brief_fields = ("id", "url", "name", "ca_url")
        extra_kwargs = {
            "ca_password": {"write_only": True},
            "dhcp4_password": {"write_only": True},
            "dhcp6_password": {"write_only": True},
        }

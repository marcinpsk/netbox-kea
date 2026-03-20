import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType

from . import models


@strawberry_django.type(
    models.Server,
    fields=(
        "id",
        "name",
        "server_url",
        "username",
        "ssl_verify",
        "client_cert_path",
        "client_key_path",
        "ca_file_path",
        "dhcp6",
        "dhcp4",
        "dhcp4_url",
        "dhcp6_url",
        "has_control_agent",
    ),
)
class ServerType(NetBoxObjectType):
    """GraphQL type for the Server model."""

    pass


@strawberry.type
class Query:
    """GraphQL root query type exposing Kea server objects."""

    @strawberry.field
    def server(self, id: int) -> ServerType:
        """Return a single Server by primary key."""
        return models.Server.objects.get(pk=id)

    server_list: list[ServerType] = strawberry_django.field()


schema = [Query]

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType

from . import models

if TYPE_CHECKING:
    # strawberry.lazy resolves the type at runtime; this is for the linters.
    from ipam.graphql.types import VRFType


@strawberry_django.type(
    models.Server,
    fields=(
        "id",
        "name",
        "ca_url",
        "ca_username",
        "dhcp4_username",
        "dhcp6_username",
        "ssl_verify",
        "client_cert_path",
        "client_key_path",
        "ca_file_path",
        "dhcp6",
        "dhcp4",
        "dhcp4_url",
        "dhcp6_url",
        "has_control_agent",
        "sync_enabled",
        "sync_leases_enabled",
        "sync_reservations_enabled",
        "sync_prefixes_enabled",
        "sync_ip_ranges_enabled",
        "sync_dhcp_plugin_enabled",
        "sync_vrf",
        "persist_config",
    ),
)
class ServerType(NetBoxObjectType):
    """GraphQL type for the Server model."""

    # Without the annotation strawberry_django resolves the FK to a placeholder
    # DjangoModelType that exposes no fields at all.
    sync_vrf: Annotated["VRFType", strawberry.lazy("ipam.graphql.types")] | None


@strawberry.type
class Query:
    """GraphQL root query type exposing Kea server objects."""

    @strawberry.field
    def server(self, id: int, info: strawberry.types.Info) -> ServerType:  # noqa: A002 - published GraphQL argument name
        """Return a single Server by primary key."""
        return models.Server.objects.restrict(info.context.request.user, "view").get(pk=id)

    server_list: list[ServerType] = strawberry_django.field()


schema = [Query]

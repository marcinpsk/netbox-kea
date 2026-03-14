from netbox.api.viewsets import NetBoxModelViewSet

from .. import filtersets, models
from .serializers import ServerSerializer


class ServerViewSet(NetBoxModelViewSet):
    """DRF viewset providing CRUD endpoints for Server objects."""

    queryset = models.Server.objects.prefetch_related("tags").order_by("-pk")
    filterset_class = filtersets.ServerFilterSet
    serializer_class = ServerSerializer

# SPDX-FileCopyrightText: 2023 Devon Mar <devon-mar@users.noreply.github.com>
# SPDX-License-Identifier: Apache-2.0
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_kea"

router = NetBoxRouter()
router.register("servers", views.ServerViewSet)

urlpatterns = router.urls

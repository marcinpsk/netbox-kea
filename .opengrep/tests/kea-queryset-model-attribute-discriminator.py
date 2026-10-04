# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
from django.db import models


def delete_database(obj):
    # ruleid: kea-queryset-model-attribute-discriminator
    if hasattr(obj, "model"):
        return obj._db

    # ruleid: kea-queryset-model-attribute-discriminator
    model = getattr(obj, "model", type(obj))

    # ok: kea-queryset-model-attribute-discriminator
    if isinstance(obj, models.QuerySet):
        return obj._db

    # ok: kea-queryset-model-attribute-discriminator
    model = obj.model if isinstance(obj, models.QuerySet) else type(obj)

    # ok: kea-queryset-model-attribute-discriminator
    if hasattr(obj, "related_manager_cls"):
        return obj.related_manager_cls
    return obj._state.db, model

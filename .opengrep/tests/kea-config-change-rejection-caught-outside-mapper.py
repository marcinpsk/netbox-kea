# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-config-change-rejection-caught-outside-mapper. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
from netbox_kea import config_write
from netbox_kea.config_write import ConfigChangeRejected


def bad_view_catches_the_rejection(request, server):
    try:
        config_write.delete_shared_network(server, 4, "net")
    # ruleid: kea-config-change-rejection-caught-outside-mapper
    except ConfigChangeRejected as rejection:
        messages.error(request, rejection.reason)


def bad_view_catches_the_qualified_rejection(request, server):
    try:
        config_write.delete_shared_network(server, 4, "net")
    # ruleid: kea-config-change-rejection-caught-outside-mapper
    except config_write.ConfigChangeRejected:
        messages.error(request, "Kea rejected the change.")


def bad_view_catches_the_rejection_in_a_tuple(request, server):
    try:
        config_write.delete_shared_network(server, 4, "net")
    # ruleid: kea-config-change-rejection-caught-outside-mapper
    except (ValueError, ConfigChangeRejected):
        messages.error(request, "Kea rejected the change.")


def bad_view_names_the_rejection_in_a_tuple(request, server):
    try:
        config_write.delete_shared_network(server, 4, "net")
    # ruleid: kea-config-change-rejection-caught-outside-mapper
    except (ValueError, config_write.ConfigChangeRejected) as exc:
        messages.error(request, type(exc).__name__)


def _run_config_change(request, confirmed, change):
    try:
        outcome = change()
    # ok: kea-config-change-rejection-caught-outside-mapper
    except ConfigChangeRejected as rejection:
        messages.error(request, rejection.reason)
        return None
    return outcome


def ok_view_runs_the_change_through_the_mapper(request, server):
    # ok: kea-config-change-rejection-caught-outside-mapper
    return _run_config_change(request, "Deleted.", lambda: config_write.delete_shared_network(server, 4, "net"))


def ok_view_catches_a_form_error(request, form):
    try:
        form.full_clean()
    # ok: kea-config-change-rejection-caught-outside-mapper
    except ValueError:
        messages.error(request, "The form is not valid.")

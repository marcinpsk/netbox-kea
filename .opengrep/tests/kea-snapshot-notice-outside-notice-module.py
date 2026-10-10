# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-snapshot-notice-outside-notice-module. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
from django.contrib import messages

from netbox_kea import server_configuration
from netbox_kea.reservation_transfer import parse_reservation_document
from netbox_kea.views.notices import notice, show_notices


def bad_view_picks_the_level_of_a_snapshot(request, snapshot):
    # ruleid: kea-snapshot-notice-outside-notice-module
    _diagnostic_messages(request, snapshot.diagnostics, messages.ERROR if snapshot.unavailable else messages.WARNING)


def bad_view_adds_a_message_at_a_snapshot_level(request, text):
    # ruleid: kea-snapshot-notice-outside-notice-module
    messages.add_message(request, messages.WARNING, text)


def bad_combined_page_splits_the_lists_by_hand(server, snapshot, errors, warnings):
    diagnostics = errors if snapshot.unavailable else warnings
    # ruleid: kea-snapshot-notice-outside-notice-module
    diagnostics.extend((server.name, item.message) for item in snapshot.diagnostics)


def bad_partial_formats_the_lease_records(snapshot):
    # ruleid: kea-snapshot-notice-outside-notice-module
    return {"lease_diagnostics": [item.message for item in snapshot.diagnostics]}


def good_view_shows_the_notice(request, server):
    configuration = server_configuration.for_verification(server, 4)
    # ok: kea-snapshot-notice-outside-notice-module
    show_notices(request, notice(configuration))
    # ok: kea-snapshot-notice-outside-notice-module
    messages.error(request, "Shared network 'net' not found or could not be retrieved.")


def good_view_reads_another_attribute(observed):
    # ok: kea-snapshot-notice-outside-notice-module
    return not observed.subnet_diagnostics


# ruleid: kea-config-change-mapper-outside-base
def _run_config_change(request, change):
    try:
        outcome = change()
    except ConfigChangeRejected as rejection:
        # ok: kea-snapshot-notice-outside-notice-module
        messages.error(request, " ".join(rejection.diagnostics))
        return None
    return outcome


def _read_reservations(client, version, catalogue, diagnostics):
    page = client.reservation_page(version, catalogue)
    # ok: kea-snapshot-notice-outside-notice-module
    diagnostics.extend(page.diagnostics)
    return page


def _incomplete_export_message(snapshot):
    # ok: kea-snapshot-notice-outside-notice-module
    return f"Export refused: {len(snapshot.diagnostics)} record(s) could not be read."


def good_import_lists_the_document_diagnostics(form):
    try:
        parsed = parse_reservation_document(form.cleaned_data["document"], "yaml", expected_family=4)
    except ReservationTransferError:
        return None
    # ok: kea-snapshot-notice-outside-notice-module
    return [*parsed.diagnostics]

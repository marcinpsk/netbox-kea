# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-exception-handler-misses-runtime-error. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
import requests

from netbox_kea.kea import KeaException


def bad_toggle_misses_runtime_error(request, client):
    # ruleid: kea-exception-handler-misses-runtime-error
    try:
        client.command("dhcp-enable", 4)
    except KeaException as exc:
        messages.error(request, kea_error_hint(exc))
    except (requests.RequestException, ValueError):
        messages.error(request, "An internal error occurred.")


def bad_badge_catches_kea_exception_in_a_tuple(client):
    # ruleid: kea-exception-handler-misses-runtime-error
    try:
        client.command("version-get", 4)
        online = True
    except (KeaException, requests.RequestException, OSError, ValueError):
        online = False
    return online


def bad_kea_exception_after_another_clause(client):
    # ruleid: kea-exception-handler-misses-runtime-error
    try:
        client.command("lease4-wipe", 4)
    except ValueError:
        logger.exception("Parse error")
    except KeaException:
        logger.exception("Kea error")


def ok_sibling_clause_catches_runtime_error(request, client):
    # ok: kea-exception-handler-misses-runtime-error
    try:
        client.command("dhcp-enable", 4)
    except KeaException as exc:
        messages.error(request, kea_error_hint(exc))
    except (requests.RequestException, RuntimeError, ValueError):
        messages.error(request, "An internal error occurred.")


def ok_runtime_error_clause_comes_first(client):
    # ok: kea-exception-handler-misses-runtime-error
    try:
        client.command("version-get", 4)
    except RuntimeError as exc:
        raise ValidationError("An internal error occurred.") from exc
    except KeaException:
        logger.exception("Kea error")


def ok_sibling_clause_catches_exception(client):
    # ok: kea-exception-handler-misses-runtime-error
    try:
        client.command("lease4-add", 4)
    except KeaException:
        logger.exception("Kea error")
    except Exception:
        logger.exception("Unexpected error")


def ok_bare_except(client):
    # ok: kea-exception-handler-misses-runtime-error
    try:
        client.command("lease4-add", 4)
    except KeaException:
        logger.exception("Kea error")
    except:
        logger.exception("Unexpected error")


def ok_no_kea_exception_clause(client):
    # ok: kea-exception-handler-misses-runtime-error
    try:
        client.command("lease4-add", 4)
    except requests.RequestException:
        logger.exception("Network error")

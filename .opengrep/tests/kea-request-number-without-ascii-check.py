# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-request-number-without-ascii-check. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).
import re

from netbox_kea.decimal_text import parse_decimal


def bad_digit_filter(request):
    # ruleid: kea-request-number-without-ascii-check
    return {int(pk) for pk in request.GET.getlist("server") if pk.isdigit()}


def bad_decimal_check(text):
    # ruleid: kea-request-number-without-ascii-check
    return text.isdecimal()


def bad_regex(text):
    # ruleid: kea-request-number-without-ascii-check
    return re.fullmatch(r"\d+", text)


def bad_direct_parameter(params):
    # ruleid: kea-request-number-without-ascii-check
    return int(params.get("subnet_id", ""))


def bad_bound_parameter(request):
    params = request.query_params
    raw_limit = params.get("limit", "")
    # ruleid: kea-request-number-without-ascii-check
    return int(raw_limit) if raw_limit else 100


def bad_cleaned_data(self):
    cleaned_data = super().clean()
    page = cleaned_data["page"]
    # ruleid: kea-request-number-without-ascii-check
    return int(page)


def bad_request_get(request):
    # ruleid: kea-request-number-without-ascii-check
    return int(request.GET["page"])


def good_parse_decimal(params):
    # ok: kea-request-number-without-ascii-check
    return parse_decimal(params.get("subnet_id", ""))


def good_ascii_regex(text):
    # ok: kea-request-number-without-ascii-check
    return re.fullmatch(r"[0-9]+", text)


def good_kea_reply(subnet):
    sid = subnet.get("id")
    # ok: kea-request-number-without-ascii-check
    return int(sid)


def good_address(address):
    # ok: kea-request-number-without-ascii-check
    return int(address)

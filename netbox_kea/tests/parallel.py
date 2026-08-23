# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Isolation helpers for parallel pytest workers."""

import re

MAX_PARALLEL_WORKERS = 8
_POSTGRES_NAME_LIMIT = 63
_WORKER_ID_PATTERN = re.compile(r"gw(?P<number>\d+)")


def isolated_test_database_name(base_name: str, worker_id: str | None) -> str:
    """Return a PostgreSQL-safe database name for one pytest worker."""
    suffix = f"_{worker_id}" if worker_id else ""
    available_bytes = _POSTGRES_NAME_LIMIT - len(suffix.encode())
    base_prefix = base_name.encode()[:available_bytes].decode(errors="ignore")
    return f"{base_prefix}{suffix}"


def isolated_redis_databases(worker_id: str | None) -> tuple[int, int]:
    """Return private task and cache Redis databases for one pytest worker."""
    if worker_id is None:
        raise ValueError("Redis database isolation requires an xdist worker ID.")

    match = _WORKER_ID_PATTERN.fullmatch(worker_id)
    if match is None:
        raise ValueError(f"Unsupported pytest worker ID: {worker_id!r}.")

    worker_number = int(match.group("number"))
    if worker_number >= MAX_PARALLEL_WORKERS:
        raise ValueError(f"At most {MAX_PARALLEL_WORKERS} pytest workers are supported.")

    return worker_number, MAX_PARALLEL_WORKERS + worker_number

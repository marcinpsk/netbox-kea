# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The downloads of scripts/record_kea_config_get.py end when a server stops sending."""

import importlib.util
import select
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "record_kea_config_get.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("record_kea_config_get", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stalled_url() -> Iterator[str]:
    """A local HTTP server that accepts each connection and never answers."""
    server = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            if select.select([server], [], [], 0.1)[0]:
                held.append(server.accept()[0])

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}"
    stop.set()
    thread.join(timeout=5)
    for connection in held:
        connection.close()
    server.close()


def _stalls_until_the_deadline(download) -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.CalledProcessError) as raised:
        download()
    assert raised.value.returncode == 28  # curl: operation timed out
    assert time.monotonic() - started < 10


def test_a_package_download_ends_when_the_server_stops_sending(script, stalled_url, tmp_path):
    with patch.object(script, "DOWNLOAD_SECONDS", 1):
        _stalls_until_the_deadline(lambda: script._curl(f"{stalled_url}/APKINDEX.tar.gz", tmp_path / "index"))


def test_a_keyword_table_download_ends_when_the_server_stops_sending(script, stalled_url):
    with (
        patch.object(script, "DOWNLOAD_SECONDS", 1),
        patch.object(script, "KEYWORD_TABLES", stalled_url + "/{version}/simple_parser{family}.cc"),
    ):
        _stalls_until_the_deadline(lambda: script._accepted_keys(4, "3.2.0"))

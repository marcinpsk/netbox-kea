# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Shared test utilities for netbox_kea view tests.

Import from this module instead of duplicating scaffold code in each test file.
"""

import re
from typing import TYPE_CHECKING

import requests
from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from netbox_kea.models import Server

from .kea_stub import stub_kea

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

# Minimal PLUGINS_CONFIG for tests that do not exercise the Subnet lease-query guard.
_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30, "lease_query_max_unpaged_leases": 0}}

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_INT_PK_RE = re.compile(r"/servers/(\d+)/")


def _make_db_server(**kwargs) -> Server:
    """Create and persist a Server without live connectivity checks.

    ``Server.objects.create()`` skips ``Model.clean()``, so no Kea connectivity
    check is triggered.  The ``PLUGINS_CONFIG`` override is applied by the calling
    test class.
    """
    defaults = {
        "name": "test-kea",
        "ca_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE")


def _refusal_receivers(signal) -> set[str]:
    """Return the label of each model whose branch refusal receiver *signal* has connected."""
    prefix = "netbox_kea.refuse_in_branch."
    return {key[0].removeprefix(prefix) for key, *_rest in signal.receivers if str(key[0]).startswith(prefix)}


def _sync_page_urls(server: Server) -> tuple[str, str]:
    """Return the two sync pages that read SyncConfig: the Sync Jobs page and the Server's Sync tab."""
    return (
        reverse("plugins:netbox_kea:sync_jobs"),
        reverse("plugins:netbox_kea:server_sync_status", args=[server.pk]),
    )


def _get_with_writes(client: Client, url: str) -> tuple["_MonkeyPatchedWSGIResponse", list[str]]:
    """GET ``url`` and return the response with each INSERT, UPDATE or DELETE statement it ran."""
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    writes = [
        query["sql"] for query in captured.captured_queries if query["sql"].lstrip().upper().startswith(_WRITE_VERBS)
    ]
    return response, writes


def _page_data(response) -> dict[str, str]:
    """Return the data that a browser posts for the form of *response*: each field that the page rendered."""
    form, page = response.context["form"], response.content.decode()
    return {
        name: "" if form[name].value() is None else str(form[name].value())
        for name in form.fields
        if f'name="{form[name].html_name}"' in page
    }


# ─────────────────────────────────────────────────────────────────────────────
# Shared base class
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _ViewTestBase(TestCase):
    """Creates a superuser and a single Server for use in all view tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="kea_testuser",
            email="kea_test@example.com",
            password="kea_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_db_server()

    def _fresh_client(self) -> None:
        """Log in with a new test client, so no message of an earlier request is still stored."""
        self.client = Client()
        self.client.force_login(self.user)

    def _assert_no_none_pk_redirect(self, response):
        """Assert that a redirect URL never contains the string ``None`` as a pk."""
        if hasattr(response, "url"):
            self.assertNotIn(
                "servers/None",
                response.url,
                f"Redirect went to bad URL: {response.url}",
            )

    def _assert_redirect_to_integer_pk(self, response):
        """Assert that a redirect URL contains an integer server pk."""
        self._assert_no_none_pk_redirect(response)
        self.assertIsNotNone(
            _INT_PK_RE.search(response.url),
            f"Expected /servers/<int>/ in redirect URL, got: {response.url}",
        )

    def _make_request(self):
        """Return a RequestFactory request pre-loaded with a FallbackStorage message backend."""
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        request.session = "session"
        storage = FallbackStorage(request)
        request._messages = storage
        return request

    @staticmethod
    def _call_version(call_args):
        """Extract the 'version' argument from a mock call_args (kwargs or positional)."""
        kwargs = call_args.kwargs or call_args[1]
        args = call_args.args or call_args[0]
        return kwargs.get("version") or (args[0] if args else None)


_UNCONFIRMED = "Kea did not confirm the change. Check the server configuration before retrying."


def _read_modify_write_cases(confirmed: str) -> tuple:
    """Each outcome and rejection of a read-modify-write view: (label, Kea replies, Server fields, the message)."""
    lost = requests.ReadTimeout("read timed out")
    restart = "It is live, but it may not survive a Kea restart, because Kea did not save it to disk."
    return (
        ("applied, persisted", {}, {}, (django_messages.SUCCESS, confirmed)),
        ("applied, not requested", {}, {"persist_config": False}, (django_messages.SUCCESS, confirmed)),
        (
            "applied, failed",
            {"config-write": {"result": 1, "text": "Unable to open file"}},
            {},
            (django_messages.WARNING, f"{confirmed} {restart} config-write failed: Unable to open file"),
        ),
        (
            "unknown, persisted",
            {"config-set": lost},
            {},
            (django_messages.WARNING, f"{_UNCONFIRMED} Kea's reply to the change was lost or unreadable."),
        ),
        (
            "unknown, failed",
            {"config-set": lost, "config-write": lost},
            {},
            (
                django_messages.WARNING,
                (
                    f"{_UNCONFIRMED} Kea also could not save its running configuration to disk. "
                    "Kea's reply to the change was lost or unreadable. The reply to config-write was lost or unreadable."
                ),
            ),
        ),
        (
            "unknown, not requested",
            {"config-set": {"result": 1, "text": "hook initialization failed"}},
            {"persist_config": False},
            (django_messages.WARNING, f"{_UNCONFIRMED} Kea replied: hook initialization failed"),
        ),
        (
            "config-test rejected",
            {"config-test": {"result": 1, "text": "subnet overlaps"}},
            {},
            (
                django_messages.ERROR,
                "Kea's config-test rejected the change, so it was not applied. Kea replied: subnet overlaps",
            ),
        ),
        (
            "not sent",
            {"config-test": requests.ConnectionError("https://kea-internal.example.invalid refused")},
            {},
            (
                django_messages.ERROR,
                "The change was not sent to Kea. Kea did not return a usable reply to config-test.",
            ),
        ),
        (
            "invalid client configuration",
            {},
            {"client_cert_path": "/etc/kea/client.pem"},
            (
                django_messages.ERROR,
                (
                    "The change was not sent to Kea, because the Server settings are not valid. "
                    "NetBox could not build a Kea client from the Server connection settings."
                ),
            ),
        ),
    )


class _ReadModifyWriteMessages(_ViewTestBase):
    """One HTTP POST per Configuration Change outcome and per rejection, for a read-modify-write view."""

    def _assert_one_message_per_case(self, url: str, data: dict, responses: dict, confirmed: str) -> None:
        original = {"persist_config": self.server.persist_config, "client_cert_path": self.server.client_cert_path}
        ok = {"result": 0}
        for label, replies, fields, expected in _read_modify_write_cases(confirmed):
            with self.subTest(label):
                self._fresh_client()
                for field, value in {**original, **fields}.items():
                    setattr(self.server, field, value)
                self.server.save()
                base = {"config-test": ok, "config-set": ok, "config-write": ok, **responses}
                with stub_kea({**base, **replies}) as kea:
                    response = self.client.post(url, data)
                self.assertEqual(response.status_code, 302)
                sent = [
                    (message.level, str(message)) for message in django_messages.get_messages(response.wsgi_request)
                ]
                self.assertEqual(sent, [expected])
                applied = label.startswith(("applied", "unknown"))
                self.assertEqual("config-set" in kea.commands(), applied)
                self.assertEqual("config-write" in kea.commands(), applied and "not requested" not in label)

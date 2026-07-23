# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""View tests for Phase 3: NetBox IPAM sync endpoints.

URL names (all registered in netbox_kea/urls.py):
  server_lease4_sync       — POST /servers/<pk>/leases4/sync/
  server_lease6_sync       — POST /servers/<pk>/leases6/sync/
  server_reservation4_sync — POST /servers/<pk>/reservations4/sync/
  server_reservation6_sync — POST /servers/<pk>/reservations6/sync/

Each endpoint accepts POST with:
  ip_address   — host IP to sync
  hostname     — (optional) hostname / dns_name
  status       — "active" (leases) or "reserved" (reservations)

Returns an HTMX HTML fragment (<td> content) with a link to the new/updated
NetBox IPAddress, or an error message if something went wrong.

These tests drive the **real** ``KeaClient`` and the **real** sync functions
(``sync_lease_to_netbox`` / ``sync_reservation_to_netbox``) — they assert the
NetBox ``IPAddress`` rows those create. Only the HTTP boundary to Kea is stubbed
via ``kea_stub.stub_kea``:

* single lease sync       → ``lease{v}-get`` (echoes the posted IP back)
* single reservation sync → ``subnet{v}-list`` + ``reservation-get``
* bulk reservation sync   → ``reservation-get-page``
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress as NbIP

from netbox_kea.models import Server

from .kea_stub import _res_page, _subnet_list, stub_kea

User = get_user_model()

_PLUGINS_CONFIG = {"netbox_kea": {"kea_timeout": 30}}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — echo the queried IP back so the real sync creates the matching NbIP
# ─────────────────────────────────────────────────────────────────────────────


def _lease_get(hostname, **extra):
    """Build a ``lease{v}-get`` callable that echoes the queried ip-address."""

    def _resp(body):
        ip = body["arguments"]["ip-address"]
        return {"result": 0, "arguments": {"ip-address": ip, "hostname": hostname, "subnet-id": 1, **extra}}

    return _resp


def _reservation_get(hostname, **extra):
    """Build a ``reservation-get`` callable that echoes the queried ip-address."""

    def _resp(body):
        ip = body["arguments"]["ip-address"]
        return {"result": 0, "arguments": {"ip-address": ip, "hostname": hostname, "subnet-id": 1, **extra}}

    return _resp


def _make_server(**kwargs) -> Server:
    defaults = {
        "name": "sync-test-kea",
        "ca_url": "https://kea.example.com",
        "dhcp4": True,
        "dhcp6": True,
        "has_control_agent": True,
    }
    defaults.update(kwargs)
    return Server.objects.create(**defaults)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class _SyncViewBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="sync_testuser",
            email="sync_test@example.com",
            password="sync_testpass",
        )
        self.client.force_login(self.user)
        self.server = _make_server()

    def _start_stub(self, responses):
        """Enter a ``stub_kea`` context for the whole test and return the stub."""
        cm = stub_kea(responses)
        stub = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return stub


# ─────────────────────────────────────────────────────────────────────────────
# TestLease4SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease4SyncView(_SyncViewBase):
    """POST to server_lease4_sync creates/updates a NetBox IPAddress."""

    def setUp(self):
        super().setUp()
        self._start_stub(
            {
                "lease4-get": _lease_get(
                    "mock-host.local", **{"hw-address": "aa:bb:cc:00:00:01", "valid-lft": 86400, "cltt": 1700000000}
                )
            }
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(self._url(), {"ip_address": "192.168.10.5", "hostname": "host-a"})
        self.assertEqual(response.status_code, 200)

    def test_creates_netbox_ip_on_post(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.6", "hostname": "host-b"})
        self.assertTrue(NbIP.objects.filter(address__startswith="192.168.10.6/").exists())

    def test_created_ip_has_dhcp_status(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.7", "hostname": "host-c"})
        ip = NbIP.objects.filter(address__startswith="192.168.10.7/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "dhcp")

    def test_created_ip_has_correct_dns_name(self):
        # hostname in POST is ignored; dns_name comes from Kea lease data (mock returns "mock-host.local")
        self.client.post(self._url(), {"ip_address": "192.168.10.8", "hostname": "dns-test.local"})
        ip = NbIP.objects.filter(address__startswith="192.168.10.8/").first()
        self.assertEqual(ip.dns_name, "mock-host.local")

    def test_response_contains_ip_link(self):
        response = self.client.post(self._url(), {"ip_address": "192.168.10.9", "hostname": "link-host"})
        self.assertContains(response, "192.168.10.9")
        # Response must contain a link to the NetBox IP detail page
        self.assertContains(response, "/ipam/ip-addresses/")

    def test_returns_400_when_ip_address_missing(self):
        response = self.client.post(self._url(), {"hostname": "no-ip"})
        self.assertEqual(response.status_code, 400)

    def test_idempotent_second_post_does_not_create_duplicate(self):

        self.client.post(self._url(), {"ip_address": "192.168.10.20", "hostname": "idem-host"})
        self.client.post(self._url(), {"ip_address": "192.168.10.20", "hostname": "idem-host"})
        self.assertEqual(NbIP.objects.filter(address__startswith="192.168.10.20/").count(), 1)

    def test_returns_404_for_nonexistent_server(self):
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[99999])
        response = self.client.post(url, {"ip_address": "192.168.10.30", "hostname": "ghost"})
        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self._url(), {"ip_address": "192.168.10.31", "hostname": "anon"})
        # Should redirect to login (3xx) or return 403
        self.assertIn(response.status_code, [302, 403])


# ─────────────────────────────────────────────────────────────────────────────
# TestLease6SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestLease6SyncView(_SyncViewBase):
    """POST to server_lease6_sync creates/updates a NetBox IPAddress for IPv6."""

    def setUp(self):
        super().setUp()
        self._start_stub(
            {"lease6-get": _lease_get("mock-v6.local", duid="01:02:03:04", **{"valid-lft": 86400, "cltt": 1700000000})}
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_lease6_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(
            self._url(),
            {"ip_address": "2001:db8::1", "hostname": "v6host"},
        )
        self.assertEqual(response.status_code, 200)

    def test_creates_netbox_ip_with_slash128_for_ipv6(self):

        self.client.post(
            self._url(),
            {"ip_address": "2001:db8::2", "hostname": "v6host2"},
        )
        ip = NbIP.objects.filter(address__startswith="2001:db8::2/").first()
        self.assertIsNotNone(ip)
        self.assertTrue(str(ip.address).endswith("/128"))

    def test_created_ip_has_dhcp_status(self):

        self.client.post(
            self._url(),
            {"ip_address": "2001:db8::3", "hostname": "v6host3"},
        )
        ip = NbIP.objects.filter(address__startswith="2001:db8::3/").first()
        self.assertEqual(ip.status, "dhcp")


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation4SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4SyncView(_SyncViewBase):
    """POST to server_reservation4_sync creates/updates NetBox IP with status=reserved."""

    def setUp(self):
        super().setUp()
        self._start_stub(
            {
                "subnet4-list": _subnet_list(4, [{"id": 1, "subnet": "10.0.0.0/24"}]),
                "reservation-get": _reservation_get("mock-res.local", **{"hw-address": "aa:bb:cc:00:00:02"}),
            }
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(self._url(), {"ip_address": "10.0.0.50", "hostname": "res-host"})
        self.assertEqual(response.status_code, 200)

    def test_creates_ip_with_reserved_status(self):

        self.client.post(self._url(), {"ip_address": "10.0.0.51", "hostname": "res-host2"})
        ip = NbIP.objects.filter(address__startswith="10.0.0.51/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_sets_dns_name(self):
        # hostname in POST is ignored; dns_name comes from Kea reservation data (mock returns "mock-res.local")
        self.client.post(self._url(), {"ip_address": "10.0.0.52", "hostname": "dns.local"})
        ip = NbIP.objects.filter(address__startswith="10.0.0.52/").first()
        self.assertEqual(ip.dns_name, "mock-res.local")

    def test_response_contains_ip_link(self):
        response = self.client.post(self._url(), {"ip_address": "10.0.0.53", "hostname": "link-res"})
        self.assertContains(response, "10.0.0.53")
        self.assertContains(response, "/ipam/ip-addresses/")

    def test_returns_400_when_ip_missing(self):
        response = self.client.post(self._url(), {"hostname": "no-ip"})
        self.assertEqual(response.status_code, 400)


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation6SyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6SyncView(_SyncViewBase):
    """POST to server_reservation6_sync creates/updates NetBox IP for IPv6 reservation."""

    def setUp(self):
        super().setUp()
        self._start_stub(
            {
                "subnet6-list": _subnet_list(6, [{"id": 1, "subnet": "2001:db8:1::/64"}]),
                "reservation-get": _reservation_get("mock-v6res.local", duid="01:02:03:04"),
            }
        )

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_sync", args=[self.server.pk])

    def test_returns_200_on_valid_post(self):
        response = self.client.post(
            self._url(),
            {"ip_address": "2001:db8:1::50", "hostname": "v6res"},
        )
        self.assertEqual(response.status_code, 200)

    def test_creates_ip_with_reserved_status(self):

        self.client.post(
            self._url(),
            {"ip_address": "2001:db8:1::51", "hostname": "v6res2"},
        )
        ip = NbIP.objects.filter(address__startswith="2001:db8:1::51/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")


# ─────────────────────────────────────────────────────────────────────────────
# TestReservationBulkSyncView
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation4BulkSyncView(_SyncViewBase):
    """POST to server_reservation4_bulk_sync syncs all reservations to NetBox."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def test_redirects_after_success(self):
        hosts = [{"ip-address": "10.0.10.1", "hostname": "bulk-host", "subnet-id": 1}]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            response = self.client.post(self._url(), follow=False)
        # Must redirect back to reservations page
        self.assertIn(response.status_code, [302, 303])

    def test_creates_netbox_ips_for_all_reservations(self):
        hosts = [
            {"ip-address": "10.0.11.1", "hostname": "bulk-1", "subnet-id": 1},
            {"ip-address": "10.0.11.2", "hostname": "bulk-2", "subnet-id": 1},
        ]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())
        self.assertTrue(NbIP.objects.filter(address__startswith="10.0.11.1/").exists())
        self.assertTrue(NbIP.objects.filter(address__startswith="10.0.11.2/").exists())

    def test_created_ips_have_reserved_status(self):
        hosts = [{"ip-address": "10.0.12.1", "hostname": "bulk-rsv", "subnet-id": 1}]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())
        ip = NbIP.objects.filter(address__startswith="10.0.12.1/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")

    def test_returns_404_for_nonexistent_server(self):
        url = reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[99999])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self._url())
        self.assertIn(response.status_code, [302, 403])


# ─────────────────────────────────────────────────────────────────────────────
# Issue #9: Authorization checks before IPAM sync mutations
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestSyncViewPermissionChecks(_SyncViewBase):
    """Sync endpoints must reject users without IPAM write permissions."""

    def setUp(self):
        super().setUp()
        # Create a non-privileged user with no IPAM permissions
        self.limited_user = User.objects.create_user(
            username="limited_sync_user",
            email="limited@example.com",
            password="limitedpass",
        )

    def _login_limited(self):
        self.client.logout()
        self.client.force_login(self.limited_user)

    def test_lease4_sync_requires_ipam_add_permission(self):
        self._login_limited()
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        response = self.client.post(url, {"ip_address": "192.168.99.1"})
        self.assertEqual(response.status_code, 403)

    def test_reservation4_sync_requires_ipam_add_permission(self):
        self._login_limited()
        url = reverse("plugins:netbox_kea:server_reservation4_sync", args=[self.server.pk])
        response = self.client.post(url, {"ip_address": "192.168.99.2"})
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_still_sync(self):
        # self.user is superuser — should succeed as before
        url = reverse("plugins:netbox_kea:server_lease4_sync", args=[self.server.pk])
        stub = {
            "lease4-get": _lease_get(
                "mock-host.local", **{"hw-address": "aa:bb:cc:00:00:01", "valid-lft": 86400, "cltt": 1700000000}
            )
        }
        with stub_kea(stub):
            response = self.client.post(url, {"ip_address": "192.168.99.3"})
        self.assertEqual(response.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# TestReservation6BulkSyncView  (issue #13)
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservation6BulkSyncView(_SyncViewBase):
    """POST to server_reservation6_bulk_sync syncs all v6 reservations to NetBox."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[self.server.pk])

    def test_post_bulk_syncs_v6_reservations(self):
        """Bulk sync v6 reservation creates an IPAddress with /128 prefix and reserved status."""
        hosts = [{"subnet-id": 1, "duid": "00:01:aa:bb", "ip-addresses": ["2001:db8::1"], "hostname": "host-v6"}]
        with stub_kea({"reservation-get-page": _res_page(hosts)}):
            self.client.post(self._url())
        ip = NbIP.objects.filter(address__startswith="2001:db8::1/").first()
        self.assertIsNotNone(ip)
        self.assertEqual(ip.status, "reserved")
        self.assertIn("/128", str(ip.address))

    def test_post_unauthenticated_redirects(self):
        self.client.logout()
        response = self.client.post(self._url(), content_type="application/json")
        self.assertEqual(response.status_code, 302)

    def test_post_nonexistent_server_returns_404(self):
        url = reverse("plugins:netbox_kea:server_reservation6_bulk_sync", args=[99999])
        response = self.client.post(url, content_type="application/json")
        self.assertEqual(response.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# Issue #64: bulk-sync conflict protection + live IP-check endpoint
# ─────────────────────────────────────────────────────────────────────────────


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationBulkSyncConflictProtection(_SyncViewBase):
    """Bulk reservation sync must not overwrite foreign NetBox IPs; conflicts are counted."""

    def _url(self):
        return reverse("plugins:netbox_kea:server_reservation4_bulk_sync", args=[self.server.pk])

    def test_foreign_ip_not_overwritten_and_counted(self):
        from django.contrib import messages as django_messages

        NbIP.objects.create(address="10.0.20.5/32", status="active", description="Router loopback")
        hosts = [{"ip-address": "10.0.20.5", "hostname": "foreign", "subnet-id": 1}]
        # follow=True lands on the reservations list, which re-drains reservation-get-page
        # and enriches with lease4-get-all per subnet.
        stub = {"reservation-get-page": _res_page(hosts), "lease4-get-all": {"result": 0, "arguments": {"leases": []}}}
        with stub_kea(stub):
            response = self.client.post(self._url(), follow=True)

        # Foreign IP left untouched.
        ip = NbIP.objects.get(address="10.0.20.5/32")
        self.assertEqual(ip.status, "active")
        self.assertEqual(ip.description, "Router loopback")
        # Conflict surfaced in the summary message.
        msgs = [str(m) for m in django_messages.get_messages(response.wsgi_request)]
        self.assertTrue(any("1 conflicts skipped" in m for m in msgs), msgs)

    def test_managed_ip_still_synced_alongside_conflict(self):
        NbIP.objects.create(address="10.0.20.6/32", status="active", description="Router loopback")
        hosts = [
            {"ip-address": "10.0.20.6", "hostname": "foreign", "subnet-id": 1},
            {"ip-address": "10.0.20.7", "hostname": "managed", "subnet-id": 1},
        ]
        stub = {"reservation-get-page": _res_page(hosts), "lease4-get-all": {"result": 0, "arguments": {"leases": []}}}
        with stub_kea(stub):
            self.client.post(self._url(), follow=True)

        # Foreign untouched, the other reservation claimed normally.
        self.assertEqual(NbIP.objects.get(address="10.0.20.6/32").status, "active")
        claimed = NbIP.objects.filter(address__startswith="10.0.20.7/").first()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, "reserved")


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestReservationCheckNetboxIPView(_SyncViewBase):
    """GET endpoint that advises whether an IP already exists in NetBox IPAM."""

    def _url(self):
        return reverse("plugins:netbox_kea:reservation_check_ip", args=[self.server.pk])

    def test_empty_when_ip_missing(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_empty_when_ip_invalid(self):
        response = self.client.get(self._url(), {"ip": "not-an-ip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_empty_when_ip_not_in_netbox(self):
        response = self.client.get(self._url(), {"ip": "10.0.40.99"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_info_alert_for_kea_managed_ip(self):
        NbIP.objects.create(address="10.0.40.1/24", status="reserved", description="Synced from Kea DHCP reservation")
        response = self.client.get(self._url(), {"ip": "10.0.40.1"})
        body = response.content.decode()
        self.assertIn("alert-info", body)
        self.assertIn("Already in NetBox IPAM", body)

    def test_info_alert_for_blank_description_ip(self):
        NbIP.objects.create(address="10.0.40.2/24", status="active", description="")
        response = self.client.get(self._url(), {"ip": "10.0.40.2"})
        body = response.content.decode()
        self.assertIn("alert-info", body)

    def test_warning_alert_for_foreign_ip(self):
        NbIP.objects.create(address="10.0.40.3/24", status="active", description="Router loopback")
        response = self.client.get(self._url(), {"ip": "10.0.40.3"})
        body = response.content.decode()
        self.assertIn("alert-warning", body)
        self.assertIn("not", body.lower())
        self.assertIn("Router loopback", body)

    def test_matches_noncanonical_ipv6_query(self):
        """A non-canonical IPv6 query (expanded/zero-padded) still matches the
        canonical stored record — the view normalizes the input before the lookup.

        The DB stores ``2001:db8::5/64``; querying with the fully-expanded form
        must canonicalize to the same value so the conflict advisory still fires.
        Without normalization the ``address__startswith`` lookup would miss it and
        silently suppress the warning.
        """
        NbIP.objects.create(address="2001:db8::5/64", status="active", description="Router loopback")
        response = self.client.get(self._url(), {"ip": "2001:0db8:0000:0000:0000:0000:0000:0005"})
        body = response.content.decode()
        self.assertIn("alert-warning", body)
        self.assertIn("Router loopback", body)

    def test_404_for_nonexistent_server(self):
        url = reverse("plugins:netbox_kea:reservation_check_ip", args=[99999])
        response = self.client.get(url, {"ip": "10.0.40.1"})
        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(self._url(), {"ip": "10.0.40.1"})
        self.assertIn(response.status_code, [302, 403])

    def test_respects_ipam_view_permission(self):
        """A user who can view the server but not IPAM IPs must get an empty advisory.

        The advisory leaks an IP's status/description/assignment, so the lookup
        must be scoped with ``.restrict(user, "view")``. With an unrestricted
        lookup this user would see the foreign-IP warning for an IP they have no
        permission to view.
        """
        from django.contrib.contenttypes.models import ContentType
        from users.models import ObjectPermission

        NbIP.objects.create(address="10.0.40.7/24", status="active", description="Router loopback")

        limited = User.objects.create_user(username="limited_ipcheck", password="pass")
        # Grant server view but deliberately NO ipam.view_ipaddress permission.
        perm = ObjectPermission.objects.create(name="view-server-only-ipcheck", actions=["view"])
        perm.object_types.add(ContentType.objects.get_for_model(Server))
        perm.users.add(limited)
        self.client.force_login(limited)

        response = self.client.get(self._url(), {"ip": "10.0.40.7"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_advisory_shown_when_user_has_ipam_view_permission(self):
        """The same lookup still renders the advisory once the user can view IPAM IPs."""
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import IPAddress as IpamIP
        from users.models import ObjectPermission

        NbIP.objects.create(address="10.0.40.8/24", status="active", description="Router loopback")

        limited = User.objects.create_user(username="limited_ipcheck_ok", password="pass")
        server_perm = ObjectPermission.objects.create(name="view-server-ipcheck-ok", actions=["view"])
        server_perm.object_types.add(ContentType.objects.get_for_model(Server))
        server_perm.users.add(limited)
        ip_perm = ObjectPermission.objects.create(name="view-ipam-ipcheck-ok", actions=["view"])
        ip_perm.object_types.add(ContentType.objects.get_for_model(IpamIP))
        ip_perm.users.add(limited)
        self.client.force_login(limited)

        response = self.client.get(self._url(), {"ip": "10.0.40.8"})
        body = response.content.decode()
        self.assertIn("alert-warning", body)
        self.assertIn("Router loopback", body)

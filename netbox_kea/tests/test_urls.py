# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from collections import defaultdict

from django.test import SimpleTestCase
from django.urls import URLResolver, include, path, resolve, reverse

from netbox_kea.urls import urlpatterns
from netbox_kea.views.leases import ServerLease4AddView, ServerLease6AddView


def _registered_paths(patterns, prefix=""):
    for entry in patterns:
        route = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _registered_paths(entry.url_patterns, route)
        else:
            yield route, entry.name


class TestPluginURLRegistration(SimpleTestCase):
    def _assert_unique_paths(self, patterns):
        names_by_path = defaultdict(list)
        for route, name in _registered_paths(patterns):
            names_by_path[route].append(name)
        duplicates = {route: names for route, names in names_by_path.items() if len(names) > 1}
        self.assertEqual(duplicates, {}, f"Duplicate plugin URL paths: {duplicates}")

    def test_plugin_paths_have_one_registration(self):
        self._assert_unique_paths(urlpatterns)

    def test_duplicate_guard_detects_an_explicit_path_repeated_in_an_include(self):
        view = ServerLease4AddView.as_view()
        patterns = [
            path("servers/<int:pk>/leases4/add/", view, name="explicit_add"),
            path("servers/<int:pk>/", include([path("leases4/add/", view, name="registered_add")])),
        ]
        with self.assertRaisesRegex(AssertionError, "Duplicate plugin URL paths"):
            self._assert_unique_paths(patterns)

    def test_duplicate_guard_accepts_the_same_leaf_under_distinct_parent_paths(self):
        view = ServerLease4AddView.as_view()
        patterns = [
            path("servers/<int:pk>/", include([path("add/", view, name="server_add")])),
            path("combined/", include([path("add/", view, name="combined_add")])),
        ]
        self._assert_unique_paths(patterns)

    def test_lease_add_names_reverse_and_resolve_to_the_registered_views(self):
        for family, view in ((4, ServerLease4AddView), (6, ServerLease6AddView)):
            with self.subTest(family=family):
                url = reverse(f"plugins:netbox_kea:server_lease{family}_add", kwargs={"pk": 1})
                self.assertTrue(url.endswith(f"/servers/1/leases{family}/add/"), url)
                match = resolve(url)
                self.assertIs(match.func.view_class, view)
                self.assertEqual(match.view_name, f"plugins:netbox_kea:server_lease{family}_add")
                self.assertEqual(match.kwargs, {"pk": 1})

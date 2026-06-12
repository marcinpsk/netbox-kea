"""Pytest fixtures/hooks for netbox_kea unit tests.

Force the URL resolver to import every plugin's urlconf while the *real*
PLUGINS_CONFIG is active (at session start), so the plugin URL namespaces are
registered and cached in sys.modules before any test enters an
``@override_settings(PLUGINS_CONFIG=<minimal>)`` block.

Some co-installed plugins (in a multi-plugin dev container) read their own
``settings.PLUGINS_CONFIG['<plugin>']`` key at *import* time. When such a
plugin's urlconf is first imported lazily — which, without this hook, happens
inside an overridden test where its key is absent — it raises ``KeyError`` and
its namespace never registers, causing ``NoReverseMatch`` when the nav menu is
rendered by *every* page-rendering view test. Pre-populating the resolver here
avoids that. In an isolated CI environment (only netbox_kea installed) this is a
harmless no-op.
"""


def pytest_configure(config):  # noqa: D103
    try:
        import django

        django.setup()
        from django.urls import get_resolver

        get_resolver()._populate()
    except Exception:  # noqa: BLE001 — best effort; never block collection
        pass

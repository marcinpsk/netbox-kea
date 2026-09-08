# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""The browser login fixture must delete what it created even when setup fails.

The fixture talks to a remote NetBox API and a real browser, so both are replaced by
recording fakes here. Everything between them is the real fixture body.
"""

from typing import Any

import pytest

from tests.ui import conftest as ui_conftest


class _Deletable:
    """One created NetBox object that records its own deletion."""

    def __init__(self, label: str, deleted: list[str]) -> None:
        self.label = label
        self.id = 1
        self._deleted = deleted

    def delete(self) -> bool:
        self._deleted.append(self.label)
        return True

    def __str__(self) -> str:
        return self.label


class _NoMatches:
    def delete(self) -> bool:
        return True


class _Endpoint:
    """Stands in for one pynetbox endpoint."""

    def __init__(self, label: str, deleted: list[str], *, create_error: Exception | None = None) -> None:
        self.label = label
        self._deleted = deleted
        self._create_error = create_error

    def filter(self, **_kwargs: Any) -> _NoMatches:
        return _NoMatches()

    def create(self, **_kwargs: Any) -> _Deletable:
        if self._create_error is not None:
            raise self._create_error
        return _Deletable(self.label, self._deleted)


class _Api:
    def __init__(self, deleted: list[str], *, permission_error: Exception | None = None) -> None:
        self.users = type(
            "_UsersApi",
            (),
            {
                "users": _Endpoint("user", deleted),
                "permissions": _Endpoint("permission", deleted, create_error=permission_error),
            },
        )()


class _Locator:
    def fill(self, _value: str) -> None:
        pass

    def click(self) -> None:
        pass


class _Page:
    """Records navigation so a login failure can be simulated."""

    def __init__(self, *, goto_error: Exception | None = None) -> None:
        self._goto_error = goto_error

    def goto(self, _url: str) -> None:
        if self._goto_error is not None:
            raise self._goto_error

    def get_by_label(self, _name: str) -> _Locator:
        return _Locator()

    def get_by_role(self, _role: str, name: str = "") -> _Locator:
        return _Locator()


def _drive(api: _Api, page: _Page):
    # pytest 8.4+ wraps a fixture in FixtureFunctionDefinition. Ask it for the
    # function before falling back to the dunder, which is not part of its contract.
    fixture = ui_conftest.netbox_login
    unwrap = getattr(fixture, "_get_wrapped_function", None)
    fixture_function = unwrap() if unwrap is not None else getattr(fixture, "__wrapped__", fixture)
    return fixture_function(
        page=page,
        netbox_url="http://netbox.invalid",
        netbox_username="ui-fixture-tester",
        netbox_password="password",
        netbox_user_permissions=[{"actions": [], "object_types": []}],
        nb_api=api,
    )


def test_a_failed_permission_create_still_deletes_the_user():
    """A raise between the first create and the yield must not leak the user."""
    deleted: list[str] = []
    generator = _drive(_Api(deleted, permission_error=RuntimeError("permission create failed")), _Page())

    with pytest.raises(RuntimeError, match="permission create failed"):
        next(generator)

    assert deleted == ["user"]


def test_a_failed_login_still_deletes_the_user_and_its_permissions():
    """The browser login runs after both creates, so its failure must clean up both."""
    deleted: list[str] = []
    generator = _drive(_Api(deleted), _Page(goto_error=RuntimeError("browser navigation failed")))

    with pytest.raises(RuntimeError, match="browser navigation failed"):
        next(generator)

    assert deleted == ["user", "permission"]


def test_a_successful_run_deletes_what_it_created():
    """The existing teardown must keep working through the new finally."""
    deleted: list[str] = []
    generator = _drive(_Api(deleted), _Page())

    next(generator)
    assert deleted == []

    generator.close()

    assert deleted == ["user", "permission"]

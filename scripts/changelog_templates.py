# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Reuse semantic-release templates while overriding the commit aggregation component.

The upstream component selects only the first commit in each category for NOTICE and
BREAKING CHANGE footers. Keep its surrounding release format and formatting macros,
but collect footers from every commit in the repository's component override.
"""

from jinja2 import ChoiceLoader, PackageLoader, PrefixLoader
from jinja2.ext import Extension


class ChangelogTemplates(Extension):
    """Add upstream templates after the repository's explicit component overrides."""

    def __init__(self, environment):
        """Keep native template resolution and use the upstream package as the fallback."""
        super().__init__(environment)
        upstream = PackageLoader("semantic_release", "data/templates/conventional/md")
        environment.loader = ChoiceLoader(
            [
                environment.loader,
                PrefixLoader({"upstream": upstream}),
                upstream,
            ]
        )

# SPDX-FileCopyrightText: 2025 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""NetBox plugin template content registration.

NetBox looks for ``template_extensions`` in this module by default when
``PluginConfig.template_extensions`` is ``None``.
"""

from .template_extensions import IPAddressKeaPanel

template_extensions = [IPAddressKeaPanel]

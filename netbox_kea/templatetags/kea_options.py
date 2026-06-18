"""Template tags for rendering known Kea DHCP option suggestions."""

from django import template

from ..constants import kea_std_options

register = template.Library()


@register.inclusion_tag("netbox_kea/inc/option_datalist.html")
def kea_option_datalist(dhcp_version: int = 4) -> dict:
    """Render a ``<datalist>`` of standard option names for the given DHCP version.

    The datalist (id ``kea-option-names``) backs the editable option-name combobox
    on the option-data editors. Suggestions are version-specific; free-form entry
    is still permitted because the bound field is a plain text input.
    """
    try:
        version = int(dhcp_version)
    except (TypeError, ValueError):
        version = 4
    return {"options": kea_std_options(version), "dhcp_version": version}

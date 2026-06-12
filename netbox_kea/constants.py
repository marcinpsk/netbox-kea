BY_IP = "ip"
BY_HOSTNAME = "hostname"
BY_DUID = "duid"
BY_SUBNET = "subnet"
BY_SUBNET_ID = "subnet_id"
BY_HW_ADDRESS = "hw"
BY_CLIENT_ID = "client_id"

HEX_STRING_REGEX = r"^([0-9A-Fa-f]{2}[:-]?)*([0-9A-Fa-f]{2})$"

# kea/src/lib/dhcp
# RFC8415 section 11.1
DUID_MAX_OCTETS = 128
DUID_MIN_OCTETS = 1
CLIENT_ID_MAX_OCTETS = DUID_MAX_OCTETS
CLIENT_ID_MIN_OCTETS = 2

# Kea lease state codes and human-readable labels.
# https://kea.readthedocs.io/en/latest/arm/lease-db.html#lease-states
LEASE_STATE_LABELS: dict[int, str] = {
    0: "Active",
    1: "Declined",
    2: "Expired",
}

LEASE_STATE_CHOICES = [("", "Any")] + [(str(k), v) for k, v in LEASE_STATE_LABELS.items()]

# ---------------------------------------------------------------------------
# Standard DHCP option names shipped with Kea's built-in option definitions.
# Used to populate editable dropdowns (<datalist>) on the option-data editors.
# These are suggestions only — users may still type any option name (including
# custom ones defined via option-def). Each entry is (name, code).
# Reference: Kea ARM "Standard DHCPv4/DHCPv6 Options".
# ---------------------------------------------------------------------------

KEA_DHCP4_STD_OPTIONS: list[tuple[str, int]] = [
    ("subnet-mask", 1),
    ("time-offset", 2),
    ("routers", 3),
    ("time-servers", 4),
    ("name-servers", 5),
    ("domain-name-servers", 6),
    ("log-servers", 7),
    ("cookie-servers", 8),
    ("lpr-servers", 9),
    ("impress-servers", 10),
    ("resource-location-servers", 11),
    ("host-name", 12),
    ("boot-size", 13),
    ("merit-dump", 14),
    ("domain-name", 15),
    ("swap-server", 16),
    ("root-path", 17),
    ("extensions-path", 18),
    ("ip-forwarding", 19),
    ("non-local-source-routing", 20),
    ("policy-filter", 21),
    ("max-dgram-reassembly", 22),
    ("default-ip-ttl", 23),
    ("path-mtu-aging-timeout", 24),
    ("path-mtu-plateau-table", 25),
    ("interface-mtu", 26),
    ("all-subnets-local", 27),
    ("broadcast-address", 28),
    ("perform-mask-discovery", 29),
    ("mask-supplier", 30),
    ("router-discovery", 31),
    ("router-solicitation-address", 32),
    ("static-routes", 33),
    ("trailer-encapsulation", 34),
    ("arp-cache-timeout", 35),
    ("ethernet-encapsulation", 36),
    ("default-tcp-ttl", 37),
    ("tcp-keepalive-interval", 38),
    ("tcp-keepalive-garbage", 39),
    ("nis-domain", 40),
    ("nis-servers", 41),
    ("ntp-servers", 42),
    ("vendor-encapsulated-options", 43),
    ("netbios-name-servers", 44),
    ("netbios-dd-server", 45),
    ("netbios-node-type", 46),
    ("netbios-scope", 47),
    ("font-servers", 48),
    ("x-display-manager", 49),
    ("dhcp-requested-address", 50),
    ("dhcp-lease-time", 51),
    ("dhcp-option-overload", 52),
    ("dhcp-message-type", 53),
    ("dhcp-server-identifier", 54),
    ("dhcp-parameter-request-list", 55),
    ("dhcp-message", 56),
    ("dhcp-max-message-size", 57),
    ("dhcp-renewal-time", 58),
    ("dhcp-rebinding-time", 59),
    ("vendor-class-identifier", 60),
    ("dhcp-client-identifier", 61),
    ("nwip-domain-name", 62),
    ("nwip-suboptions", 63),
    ("nisplus-domain-name", 64),
    ("nisplus-servers", 65),
    ("tftp-server-name", 66),
    ("boot-file-name", 67),
    ("mobile-ip-home-agent", 68),
    ("smtp-server", 69),
    ("pop-server", 70),
    ("nntp-server", 71),
    ("www-server", 72),
    ("finger-server", 73),
    ("irc-server", 74),
    ("streettalk-server", 75),
    ("streettalk-directory-assistance-server", 76),
    ("user-class", 77),
    ("slp-directory-agent", 78),
    ("slp-service-scope", 79),
    ("fqdn", 81),
    ("dhcp-agent-options", 82),
    ("nds-servers", 85),
    ("nds-tree-name", 86),
    ("nds-context", 87),
    ("bcms-controller-names", 88),
    ("bcms-controller-address", 89),
    ("authenticate", 90),
    ("client-last-transaction-time", 91),
    ("associated-ip", 92),
    ("client-system", 93),
    ("client-ndi", 94),
    ("uuid-guid", 97),
    ("uap-servers", 98),
    ("netinfo-server-address", 112),
    ("netinfo-server-tag", 113),
    ("default-url", 114),
    ("auto-config", 116),
    ("name-service-search", 117),
    ("subnet-selection", 118),
    ("domain-search", 119),
    ("classless-static-route", 121),
    ("vivco-suboptions", 124),
    ("vivso-suboptions", 125),
    ("pana-agent", 136),
    ("v4-lost", 137),
    ("capwap-ac-v4", 138),
    ("sip-ua-cs-domains", 141),
    ("rdnss-selection", 146),
    ("v4-portparams", 159),
    ("v4-captive-portal", 160),
]

KEA_DHCP6_STD_OPTIONS: list[tuple[str, int]] = [
    ("preference", 7),
    ("unicast", 12),
    ("status-code", 13),
    ("rapid-commit", 14),
    ("user-class", 15),
    ("vendor-class", 16),
    ("vendor-opts", 17),
    ("interface-id", 18),
    ("reconf-msg", 19),
    ("reconf-accept", 20),
    ("sip-server-dns", 21),
    ("sip-server-addr", 22),
    ("dns-servers", 23),
    ("domain-search", 24),
    ("nis-servers", 27),
    ("nisp-servers", 28),
    ("nis-domain-name", 29),
    ("nisp-domain-name", 30),
    ("sntp-servers", 31),
    ("information-refresh-time", 32),
    ("bcmcs-server-dns", 33),
    ("bcmcs-server-addr", 34),
    ("geoconf-civic", 36),
    ("remote-id", 37),
    ("subscriber-id", 38),
    ("client-fqdn", 39),
    ("pana-agent", 40),
    ("new-posix-timezone", 41),
    ("new-tzdb-timezone", 42),
    ("ero", 43),
    ("lq-query", 44),
    ("client-data", 45),
    ("clt-time", 46),
    ("lq-relay-data", 47),
    ("lq-client-link", 48),
    ("bootfile-url", 59),
    ("bootfile-param", 60),
    ("client-arch-type", 61),
    ("nii", 62),
    ("aftr-name", 64),
    ("erp-local-domain-name", 65),
    ("rsoo", 66),
    ("pd-exclude", 67),
    ("rdnss-selection", 74),
    ("client-linklayer-addr", 79),
    ("solmax-rt", 82),
    ("inf-max-rt", 83),
    ("dhcp4o6-server-addr", 88),
    ("v6-captive-portal", 103),
]


def kea_std_options(version: int) -> list[tuple[str, int]]:
    """Return the standard option (name, code) list for the given DHCP version."""
    return KEA_DHCP6_STD_OPTIONS if version == 6 else KEA_DHCP4_STD_OPTIONS

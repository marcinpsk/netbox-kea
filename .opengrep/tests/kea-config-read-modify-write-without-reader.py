# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
# Test fixtures for kea-config-read-modify-write-without-reader. Intentionally
# contains rule-violating code; excluded from ruff (see pyproject exclude).


class Client:
    def bad_update(self, version, options):
        service = f"dhcp{version}"
        # ruleid: kea-config-read-modify-write-without-reader
        resp = self.command("config-get", service=[service])
        config = resp[0]["arguments"]
        config.setdefault(f"Dhcp{version}", {})["option-data"] = options
        self.config_set(config)

    def bad_update_keyword(self, version, name):
        service = f"dhcp{version}"
        # ruleid: kea-config-read-modify-write-without-reader
        resp = self.command(command="config-get", service=[service])
        for network in resp[0]["arguments"][f"Dhcp{version}"]["shared-networks"]:
            network["description"] = name
        self.config_set(resp[0]["arguments"])
        return name

    def ok_reader(self, version, options):
        candidate = self.config_candidate(version)
        candidate.set_global_options(options)
        self.config_set(candidate)

    def config_candidate(self, version):
        service = f"dhcp{version}"
        # ok: kea-config-read-modify-write-without-reader
        resp = self.command("config-get", service=[service])
        return resp[0]["arguments"]

    def ok_read_only(self, version):
        # ok: kea-config-read-modify-write-without-reader
        return self.command("config-get", service=[f"dhcp{version}"])

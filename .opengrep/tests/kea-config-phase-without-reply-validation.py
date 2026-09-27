# SPDX-FileCopyrightText: 2026 Marcin Zieba
# SPDX-License-Identifier: Apache-2.0


def apply(client, service, config):
    # ruleid: kea-config-phase-without-reply-validation
    client.command("config-test", service=[service], arguments=config)
    # ruleid: kea-config-phase-without-reply-validation
    client.command("config-set", service=[service], arguments=config)
    # ruleid: kea-config-phase-without-reply-validation
    client.command("config-write", service=[service])
    # ruleid: kea-config-phase-without-reply-validation
    client._config_mutation_command("config-set", service, config)
    # ruleid: kea-config-phase-without-reply-validation
    client.command(command="config-test", service=[service], arguments=config)
    # ruleid: kea-config-phase-without-reply-validation
    client.command(command="config-set", service=[service], arguments=config)
    # ruleid: kea-config-phase-without-reply-validation
    client.command(service=[service], command="config-write")
    # ruleid: kea-config-phase-without-reply-validation
    client._config_mutation_command(command="config-set", service=service, arguments=config)
    # ok: kea-config-phase-without-reply-validation
    client._config_phase_command("config-test", service, config)
    # ok: kea-config-phase-without-reply-validation
    client._config_phase_command("config-set", service, config)
    # ok: kea-config-phase-without-reply-validation
    client._config_phase_command("config-write", service)
    # ok: kea-config-phase-without-reply-validation
    client.command("config-get", service=[service])


def _config_phase_command(client, command, service):
    # ok: kea-config-phase-without-reply-validation
    return client.command(command, service=[service], check=None)

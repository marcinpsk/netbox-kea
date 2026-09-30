# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0


def apply(client, family, config):
    # ruleid: kea-config-phase-without-reply-validation
    client.command(KeaCommand.CONFIG_TEST, family, arguments=config)
    # ruleid: kea-config-phase-without-reply-validation
    client.command(KeaCommand.CONFIG_SET, family, arguments=config)
    # ruleid: kea-config-phase-without-reply-validation
    client.command(KeaCommand.CONFIG_WRITE, family)
    # ruleid: kea-config-phase-without-reply-validation
    client.command(command=KeaCommand.CONFIG_TEST, target=family, arguments=config)
    # ruleid: kea-config-phase-without-reply-validation
    client.command(target=family, command=KeaCommand.CONFIG_WRITE)
    # ruleid: kea-config-phase-without-reply-validation
    client._one_command(KeaCommand.CONFIG_SET, family, config)
    # ok: kea-config-phase-without-reply-validation
    client._one_command(KeaCommand.CONFIG_TEST, family, config)
    # ok: kea-config-phase-without-reply-validation
    client._one_command(KeaCommand.CONFIG_WRITE, family)
    # ok: kea-config-phase-without-reply-validation
    client._config_mutation_command(KeaCommand.CONFIG_SET, family, config)
    # ok: kea-config-phase-without-reply-validation
    client.command(KeaCommand.CONFIG_GET, family)


def _one_command(client, command, family):
    # ok: kea-config-phase-without-reply-validation
    return client.command(command, family, check=None)

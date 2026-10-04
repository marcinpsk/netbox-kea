# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Keep DHCP Import Mappings in the same lifecycle as their imported targets."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import cache, wraps

from django.apps import apps
from django.db import connections, models, router, transaction
from django.db.models.deletion import Collector
from django.db.models.signals import m2m_changed, pre_delete, pre_save
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from utilities.exceptions import AbortRequest

from . import branching

MAPPING_IDENTITY_FIELDS = ("server", "family", "kea_subnet_id", "kea_identity", "object_type", "object_id")
_METADATA_LOCK = (0x4B4541, 0x44484350)


@dataclass(frozen=True)
class ReplayOperation:
    """The explicit native action that owns a main replay transaction."""

    branch: object
    action: str
    mappings: dict = dataclass_field(default_factory=dict)
    destructive_targets: set = dataclass_field(default_factory=set)
    protected_targets: set = dataclass_field(default_factory=set)
    object_states: dict = dataclass_field(default_factory=dict)
    target_associations: dict = dataclass_field(default_factory=dict)
    native_requests: set[object] = dataclass_field(default_factory=set)


_replay_operation: ContextVar[ReplayOperation | None] = ContextVar("kea_mapping_replay_operation", default=None)


@contextmanager
def metadata_scope(using: str = "default"):
    """Coordinate before native selection, and refuse contention when a caller can hold row locks."""
    aliases = {*branching.connection_aliases(), using}
    transactional = any(
        connection.connection is not None and (connection.in_atomic_block or not connection.get_autocommit())
        for connection in (connections[alias] for alias in aliases)
    )
    with transaction.atomic(using=using):
        with connections[using].cursor() as cursor:
            function = "pg_try_advisory_xact_lock" if transactional else "pg_advisory_xact_lock"
            cursor.execute(f"SELECT {function}(%s, %s)", _METADATA_LOCK)
            if transactional and not cursor.fetchone()[0]:
                raise AbortRequest("DHCP mapping metadata is changing in another transaction. Retry this operation.")
        yield


@contextmanager
def action_scope(branch, action: str):
    """Isolate native deletion history and restore the enclosing request on every exit."""
    from core.signals import _signals_received
    from netbox.context import current_request

    enclosing = current_request.get() is not None
    previous: set[tuple[object, int]] = getattr(_signals_received, "pre_delete", set()) if enclosing else set()
    _signals_received.pre_delete = set()
    token = _replay_operation.set(ReplayOperation(branch, action))
    try:
        yield
    finally:
        _replay_operation.reset(token)
        _signals_received.pre_delete = previous


def mapping_identity(mapping) -> tuple:
    """Read the shared semantic identity from a mapping instance or native history payload."""
    if isinstance(mapping, dict):
        return tuple(mapping.get(name) for name in MAPPING_IDENTITY_FIELDS)
    return tuple(getattr(mapping, mapping._meta.get_field(name).attname) for name in MAPPING_IDENTITY_FIELDS)


def observe_mapping(server, family: int, target, *, subnet_id=None, reservation_identity=None):
    """Record a source association, or refresh its observation without a semantic history change."""
    from django.contrib.contenttypes.models import ContentType

    from .models import KeaDhcpLink

    source = {"server": server, "family": family}
    source["kea_subnet_id" if subnet_id is not None else "kea_identity"] = (
        subnet_id if subnet_id is not None else reservation_identity
    )
    association = {
        **source,
        "kea_subnet_id": subnet_id,
        "kea_identity": reservation_identity,
        "object_type": ContentType.objects.get_for_model(target),
        "object_id": target.pk,
    }
    with metadata_scope():
        mapping = KeaDhcpLink.objects.filter(**source).first()
        desired = KeaDhcpLink(**association)
        if mapping is None:
            desired.save()
            return desired
        if mapping_identity(mapping) == mapping_identity(desired):
            mapping.last_synced = timezone.now()
            KeaDhcpLink.objects.filter(pk=mapping.pk).update(last_synced=mapping.last_synced)
            return mapping
        mapping.snapshot()
        for name, value in association.items():
            setattr(mapping, name, value)
        mapping.save()
        return mapping


class MappingUnavailable(AbortRequest):
    """The active branch cannot safely read or change DHCP Import Mappings."""


def target_models():
    """Return the supported imported target classes when the DHCP plugin is installed."""
    if not apps.is_installed("netbox_dhcp"):
        return ()
    return tuple(apps.get_model("netbox_dhcp", name) for name in ("Subnet", "HostReservation"))


@cache
def metadata_writer_models() -> set:
    """Include endpoints whose names form serialized target relation state."""
    from .models import KeaDhcpLink

    targets = target_models()
    named_relations = {
        field.remote_field.model
        for target in targets
        for field in target._meta.many_to_many
        if _relation_identity_field(field) == "name"
    }
    return {*targets, KeaDhcpLink, *named_relations}


def _relation_identity_field(field) -> str:
    return "name" if hasattr(field, "manager") else "pk"


def require_branch_mappings(target=None, *, using=None) -> None:
    """Refuse configured main routing or an incomplete branch mapping table before a query."""
    branch = branching.branch_for_alias(using) or branching.active_branch()
    if branch is None:
        return
    from .models import KeaDhcpLink

    models = (KeaDhcpLink, *((target,) if target is not None else target_models()))
    if any(not branching.supports_branching(model) for model in models):
        raise MappingUnavailable(
            "DHCP Import Mappings and their targets must support branching. "
            "Remove the affected branching exemption and create a fresh branch."
        )
    with connections["default"].cursor() as cursor:
        for model in models:
            cursor.execute(
                "SELECT a.attname FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped",
                [branch.schema_name, model._meta.db_table],
            )
            columns = {row[0] for row in cursor.fetchall()}
            if not {field.column for field in model._meta.concrete_fields}.issubset(columns):
                raise MappingUnavailable(
                    "DHCP Import Mappings or their targets are unavailable in this older branch. "
                    "Create a fresh branch to read mappings or delete imported DHCP targets."
                )


def mapping_unavailable_reason() -> str | None:
    """Return an explanation for a read page that does not require mapping access."""
    try:
        require_branch_mappings()
    except MappingUnavailable as error:
        return str(error)
    return None


def preflight_action(branch, operation: str) -> None:
    """Reject an affected unsupported strategy before the native action changes status."""
    changes = branch.get_changes()
    mappings = changes.filter(changed_object_type__app_label="netbox_kea", changed_object_type__model="keadhcplink")
    destructive_targets = changes.filter(
        changed_object_type__app_label="netbox_dhcp",
        changed_object_type__model__in=[model._meta.model_name for model in target_models()],
        action="delete" if operation == "merge" else "create",
    )
    if not mappings.exists() and not destructive_targets.exists():
        return
    with branching.branch_scope(branch):
        require_branch_mappings()
    if mappings.exists() and branch.merge_strategy != "squash":
        raise AbortRequest("DHCP Import Mapping recovery requires the squash merge strategy. Select squash and retry.")


def prepare_replay(branch, collapsed, operation: str, request) -> None:
    """Validate the whole affected main footprint before the first native replay mutation."""
    from .models import KeaDhcpLink

    mappings = {change.key[1]: change for change in collapsed.values() if change.model_class is KeaDhcpLink}
    for change in mappings.values():
        if change.final_action in {"delete", "update"}:
            _validate_history_payload(KeaDhcpLink, change.prechange_data)
        if change.final_action in {"create", "update"}:
            _validate_history_payload(KeaDhcpLink, change.postchange_data)
    protected = _historical_targets(mappings.values())
    target_changes, affected, destructive = _replay_target_footprint(collapsed, operation)
    current_targets, expected_mappings, copied_associations = _validate_target_associations(
        branch, mappings, target_changes, affected, destructive, operation
    )
    protected.update(current_targets)
    for change in collapsed.values():
        if change.model_class is KeaDhcpLink or (change.model_class, change.key[1]) in protected | destructive:
            _validate_current_state(change, operation)
    source_ids = set()
    for change in mappings.values():
        restores = change.final_action in (("create", "update") if operation == "merge" else ("delete", "update"))
        if restores:
            payload = change.postchange_data if operation == "merge" else change.prechange_data
            source_ids.add(payload["server"])
    _require_servers(source_ids)
    _validate_restorations(collapsed, protected, operation)
    replay = _replay_operation.get()
    if replay is not None:
        if request is not None:
            replay.native_requests.add(request)
        replay.protected_targets.update(protected)
        replay.destructive_targets.update(destructive)
        state_changes = {
            **{key: change for key, change in target_changes.items() if key in protected | destructive},
            **{(KeaDhcpLink, pk): change for pk, change in mappings.items()},
        }
        replay.object_states.update(
            (
                key,
                None
                if change.final_action == ("create" if operation == "merge" else "delete")
                else change.prechange_data
                if operation == "merge"
                else change.postchange_data,
            )
            for key, change in state_changes.items()
        )
        replay.mappings.update(expected_mappings)
        replay.target_associations.update(copied_associations)
        for change in mappings.values():
            if change.final_action == ("delete" if operation == "merge" else "create"):
                replay.mappings[change.key[1]] = (
                    change.prechange_data if operation == "merge" else change.postchange_data
                )


def _replay_target_footprint(collapsed, operation: str):
    target_changes = {
        (change.model_class, change.key[1]): change
        for change in collapsed.values()
        if change.model_class in target_models() and change.final_action != "skip"
    }
    deleted: dict[object, set] = {}
    updates: dict[object, dict] = {}
    for change in collapsed.values():
        if change.final_action == ("delete" if operation == "merge" else "create"):
            deleted.setdefault(change.model_class, set()).add(change.key[1])
        elif change.final_action == "update":
            updates.setdefault(change.model_class, {})[change.key[1]] = change.generate_object_change().get_merge_data(
                reverse=operation == "revert"
            )
    effects = set()
    for change in collapsed.values():
        if change.model_class in delete_effect_models() and change.final_action == (
            "delete" if operation == "merge" else "create"
        ):
            current = change.model_class._base_manager.using("default").filter(pk=change.key[1]).first()
            if current is not None:
                effects.update(_delete_effect_targets(current, deleted, updates))
    affected = effects | set(target_changes)
    destructive = {
        key
        for key, change in target_changes.items()
        if change.final_action == ("delete" if operation == "merge" else "create")
    }
    return target_changes, affected, destructive


def _validate_target_associations(branch, mappings, target_changes, affected, destructive, operation):
    from django.contrib.contenttypes.models import ContentType

    from .models import KeaDhcpLink

    protected = set()
    expected_mappings = {}
    copied_associations = {}
    for model, pk in affected:
        with branching.branch_scope(branch):
            require_branch_mappings(model)
        association = {"object_type": ContentType.objects.get_for_model(model), "object_id": pk}
        copied = list(KeaDhcpLink.objects.using(branch.connection_name).filter(**association))
        _require_servers({mapping.server_id for mapping in copied})
        current = list(KeaDhcpLink.objects.using("default").filter(**association))
        for mapping in copied:
            protected.add((model, pk))
            if mapping.pk not in mappings:
                original = next((row for row in current if row.pk == mapping.pk), None)
                if original is None:
                    raise AbortRequest("A main DHCP mapping from the selected branch is missing. Nothing changed.")
                payload = mapping.serialize_object()
                _validate_existing_state(original, payload)
                copied_associations.setdefault((model, pk), {})[mapping.pk] = payload
        for mapping in current:
            if (model, pk) not in target_changes:
                raise AbortRequest("A main DHCP mapping target has no reversible branch history. Nothing changed.")
            protected.add((model, pk))
            if (model, pk) not in destructive:
                continue
            recorded = mappings.get(mapping.pk)
            payload = recorded.prechange_data if recorded is not None else None
            if operation == "revert" and recorded is not None:
                payload = recorded.postchange_data
            if payload is None or mapping_identity(mapping) != mapping_identity(payload):
                raise AbortRequest("A main DHCP Import mapping has no reversible branch history. Nothing changed.")
            if branch.merge_strategy != "squash":
                raise AbortRequest(
                    "DHCP Import Mapping recovery requires the squash merge strategy. Select squash and retry."
                )
            expected_mappings[mapping.pk] = payload
    return protected, expected_mappings, copied_associations


def _delete_effect_targets(instance, deleted, updates) -> set:
    """Use the native collector to inspect cascades and relation changes without mutation."""

    class FootprintCollector(Collector):
        """Inspect relations after the replay's separately validated deletions and FK changes."""

        def related_objects(self, related_model, related_fields, objs):
            queryset = super().related_objects(related_model, related_fields, objs)
            queryset = queryset.exclude(pk__in=deleted.get(related_model, ()))
            moved = models.Q()
            for pk, payload in updates.get(related_model, {}).items():
                condition = models.Q(pk=pk)
                for field in related_fields:
                    selected = {getattr(obj, field.target_field.attname) for obj in objs}
                    if field.name in payload:
                        if field.target_field.to_python(payload[field.name]) in selected:
                            break
                    else:
                        condition &= ~models.Q(**{f"{field.attname}__in": selected})
                else:
                    moved |= condition
            return queryset.exclude(moved)

    collector = FootprintCollector(using="default")
    collector.collect([instance])
    selected = {model: {obj.pk for obj in objects} for model, objects in collector.data.items()}
    affected = {(model, pk) for model in target_models() for pk in selected.get(model, ())}
    for target in target_models():
        for field in (*target._meta.concrete_fields, *target._meta.many_to_many):
            remote = getattr(field, "remote_field", None)
            if remote is None or remote.model not in selected:
                continue
            queryset = target._base_manager.using("default").filter(**{f"{field.name}__in": selected[remote.model]})
            affected.update((target, pk) for pk in queryset.values_list("pk", flat=True))
    return affected


def _validate_restorations(collapsed, protected, operation: str) -> None:
    from .models import KeaDhcpLink

    restores = {}
    creates = {}
    deleted: dict[object, set] = {}
    for change in collapsed.values():
        if change.final_action == "skip":
            continue
        key = (change.model_class, change.key[1])
        if change.final_action == ("delete" if operation == "merge" else "create"):
            deleted.setdefault(change.model_class, set()).add(change.key[1])
        else:
            restores[key] = change.postchange_data if operation == "merge" else change.prechange_data
            if change.final_action == ("create" if operation == "merge" else "delete"):
                creates[key] = restores[key]
    for (model, pk), payload in restores.items():
        if model is not KeaDhcpLink and (model, pk) not in protected:
            continue
        _validate_history_payload(model, payload)
        _require_dependencies(model, payload, creates)
        if model is KeaDhcpLink:
            _validate_mapping_restoration(pk, payload, creates, deleted)
        else:
            _validate_target_uniqueness(model, pk, payload, deleted)


def _mapping_target_model(content_type_id):
    from django.contrib.contenttypes.models import ContentType

    content_type = ContentType.objects.using("default").filter(pk=content_type_id).first()
    return content_type.model_class() if content_type is not None else None


def _require_dependencies(model, payload, restores):
    for field in model._meta.concrete_fields:
        if not isinstance(field, models.ForeignKey) or payload[field.name] is None:
            continue
        related = field.remote_field.model
        identity = payload[field.name]
        if (related, identity) not in restores and not related._base_manager.using("default").filter(
            pk=identity
        ).exists():
            raise AbortRequest("A DHCP mapping or target dependency is missing. Nothing changed.")
    for field in model._meta.many_to_many:
        identities = set(payload[field.name] or [])
        related = field.remote_field.model
        lookup = _relation_identity_field(field)
        present = set(
            related._base_manager.using("default")
            .filter(**{f"{lookup}__in": identities})
            .values_list(lookup, flat=True)
        )
        planned = (
            {key[1] for key in restores if key[0] is related}
            if lookup == "pk"
            else {data["name"] for key, data in restores.items() if key[0] is related}
        )
        if identities - present - planned:
            raise AbortRequest("A DHCP target relation dependency is missing. Nothing changed.")


def _validate_mapping_restoration(pk, payload, restores, deleted):
    from .models import KeaDhcpLink

    target = _mapping_target_model(payload["object_type"])
    if target not in target_models() or (
        (target, payload["object_id"]) not in restores
        and not target._base_manager.using("default").filter(pk=payload["object_id"]).exists()
    ):
        raise AbortRequest("A DHCP mapping target dependency is missing. Nothing changed.")
    source_field = "kea_subnet_id" if payload["kea_subnet_id"] is not None else "kea_identity"
    lookups = (
        {name: payload[name] for name in ("server", "family", source_field)},
        {name: payload[name] for name in ("object_type", "object_id")},
    )
    for lookup in lookups:
        if (
            KeaDhcpLink._base_manager.using("default")
            .filter(**lookup)
            .exclude(pk=pk)
            .exclude(pk__in=deleted.get(KeaDhcpLink, ()))
            .exists()
        ):
            raise AbortRequest("A DHCP mapping source or target identity conflicts with main. Nothing changed.")


def _validate_target_uniqueness(model, pk, payload, deleted):
    unique = [(field.name,) for field in model._meta.concrete_fields if field.unique and not field.primary_key]
    unique.extend(
        constraint.fields
        for constraint in model._meta.constraints
        if isinstance(constraint, models.UniqueConstraint) and constraint.fields and constraint.condition is None
    )
    for names in unique:
        lookup = {name: payload[name] for name in names}
        if (
            all(value is not None for value in lookup.values())
            and model._base_manager.using("default")
            .filter(**lookup)
            .exclude(pk=pk)
            .exclude(pk__in=deleted.get(model, ()))
            .exists()
        ):
            raise AbortRequest("A DHCP target identity conflicts with main. Nothing changed.")


def _require_servers(source_ids: set) -> None:
    from .models import Server

    present = set(Server._base_manager.using("default").filter(pk__in=source_ids).values_list("pk", flat=True))
    if source_ids - present:
        raise AbortRequest("The source Server of a DHCP mapping is missing. Nothing changed.")


def _require_delete_sources(subject, using: str) -> None:
    from django.contrib.contenttypes.models import ContentType

    from .models import KeaDhcpLink

    model = getattr(subject, "model", type(subject))
    selected = (
        subject.using(using) if hasattr(subject, "model") else model._base_manager.using(using).filter(pk=subject.pk)
    )
    if model is KeaDhcpLink:
        mappings = selected
    else:
        mappings = KeaDhcpLink.objects.using(using).filter(
            object_type=ContentType.objects.db_manager(using).get_for_model(model),
            object_id__in=selected.values_list("pk", flat=True),
        )
    _require_servers(set(mappings.values_list("server_id", flat=True)))


def _historical_targets(mappings) -> set:
    targets: set = set()
    for change in mappings:
        for payload in (change.prechange_data, change.postchange_data):
            if not payload:
                continue
            model = _mapping_target_model(payload["object_type"])
            if model not in target_models():
                raise AbortRequest("The DHCP mapping target type is unavailable. Nothing changed.")
            targets.add((model, payload["object_id"]))
    return targets


def _semantic_state(model, payload: dict) -> dict:
    from .models import KeaDhcpLink

    if model is KeaDhcpLink:
        return dict(zip(MAPPING_IDENTITY_FIELDS, mapping_identity(payload), strict=True))
    state = {key: value for key, value in payload.items() if key not in {"created", "last_updated"}}
    for field in model._meta.many_to_many:
        if field.name in state:
            state[field.name] = sorted(state[field.name] or [], key=repr)
    return state


def _validate_current_state(change, operation: str) -> None:
    if change.final_action == "skip":
        return
    model = change.model_class
    expected = change.prechange_data if operation == "merge" else change.postchange_data
    should_exist = change.final_action in (("delete", "update") if operation == "merge" else ("create", "update"))
    current = model._base_manager.using("default").filter(pk=change.key[1]).first()
    if not should_exist:
        if current is not None:
            raise AbortRequest("A DHCP mapping or target primary key has been reused. Nothing changed.")
        return
    if current is None:
        raise AbortRequest("A DHCP mapping or target from reversible history is missing. Nothing changed.")
    _validate_existing_state(current, expected)


def _validate_existing_state(current, expected) -> None:
    """Compare row generation and semantic state at each replay mutation boundary."""
    model = type(current)
    _validate_history_payload(model, expected)
    recorded_created = parse_datetime(expected["created"])
    if current.created != recorded_created:
        raise AbortRequest("A DHCP mapping or target primary key has been reused. Nothing changed.")
    if _semantic_state(model, current.serialize_object()) != _semantic_state(model, expected):
        raise AbortRequest("Main DHCP mapping or target data has changed. Nothing changed.")


def _validate_history_payload(model, payload) -> None:
    required = {
        "custom_fields" if field.name == "custom_field_data" else field.name
        for field in model._meta.concrete_fields
        if field.serialize and not field.primary_key and field.name not in {"last_updated", "last_synced"}
    }
    required.update(field.name for field in model._meta.many_to_many)
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise AbortRequest("DHCP mapping or target history is incomplete. Nothing changed.")
    try:
        generation = parse_datetime(payload["created"])
    except (TypeError, ValueError):
        generation = None
    if generation is None or timezone.is_naive(generation):
        raise AbortRequest("DHCP mapping or target generation history is incomplete. Nothing changed.")


def _precise_serialization(original):
    @wraps(original)
    def serialized(self, *args, **kwargs):
        payload = original(self, *args, **kwargs)
        for name in ("created", "last_updated", "last_synced"):
            value = getattr(self, name, None)
            if name in payload and value is not None:
                payload[name] = value.isoformat(timespec="microseconds")
        return payload

    return serialized


def _complete_history(original):
    @wraps(original)
    def recorded(self, action):
        change = original(self, action)
        if action in {"create", "delete"}:
            payload = change.postchange_data if action == "create" else change.prechange_data
            precise = self.serialize_object()
            for name in ("created", "last_updated", "last_synced"):
                if name in precise:
                    payload[name] = precise[name]
        elif type(self)._meta.label_lower == "netbox_kea.keadhcplink":
            for payload in (change.prechange_data, change.postchange_data):
                if payload is not None:
                    for name in ("last_synced", "last_updated"):
                        payload.pop(name, None)
        return change

    return recorded


def _guard_mapping_read(original):
    @wraps(original)
    def guarded(self, *args, **kwargs):
        require_branch_mappings(using=self.db)
        return original(self, *args, **kwargs)

    return guarded


def _guard_mapping_iterator(original):
    @wraps(original)
    def guarded(self, *args, **kwargs):
        require_branch_mappings(using=self.db)
        yield from original(self, *args, **kwargs)

    return guarded


def _guard_target_delete(original):
    @wraps(original)
    def guarded(self, *args, **kwargs):
        using = (
            self._db or router.db_for_write(self.model, **self._hints)
            if hasattr(self, "model")
            else (kwargs.get("using") or (args[0] if args else None) or router.db_for_write(type(self), instance=self))
        )
        require_branch_mappings(getattr(self, "model", type(self)), using=using)
        with metadata_scope(using):
            _require_delete_sources(self, using)
            return original(self, *args, **kwargs)

    return guarded


def _guard_model_write(original, using_position: int):
    @wraps(original)
    def guarded(self, *args, **kwargs):
        from .models import KeaDhcpLink

        using = kwargs.get("using") or (args[using_position] if len(args) > using_position else None)
        using = using or router.db_for_write(type(self), instance=self)
        if isinstance(self, KeaDhcpLink):
            require_branch_mappings(using=using)
        with metadata_scope(using):
            if self.pk is not None:
                persisted = type(self)._base_manager.using(using).filter(pk=self.pk).first()
                if persisted is not None:
                    persisted.snapshot()
                    self._prechange_snapshot = persisted._prechange_snapshot
            return original(self, *args, **kwargs)

    return guarded


def _guard_queryset_write(original):
    @wraps(original)
    def guarded(self, *args, **kwargs):
        from .models import KeaDhcpLink

        using = self._db or router.db_for_write(self.model, **self._hints)
        if self.model is KeaDhcpLink:
            require_branch_mappings(using=using)
        with metadata_scope(using):
            return original(self, *args, **kwargs)

    return guarded


def coordinated_import(original):
    """Enter the main writer transaction before importer selection or ownership locks."""

    @wraps(original)
    def importing(*args, **kwargs):
        branching.refuse_in_branch("A DHCP-plugin import")
        with metadata_scope():
            return original(*args, **kwargs)

    return importing


@cache
def delete_effect_models() -> set:
    """Derive deletion roots that can change a target, mapping or semantic relation."""
    from .models import KeaDhcpLink

    protected = (*target_models(), KeaDhcpLink)
    roots = set(protected)
    nonwriting = (models.PROTECT, models.RESTRICT, models.DO_NOTHING)
    for model in protected:
        for field in model._meta.concrete_fields:
            if isinstance(field, models.ForeignKey) and field.remote_field.on_delete not in nonwriting:
                roots.add(field.remote_field.model)
        roots.update(field.remote_field.model for field in model._meta.many_to_many)
    owners = list(apps.get_models())
    pending = list(roots)
    while pending:
        model = pending.pop()
        parents = set(model._meta.parents)
        parents.update(
            field.remote_field.model
            for field in model._meta.concrete_fields
            if isinstance(field, models.ForeignKey) and field.remote_field.on_delete is models.CASCADE
        )
        parents.update(
            owner
            for owner in owners
            for field in owner._meta.private_fields
            if hasattr(field, "bulk_related_objects") and field.remote_field.model is model
        )
        pending.extend(parents - roots)
        roots.update(parents)
    return roots


def _guard_parent_delete(original):
    @wraps(original)
    def guarded(self, *args, **kwargs):
        using = (
            self._db or router.db_for_write(self.model, **self._hints)
            if hasattr(self, "model")
            else (kwargs.get("using") or (args[0] if args else None) or router.db_for_write(type(self), instance=self))
        )
        with metadata_scope(using):
            return original(self, *args, **kwargs)

    return guarded


_queryset_types: dict[tuple[type, object], type] = {}
_related_manager_types: dict[type, type] = {}


def _boundary_queryset(queryset, model):
    from .models import KeaDhcpLink

    key = (queryset, model)
    if key in _queryset_types:
        return _queryset_types[key]
    protected = model in (*target_models(), KeaDhcpLink)
    methods = {}
    if model in metadata_writer_models():
        for name in ("create", "get_or_create", "update_or_create", "update", "bulk_create", "bulk_update"):
            methods[name] = _guard_queryset_write(getattr(queryset, name))
    if model is KeaDhcpLink:
        for name in ("_fetch_all", "exists", "count", "aggregate"):
            methods[name] = _guard_mapping_read(getattr(queryset, name))
        methods["iterator"] = _guard_mapping_iterator(queryset.iterator)
    methods["delete"] = (_guard_target_delete if protected else _guard_parent_delete)(queryset.delete)
    adapted = type(f"{queryset.__name__}DhcpMappingBoundary", (queryset,), methods)
    _queryset_types[key] = adapted
    _queryset_types[(adapted, model)] = adapted
    return adapted


def _guard_relation_write(original):
    @wraps(original)
    def guarded(self, *args, **kwargs):
        using = self._db or router.db_for_write(getattr(self, "through", self.model), instance=self.instance)
        with metadata_scope(using):
            return original(self, *args, **kwargs)

    return guarded


def _adapt_related_manager(manager):
    if manager in _related_manager_types:
        return _related_manager_types[manager]
    methods = {}
    for name in ("set", "add", "remove", "clear", "create", "get_or_create", "update_or_create"):
        if hasattr(manager, name):
            methods[name] = _guard_relation_write(getattr(manager, name))
    original_init = manager.__init__

    @wraps(original_init)
    def initialized(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if self.model in delete_effect_models():
            self._queryset_class = _boundary_queryset(self._queryset_class, self.model)

    methods["__init__"] = initialized
    if any("__call__" in base.__dict__ for base in manager.__mro__):
        original_call = manager.__call__

        @wraps(original_call)
        def selected(self, *args, **kwargs):
            result = original_call(self, *args, **kwargs)
            result.__class__ = _adapt_related_manager(type(result))
            if result.model in delete_effect_models():
                result._queryset_class = _boundary_queryset(result._queryset_class, result.model)
            return result

        methods["__call__"] = selected
    adapted = type(f"{manager.__name__}DhcpMappingBoundary", (manager,), methods)
    _related_manager_types[manager] = adapted
    _related_manager_types[adapted] = adapted
    return adapted


def _adapt_descriptor(owner, name):
    if not name:
        return
    descriptor = getattr(owner, name, None)
    if hasattr(descriptor, "related_manager_cls"):
        descriptor.related_manager_cls = _adapt_related_manager(descriptor.related_manager_cls)


def _register_relations(protected, roots):
    for model in protected:
        for field in model._meta.many_to_many:
            if hasattr(field, "manager"):
                field.manager = _adapt_related_manager(field.manager)
            else:
                _adapt_descriptor(model, field.name)
                _adapt_descriptor(field.remote_field.model, field.remote_field.get_accessor_name())
            m2m_changed.connect(
                _late_relation_guard,
                sender=field.remote_field.through,
                dispatch_uid=f"netbox_kea.mapping_relation.{field.remote_field.through._meta.label}",
            )
        for field in model._meta.concrete_fields:
            if isinstance(field, models.ForeignKey) and not field.remote_field.hidden:
                _adapt_descriptor(field.remote_field.model, field.remote_field.get_accessor_name())
    for owner in apps.get_models():
        for field in owner._meta.private_fields:
            if hasattr(field, "bulk_related_objects") and field.remote_field.model in roots:
                _adapt_descriptor(owner, field.name)


def _late_relation_guard(sender, instance, action, using, model, **kwargs):
    if not action.startswith("pre_"):
        return
    if instance._meta.label_lower == "extras.tag" or isinstance(instance, target_models()) or model in target_models():
        with metadata_scope(using):
            pass


def _late_delete_guard(sender, instance, using, **kwargs):
    with metadata_scope(using):
        if sender in target_models() or sender._meta.label_lower == "netbox_kea.keadhcplink":
            require_branch_mappings(sender, using=using)
            _require_delete_sources(instance, using)
            _validate_native_delete(instance, using)


def _late_save_guard(sender, instance, using, **kwargs):
    """Recheck each squash mapping or target before native save, including raw deserializer saves."""
    from django.contrib.contenttypes.models import ContentType
    from netbox.context import current_request

    from .models import KeaDhcpLink

    replay = _replay_operation.get()
    if replay is None or using != "default" or current_request.get() not in replay.native_requests:
        return
    with metadata_scope(using):
        key = (sender, instance.pk)
        for pk, payload in replay.target_associations.get(key, {}).items():
            _require_servers({payload["server"]})
            mapping = KeaDhcpLink.objects.using(using).filter(pk=pk).first()
            if mapping is None:
                raise AbortRequest("A main DHCP mapping from the selected branch is missing. Nothing changed.")
            _validate_existing_state(mapping, payload)
        if key not in replay.object_states:
            if sender is KeaDhcpLink:
                raise AbortRequest("A main DHCP mapping has no reversible branch history. Nothing changed.")
            if (
                key in replay.protected_targets
                or KeaDhcpLink.objects.using(using)
                .filter(object_type=ContentType.objects.get_for_model(instance), object_id=instance.pk)
                .exists()
            ):
                raise AbortRequest("A main DHCP mapping target has no reversible branch history. Nothing changed.")
            return
        current = sender._base_manager.using(using).filter(pk=instance.pk).first()
        expected = replay.object_states[key]
        if expected is None:
            if current is not None:
                raise AbortRequest("A DHCP mapping or target primary key has been reused. Nothing changed.")
        elif current is None:
            raise AbortRequest("A DHCP mapping or target from reversible history is missing. Nothing changed.")
        else:
            _validate_existing_state(current, expected)


def _validate_native_delete(instance, using):
    from django.contrib.contenttypes.models import ContentType

    from .models import KeaDhcpLink

    replay = _replay_operation.get()
    if replay is None or using != "default":
        return
    if isinstance(instance, KeaDhcpLink):
        _validate_recorded_mapping(instance, replay)
    else:
        key = (type(instance), instance.pk)
        mappings = KeaDhcpLink.objects.using(using).filter(
            object_type=ContentType.objects.get_for_model(instance), object_id=instance.pk
        )
        for mapping in mappings:
            _validate_recorded_mapping(mapping, replay)
        if key not in replay.destructive_targets:
            if mappings.exists():
                raise AbortRequest("A main DHCP mapping target has no reversible branch history. Nothing changed.")
            return
        current = type(instance)._base_manager.using(using).filter(pk=instance.pk).first()
        if current is None:
            raise AbortRequest("A DHCP target from reversible history is missing. Nothing changed.")
        _validate_existing_state(current, replay.object_states[key])
    if not branching.has_native_delete_receipt(replay.branch, instance):
        raise AbortRequest(
            "The native DHCP mapping deletion has no applied history in this transaction. Nothing changed."
        )


def _validate_recorded_mapping(mapping, replay):
    payload = replay.mappings.get(mapping.pk)
    if payload is None or mapping_identity(mapping) != mapping_identity(payload):
        raise AbortRequest("A main DHCP mapping has no reversible branch history. Nothing changed.")
    if mapping.created != parse_datetime(payload["created"]):
        raise AbortRequest("A DHCP mapping primary key has been reused. Nothing changed.")


def register() -> None:
    """Adapt supported model boundaries while preserving each native manager and queryset API."""
    from .models import KeaDhcpLink

    targets = target_models()
    protected = (*targets, KeaDhcpLink)
    roots = delete_effect_models()
    writers = metadata_writer_models()
    for model in roots:
        if model.__dict__.get("_kea_mapping_boundaries", False):
            continue
        model._kea_mapping_boundaries = True
        if model in protected:
            model.serialize_object = _precise_serialization(model.serialize_object)
            model.to_objectchange = _complete_history(model.to_objectchange)
        if model in writers:
            model.save = _guard_model_write(model.save, 2)
            model.save_base = _guard_model_write(model.save_base, 3)
        if model in protected:
            pre_save.connect(
                _late_save_guard, sender=model, dispatch_uid=f"netbox_kea.mapping_save.{model._meta.label}"
            )
        model.delete = (_guard_target_delete if model in protected else _guard_parent_delete)(model.delete)
        pre_delete.connect(
            _late_delete_guard, sender=model, dispatch_uid=f"netbox_kea.mapping_delete.{model._meta.label}"
        )
        managers = {
            id(manager): manager for manager in (*model._meta.managers, model._default_manager, model._base_manager)
        }
        for manager in managers.values():
            manager._queryset_class = _boundary_queryset(manager._queryset_class, model)
    _register_relations(protected, roots)

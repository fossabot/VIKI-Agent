"""Parse and evaluate profile documents against a recorded capability catalog.

Parsing validates, deep-freezes and canonicalizes input. Evaluation performs no I/O and
preserves every unverified restriction.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from viki_agent.drivers.claude.control import EFFORTS, PERMISSION_MODES
from viki_agent.llm.connections import PREFIX
from viki_agent.profiles.mcp_config import McpConfigError, check_configuration

FORMAT_VERSION = 1
MAX_CANONICAL_BYTES = 262144  # 256 KiB; schema 7 caps profile body_bytes here
MAX_RAW_BYTES = 327680  # storage's encrypted-body ceiling; also bounds whitespace-heavy JSON
MAX_NAME_BYTES = 128
MAX_PURPOSE_BYTES = 2048
MAX_INSTRUCTIONS_BYTES = 65536
MAX_STRING_BYTES = 4096
MAX_LIST_IDS = 32  # workspace IDs, context items, native references
MAX_TOOL_NAMES = 64
MAX_SAFE_INTEGER = 9007199254740991
_ACCENTS = frozenset({"blue", "green", "orange", "purple", "red", "slate"})
_SHAPES = frozenset({"circle", "rounded", "square"})
_FORBIDDEN_KEYS = frozenset({"path", "argv", "environment", "executable", "secret"})
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,255}$")
_TOP_KEYS = (
    "format_version",
    "identity",
    "runtime",
    "instructions",
    "workspace",
    "context",
    "native_assets",
    "tools",
    "memory",
    "limits",
    "provenance",
)
_IDENTITY_KEYS = ("name", "purpose", "avatar_asset_id", "accent", "shape")
_RUNTIME_KEYS = ("provider", "model", "effort", "permission_mode")
_WORKSPACE_KEYS = ("allowed_workspace_ids",)
_CONTEXT_KEYS = ("items",)
_TOOLS_KEYS = ("required", "denied", "mcp_configuration")
_MEMORY_KEYS = ("mode", "sources")
_LIMIT_KEYS = ("timeout_seconds", "max_root_tasks", "token_budget", "currency_budget")
_PROVENANCE_KEYS = ("kind", "derived_from", "native_source")


class ProfileDocumentError(Exception):
    """A malformed document: stable `code` plus dotted `field` path ("" at the root)."""

    def __init__(self, code: str, field: str = "") -> None:
        super().__init__(f"{code} at {field}" if field else code)
        self.code = code
        self.field = field


@dataclass(frozen=True)
class ProfileDocument:
    """A validated, deep-frozen document plus its canonical UTF-8 JSON bytes."""

    value: Mapping[str, object]
    canonical: bytes

    def __post_init__(self) -> None:
        try:
            plain = _to_plain(self.value)
            if not isinstance(plain, dict) or not isinstance(self.canonical, bytes):
                raise ProfileDocumentError("invalid_input")
            _walk(plain, "")
            _validate(plain)
            if _canonical(plain) != self.canonical:
                raise ProfileDocumentError("noncanonical_document")
        except (ProfileDocumentError, RecursionError) as exc:
            raise ValueError("invalid profile document") from exc
        object.__setattr__(self, "value", _freeze(plain))


@dataclass(frozen=True)
class Capability:
    """One capability row; status is exactly supported, unsupported or unverified."""

    id: str
    status: str
    reason: str | None = None
    evidence: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _CAPABILITY_STATUSES:
            raise ValueError("invalid capability status")
        _capability_code(self.id, "capability id")
        if self.reason is not None:
            _capability_code(self.reason, "capability reason")
        if self.evidence is not None:
            _capability_code(self.evidence, "capability evidence")


_CAPABILITY_STATUSES = frozenset({"supported", "unsupported", "unverified"})


@dataclass(frozen=True)
class CapabilityCatalog:
    """A recorded adapter observation: bounded rows plus the discovered models."""

    provider: str
    adapter_version: str
    native_version: str
    capabilities: Sequence[Capability]
    models: Sequence[str] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not 1 <= len(self.provider.encode("utf-8")) <= 32:
            raise ValueError("invalid provider")
        for label in (self.adapter_version, self.native_version):
            if not isinstance(label, str) or not 1 <= len(label.encode("utf-8")) <= 64:
                raise ValueError("invalid version")
        if len(self.capabilities) > 64 or len(self.models) > 128:
            raise ValueError("catalog too large")
        if not all(isinstance(cap, Capability) for cap in self.capabilities):
            raise ValueError("capabilities must hold Capability rows")
        seen: set[str] = set()
        for capability in self.capabilities:
            if capability.status not in _CAPABILITY_STATUSES:
                raise ValueError(f"bad capability status: {capability.status!r}")
            if capability.id in seen:
                raise ValueError(f"duplicate capability id: {capability.id!r}")
            seen.add(capability.id)
        seen_models: set[str] = set()
        for model in self.models:
            _bounded_catalog_text(model, "model")
            if model in seen_models:
                raise ValueError(f"duplicate model: {model!r}")
            seen_models.add(model)
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "models", tuple(self.models))


def parse_profile_document(raw: object) -> ProfileDocument:
    """Parse and freeze one closed document v1.

    Accepts JSON text (str/bytes) with duplicate keys and NaN/Infinity rejected, or an
    already-decoded mapping. Keys must be strings; `path`, `argv`, `environment`,
    `executable` and `secret` are refused at any depth.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw_size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
        except UnicodeEncodeError as exc:
            raise ProfileDocumentError("invalid_json", "") from exc
        if raw_size > MAX_RAW_BYTES:
            raise ProfileDocumentError("document_too_large", "")
        try:
            plain: object = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
        except ProfileDocumentError:
            raise
        except (ValueError, RecursionError) as exc:
            raise ProfileDocumentError("invalid_json", "") from exc
    elif isinstance(raw, Mapping):
        try:
            plain = _to_plain(raw)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ProfileDocumentError("invalid_input", "") from exc
    else:
        raise ProfileDocumentError("invalid_input", "")
    if not isinstance(plain, dict):
        raise ProfileDocumentError("invalid_input", "")
    try:
        _walk(plain, "")
        _validate(plain)
        value = cast(Mapping[str, object], _freeze(plain))
        canonical = _canonical(plain)
    except ProfileDocumentError:
        raise
    except RecursionError as exc:
        raise ProfileDocumentError("invalid_input", "") from exc
    return ProfileDocument(value=value, canonical=canonical)


def catalog_capability(catalog: CapabilityCatalog, capability_id: str) -> Capability | None:
    for capability in catalog.capabilities:
        if capability.id == capability_id:
            return capability
    return None


@dataclass(frozen=True)
class FieldEvaluation:
    """One stable preview row: field, preview class, status and value source."""

    field: str
    preview_class: str
    status: str  # supported | unsupported | unverified
    effective_value: object
    source: str  # profile | inherited
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _CAPABILITY_STATUSES:
            raise ValueError("invalid field status")


@dataclass(frozen=True)
class ProfileEvaluation:
    """The full per-field judgment; `blockers` decides `launchable`."""

    fields: tuple[FieldEvaluation, ...]
    blockers: tuple[str, ...]
    launchable: bool

    def __post_init__(self) -> None:
        expected = tuple(row.field for row in self.fields if row.status != "supported")
        if self.blockers != expected or self.launchable != (not expected):
            raise ValueError("inconsistent profile evaluation")


def evaluate_profile(
    document: ProfileDocument,
    catalog: CapabilityCatalog | None = None,
) -> ProfileEvaluation:
    """Judge every field; pure (no I/O, no provider calls, no defaults invented).

    Profiles may store with blockers; only start-preview requires a launchable document.
    """
    value = document.value
    rows: list[FieldEvaluation] = []

    def add(
        field_name: str,
        preview_class: str,
        status: str,
        effective_value: object,
        source: str,
        reason: str | None = None,
    ) -> None:
        rows.append(
            FieldEvaluation(field_name, preview_class, status, effective_value, source, reason)
        )

    def list_row(field_name: str, preview_class: str, items: object, code: str) -> None:
        if items:
            add(field_name, preview_class, "unsupported", items, "profile", code)
        else:
            add(field_name, preview_class, "supported", items, "profile")

    identity = cast(Mapping[str, object], value["identity"])
    for key in _IDENTITY_KEYS:
        add(f"identity.{key}", "display_only", "supported", identity[key], "profile")
    add("instructions", "task_input", "supported", value["instructions"], "profile")

    runtime = cast(Mapping[str, object], value["runtime"])
    provider = runtime["provider"]
    if provider == "claude":
        add("runtime.provider", "native_effective", "supported", provider, "profile")
    else:
        add(
            "runtime.provider",
            "native_effective",
            "unsupported",
            provider,
            "profile",
            "provider_unsupported",
        )

    applicable = (
        catalog if provider == "claude" and catalog and catalog.provider == provider else None
    )
    model = runtime["model"]
    model_capability = catalog_capability(applicable, "model_selection") if applicable else None
    if model is None:
        add("runtime.model", "native_effective", "supported", None, "inherited")
    elif isinstance(model, str) and model.startswith(PREFIX):
        add(
            "runtime.model",
            "native_effective",
            "unverified",
            model,
            "profile",
            "connection_resolution_required",
        )
    elif (
        applicable
        and model_capability
        and model_capability.status == "supported"
        and model in applicable.models
    ):
        add("runtime.model", "native_effective", "supported", model, "profile")
    elif model_capability and model_capability.status != "supported":
        add(
            "runtime.model",
            "native_effective",
            model_capability.status,
            model,
            "profile",
            model_capability.reason or f"model_selection_{model_capability.status}",
        )
    else:
        add(
            "runtime.model",
            "native_effective",
            "unverified",
            model,
            "profile",
            "model_unobserved",
        )

    effort_capability = catalog_capability(applicable, "effort_selection") if applicable else None
    if effort_capability and effort_capability.status == "supported":
        add("runtime.effort", "native_effective", "supported", runtime["effort"], "profile")
    else:
        status = effort_capability.status if effort_capability else "unverified"
        add(
            "runtime.effort",
            "native_effective",
            status,
            runtime["effort"],
            "profile",
            (effort_capability.reason if effort_capability else None) or f"effort_{status}",
        )

    permission_mode = runtime["permission_mode"]
    plan_capability = catalog_capability(applicable, "plan_mode") if applicable else None
    if permission_mode is None:
        add("runtime.permission_mode", "native_effective", "supported", None, "inherited")
    elif permission_mode == "plan" and plan_capability and plan_capability.status == "supported":
        add("runtime.permission_mode", "native_effective", "supported", "plan", "profile")
    elif permission_mode == "plan" and plan_capability:
        add(
            "runtime.permission_mode",
            "native_effective",
            plan_capability.status,
            "plan",
            "profile",
            plan_capability.reason or f"plan_mode_{plan_capability.status}",
        )
    else:
        add(
            "runtime.permission_mode",
            "native_effective",
            "unverified",
            permission_mode,
            "profile",
            "permission_unverified",
        )

    workspace = cast(Mapping[str, object], value["workspace"])
    allowed = workspace["allowed_workspace_ids"]
    if allowed:
        add(
            "workspace.allowed_workspace_ids",
            "enforced_restriction",
            "unverified",
            allowed,
            "profile",
            "workspace_resolution_required",
        )
    else:
        add(
            "workspace.allowed_workspace_ids",
            "enforced_restriction",
            "supported",
            allowed,
            "inherited",
        )

    context = cast(Mapping[str, object], value["context"])
    list_row("context.items", "task_input", context["items"], "context_unresolved")
    list_row("native_assets", "unsupported", value["native_assets"], "native_asset_unresolved")

    tools = cast(Mapping[str, object], value["tools"])
    list_row("tools.required", "enforced_restriction", tools["required"], "required_tools_unproven")
    list_row("tools.denied", "enforced_restriction", tools["denied"], "denied_tools_unproven")

    mcp_configuration = tools["mcp_configuration"]
    if mcp_configuration is None:
        add("tools.mcp_configuration", "native_effective", "supported", None, "inherited")
    else:
        try:
            check_configuration(mcp_configuration)
        except McpConfigError:
            add(
                "tools.mcp_configuration",
                "native_effective",
                "unsupported",
                mcp_configuration,
                "profile",
                "mcp_config_invalid",
            )
        else:
            mcp_capability = catalog_capability(applicable, "mcp_config") if applicable else None
            if mcp_capability and mcp_capability.status == "supported":
                add(
                    "tools.mcp_configuration",
                    "native_effective",
                    "supported",
                    mcp_configuration,
                    "profile",
                )
            else:
                add(
                    "tools.mcp_configuration",
                    "native_effective",
                    "unsupported",
                    mcp_configuration,
                    "profile",
                    "mcp_config_unproven",
                )

    memory = cast(Mapping[str, object], value["memory"])
    if memory["mode"] == "provider_inherited":
        add("memory.mode", "native_effective", "supported", memory["mode"], "inherited")
    else:
        add(
            "memory.mode",
            "native_effective",
            "unsupported",
            memory["mode"],
            "profile",
            "memory_isolation_unproven",
        )
    list_row("memory.sources", "unsupported", memory["sources"], "memory_isolation_unproven")

    limits = cast(Mapping[str, object], value["limits"])
    limit_rows = (
        ("timeout_seconds", "enforced_restriction", "timeout_unproven"),
        ("max_root_tasks", "enforced_restriction", "max_root_tasks_unproven"),
        ("token_budget", "unsupported", "token_budget_unproven"),
        ("currency_budget", "unsupported", "currency_budget_unproven"),
    )
    for key, preview_class, code in limit_rows:
        bound = limits[key]
        if bound is None:
            add(f"limits.{key}", preview_class, "supported", None, "inherited")
        else:
            add(f"limits.{key}", preview_class, "unsupported", bound, "profile", code)

    provenance = cast(Mapping[str, object], value["provenance"])
    for key in _PROVENANCE_KEYS:
        add(f"provenance.{key}", "display_only", "supported", provenance[key], "profile")

    field_rows = tuple(rows)
    blockers = tuple(row.field for row in field_rows if row.status != "supported")
    return ProfileEvaluation(fields=field_rows, blockers=blockers, launchable=not blockers)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise ProfileDocumentError("duplicate_key", key)
        result[key] = item
    return result


def _reject_constant(name: str) -> object:
    raise ProfileDocumentError("invalid_number", "")


def _to_plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(item) for item in value]
    return value


def _walk(value: object, path: str) -> None:
    """Reject non-string keys, forbidden keys, surrogates and non-JSON leaves."""
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise ProfileDocumentError("invalid_type", f"{path}.<key>" if path else "<key>")
            if key in _FORBIDDEN_KEYS:
                raise ProfileDocumentError("forbidden_key", _at(path, key))
        for key, item in value.items():
            _walk(item, _at(path, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProfileDocumentError("invalid_value", path) from exc
    elif value is None or isinstance(value, bool):
        return
    elif isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ProfileDocumentError("invalid_number", path)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ProfileDocumentError("invalid_number", path)
        raise ProfileDocumentError("invalid_type", path)
    else:
        raise ProfileDocumentError("invalid_type", path)


def _at(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _validate(doc: dict[str, object]) -> None:
    _require_keys(doc, _TOP_KEYS, "")
    version = doc["format_version"]
    if isinstance(version, bool):
        raise ProfileDocumentError("invalid_type", "format_version")
    if not isinstance(version, int) or version != FORMAT_VERSION:
        raise ProfileDocumentError("invalid_value", "format_version")

    identity = _object(doc, "identity")
    _require_keys(identity, _IDENTITY_KEYS, "identity")
    _string(identity["name"], "identity.name", 1, MAX_NAME_BYTES)
    _string(identity["purpose"], "identity.purpose", 0, MAX_PURPOSE_BYTES)
    _nullable_uuid(identity["avatar_asset_id"], "identity.avatar_asset_id")
    _enum(identity["accent"], "identity.accent", _ACCENTS)
    _enum(identity["shape"], "identity.shape", _SHAPES)

    runtime = _object(doc, "runtime")
    _require_keys(runtime, _RUNTIME_KEYS, "runtime")
    _string(runtime["provider"], "runtime.provider", 1, MAX_STRING_BYTES)
    _nullable_string(runtime["model"], "runtime.model", MAX_STRING_BYTES)
    _enum(runtime["effort"], "runtime.effort", EFFORTS)
    if runtime["permission_mode"] is not None:
        _enum(runtime["permission_mode"], "runtime.permission_mode", PERMISSION_MODES)

    _string(doc["instructions"], "instructions", 0, MAX_INSTRUCTIONS_BYTES)

    workspace = _object(doc, "workspace")
    _require_keys(workspace, _WORKSPACE_KEYS, "workspace")
    _id_list(workspace["allowed_workspace_ids"], "workspace.allowed_workspace_ids")

    context = _object(doc, "context")
    _require_keys(context, _CONTEXT_KEYS, "context")
    _object_list(context["items"], "context.items", MAX_LIST_IDS)

    _object_list(doc["native_assets"], "native_assets", MAX_LIST_IDS)

    tools = _object(doc, "tools")
    _require_keys(tools, _TOOLS_KEYS, "tools")
    _tool_list(tools["required"], "tools.required")
    _tool_list(tools["denied"], "tools.denied")
    if tools["mcp_configuration"] is not None and not isinstance(tools["mcp_configuration"], dict):
        raise ProfileDocumentError("invalid_type", "tools.mcp_configuration")

    memory = _object(doc, "memory")
    _require_keys(memory, _MEMORY_KEYS, "memory")
    _string(memory["mode"], "memory.mode", 1, MAX_STRING_BYTES)
    _object_list(memory["sources"], "memory.sources", None)

    limits = _object(doc, "limits")
    _require_keys(limits, _LIMIT_KEYS, "limits")
    for key in _LIMIT_KEYS:
        _limit(limits[key], f"limits.{key}")

    provenance = _object(doc, "provenance")
    _require_keys(provenance, _PROVENANCE_KEYS, "provenance")
    _string(provenance["kind"], "provenance.kind", 1, MAX_STRING_BYTES)
    _nullable_object(provenance["derived_from"], "provenance.derived_from")
    _nullable_object(provenance["native_source"], "provenance.native_source")


def _object(doc: dict[str, object], key: str) -> dict[str, object]:
    value = doc[key]
    if not isinstance(value, dict):
        raise ProfileDocumentError("invalid_type", key)
    return value


def _require_keys(container: Mapping[str, object], keys: tuple[str, ...], path: str) -> None:
    for key in keys:
        if key not in container:
            raise ProfileDocumentError("missing_key", _at(path, key))
    for key in container:
        if key not in keys:
            raise ProfileDocumentError("extra_key", _at(path, key))


def _string(value: object, path: str, min_bytes: int, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ProfileDocumentError("invalid_type", path)
    if not min_bytes <= len(value.encode("utf-8")) <= max_bytes:
        raise ProfileDocumentError("invalid_string_length", path)
    return value


def _nullable_string(value: object, path: str, max_bytes: int) -> None:
    if value is not None:
        _string(value, path, 1, max_bytes)


def _enum(value: object, path: str, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ProfileDocumentError("invalid_enum", path)


def _uuid(value: object, path: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ProfileDocumentError("invalid_uuid", path)
    try:
        if str(uuid.UUID(value)) != value:
            raise ProfileDocumentError("invalid_uuid", path)
    except ValueError:
        raise ProfileDocumentError("invalid_uuid", path) from None
    return value


def _nullable_uuid(value: object, path: str) -> None:
    if value is not None:
        _uuid(value, path)


def _nullable_object(value: object, path: str) -> None:
    if value is not None and not isinstance(value, dict):
        raise ProfileDocumentError("invalid_type", path)


def _id_list(value: object, path: str) -> None:
    if not isinstance(value, list):
        raise ProfileDocumentError("invalid_type", path)
    if len(value) > MAX_LIST_IDS:
        raise ProfileDocumentError("invalid_value", path)
    for index, item in enumerate(value):
        _uuid(item, f"{path}[{index}]")


def _object_list(value: object, path: str, cap: int | None) -> None:
    """A list of plain objects (typed references); cap is a list-length bound."""
    if not isinstance(value, list):
        raise ProfileDocumentError("invalid_type", path)
    if cap is not None and len(value) > cap:
        raise ProfileDocumentError("invalid_value", path)
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ProfileDocumentError("invalid_type", f"{path}[{index}]")


def _tool_list(value: object, path: str) -> None:
    if not isinstance(value, list):
        raise ProfileDocumentError("invalid_type", path)
    if len(value) > MAX_TOOL_NAMES:
        raise ProfileDocumentError("invalid_value", path)
    seen: set[str] = set()
    for index, item in enumerate(value):
        name = _string(item, f"{path}[{index}]", 1, MAX_STRING_BYTES)
        if name in seen:
            raise ProfileDocumentError("invalid_value", f"{path}[{index}]")
        seen.add(name)


def _limit(value: object, path: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileDocumentError("invalid_type", path)
    if not 1 <= value <= MAX_SAFE_INTEGER:
        raise ProfileDocumentError("invalid_value", path)


def _canonical(doc: dict[str, object]) -> bytes:
    text = json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    data = text.encode("utf-8")
    if len(data) > MAX_CANONICAL_BYTES:
        raise ProfileDocumentError("canonical_too_large", "")
    return data


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _bounded_catalog_text(value: object, label: str) -> None:
    try:
        valid = isinstance(value, str) and 1 <= len(value.encode("utf-8")) <= MAX_STRING_BYTES
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise ValueError(f"invalid {label}")


def _capability_code(value: object, label: str) -> None:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")

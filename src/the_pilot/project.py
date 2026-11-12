"""Validate project overlays and resolve their pure, project-neutral defaults.

Only the installer discovers or reads files. This module accepts JSON values
and returns independent copies; concurrent compilations cannot change defaults.
"""
from __future__ import annotations

import copy
from typing import Any

from .defaults import DEFAULT_SPECS, RESERVED_COMMA_ALIASES, is_entry_key

PROJECT_SCHEMA = "the-pilot-project/v1"


class ProfileError(ValueError):
    """Invalid preferences or project contract; no installation may be written."""


def _merge(base: dict, overlay: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def normalize_project(raw: Any) -> dict[str, Any]:
    """Reject unknown fields, invalid content, and scene path escapes."""
    if not isinstance(raw, dict) or raw.get("schema") != PROJECT_SCHEMA:
        raise ProfileError(f"project.schema must be {PROJECT_SCHEMA}")
    allowed = {"schema", "projectId", "description", *DEFAULT_SPECS}
    if set(raw) - allowed:
        raise ProfileError(f"Unknown project fields: {sorted(set(raw) - allowed)}")
    if not isinstance(raw.get("projectId"), str) or not raw["projectId"].strip():
        raise ProfileError("projectId must be a nonempty string")
    if "description" in raw and not isinstance(raw["description"], str):
        raise ProfileError("project.description must be a string")
    for field in set(DEFAULT_SPECS) - {"entry_order"}:
        if field in raw and not isinstance(raw[field], dict):
            raise ProfileError(f"project.{field} must be an object")
    normalized = copy.deepcopy(raw)
    for name, preset in (normalized.get("presets") or {}).items():
        if not isinstance(preset, dict):
            raise ProfileError(f"Invalid preset: {name}")
        if "pilot" in preset and "autopilot" in preset:
            raise ProfileError(f"project.presets.{name} 同时包含 pilot 与旧版 autopilot 段")
        if "autopilot" in preset:
            preset["pilot"] = preset.pop("autopilot")
    specs = _merge(DEFAULT_SPECS, {k: v for k, v in normalized.items() if k in DEFAULT_SPECS})
    order = specs["entry_order"]
    if (not isinstance(order, list) or not all(isinstance(k, str) for k in order)
            or len(set(order)) != len(order) or set(order) != set(specs["entry_specs"])):
        raise ProfileError("entry_order must list every entry spec exactly once")
    identities = set()
    for key, spec in specs["entry_specs"].items():
        if not is_entry_key(key) or key in RESERVED_COMMA_ALIASES:
            raise ProfileError(f"Invalid or reserved entry key: {key}")
        required = {"command_id", "description", "requires_arg", "usage", "template"}
        if not isinstance(spec, dict) or not required <= set(spec) or set(spec) - required - {"workflow_type"}:
            raise ProfileError(f"Invalid entry spec: {key}")
        if not isinstance(spec["requires_arg"], bool):
            raise ProfileError(f"{key}.requires_arg must be boolean")
        for field in set(spec) - {"requires_arg"}:
            if not isinstance(spec[field], str) or not spec[field].strip():
                raise ProfileError(f"{key}.{field} must be nonempty text")
        if spec["command_id"] in identities:
            raise ProfileError("Project entry command IDs must be unique")
        identities.add(spec["command_id"])
    for field in ("presets", "scene_specs", "deliver_content", "feedback_closeout"):
        if set(specs[field]) != set(DEFAULT_SPECS[field]):
            raise ProfileError(f"project.{field} has unsupported keys")
    for name, spec in specs["scene_specs"].items():
        default = DEFAULT_SPECS["scene_specs"][name]
        if not isinstance(spec, dict) or set(spec) != set(default):
            raise ProfileError(f"Invalid scene spec: {name}")
        for field, value in spec.items():
            if type(value) is not type(default[field]):
                raise ProfileError(f"Invalid scene field: {name}.{field}")
        if spec["file"] != default["file"]:
            raise ProfileError("Built-in scene filenames cannot be changed")
    for field in ("deliver_content", "feedback_closeout"):
        for key, spec in specs[field].items():
            expected = DEFAULT_SPECS[field][key]
            if (not isinstance(spec, dict) or set(spec) != set(expected)
                    or not all(isinstance(v, str) for v in spec.values())):
                raise ProfileError(f"Invalid template fields: {field}.{key}")
    for name, preset in specs["presets"].items():
        if not isinstance(preset, dict) or set(preset) - {"orchestration", "pilot", "scenes", "shortcuts"}:
            raise ProfileError(f"Invalid preset: {name}")
        if not all(isinstance(value, dict) for value in preset.values()):
            raise ProfileError(f"Preset sections must be objects: {name}")
    return normalized


def project_specs(project: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve one overlay without sharing mutable state with the caller."""
    if project is None:
        return copy.deepcopy(DEFAULT_SPECS)
    overlay = normalize_project(project)
    return _merge(DEFAULT_SPECS, {k: v for k, v in overlay.items() if k in DEFAULT_SPECS})

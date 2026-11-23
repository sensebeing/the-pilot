"""Validate project overlays and resolve their pure, project-neutral defaults.

This module accepts JSON values and returns independent copies. The runtime
owns the executable shortcut contract and discovers overlays per workspace.
"""
from __future__ import annotations

import copy
from typing import Any

from .assets.runtime import validate_project_entries
from .defaults import DEFAULT_SPECS


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
    try:
        entries = validate_project_entries(raw)
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc
    normalized = copy.deepcopy(raw)
    if "entry_specs" in normalized:
        normalized["entry_specs"] = entries
    for name, preset in (normalized.get("presets") or {}).items():
        if not isinstance(preset, dict):
            raise ProfileError(f"Invalid preset: {name}")
        if "pilot" in preset and "autopilot" in preset:
            raise ProfileError(f"project.presets.{name} 同时包含 pilot 与旧版 autopilot 段")
        if "autopilot" in preset:
            preset["pilot"] = preset.pop("autopilot")
    specs = _merge(DEFAULT_SPECS, {k: v for k, v in normalized.items() if k in DEFAULT_SPECS})
    for field in ("presets", "scene_specs", "deliver_content"):
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
    for key, spec in specs["deliver_content"].items():
        expected = DEFAULT_SPECS["deliver_content"][key]
        if (not isinstance(spec, dict) or set(spec) != set(expected)
                or not all(isinstance(v, str) for v in spec.values())):
            raise ProfileError(f"Invalid template fields: deliver_content.{key}")
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
    specs = _merge(DEFAULT_SPECS, {k: v for k, v in overlay.items() if k in DEFAULT_SPECS})
    specs["entry_order"].extend(key for key in DEFAULT_SPECS["entry_order"] if key not in specs["entry_order"])
    return specs

"""Normalize durable user preferences."""
from __future__ import annotations

import copy
import re
from typing import Any

from .defaults import COMMA_ALIAS_RE, DELIVER_STRATEGIES, ENTRY_KEY_RE, MODES, PROFILE_SCHEMA, RESERVED_COMMA_ALIASES, REVIEW_AGENT_ORDER, SCENE_ORDER, WORKER_AGENT_ORDER
from .project import ProfileError, project_specs


def resolve_profile(
    base: dict[str, Any],
    *,
    preset: str | None = "recommended",
    overrides: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a base profile with one preset and the caller's overrides."""
    specs = project_specs(project)
    presets = specs["presets"]
    base_copy = _copy_mapping(base, "base profile")
    merged = base_copy
    if preset is not None:
        if preset not in presets:
            raise ProfileError(f"未知预设 {preset!r}；允许的预设为 {sorted(presets)}")
        merged = _deep_merge(merged, presets[preset])
    if overrides:
        merged = _deep_merge(merged, _copy_mapping(overrides, "profile overrides"))
    return validate_profile(merged, project=project)


def validate_profile(raw: Any, *, project: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a normalized copy of ``raw`` or raise ``ProfileError``."""
    specs = project_specs(project)
    profile = _copy_mapping(raw, "profile")
    _reject_unknown(
        profile,
        ("schema", "orchestration", "autopilot", "scenes", "shortcuts"),
        "profile",
    )
    if profile.get("schema", PROFILE_SCHEMA) != PROFILE_SCHEMA:
        raise ProfileError(
            f"profile.schema 必须是 {PROFILE_SCHEMA}，实际为 {profile.get('schema')!r}"
        )
    orchestration = _require_mapping(profile.get("orchestration"), "orchestration")
    autopilot = _require_mapping(profile.get("autopilot"), "autopilot")
    scenes = _require_mapping(profile.get("scenes"), "scenes")
    shortcuts = _require_mapping(profile.get("shortcuts"), "shortcuts")
    _reject_unknown(orchestration, ("worker_agents", "review_agents"), "orchestration")
    _reject_unknown(autopilot, ("mode", "max_consecutive_auto_turns"), "autopilot")
    _reject_unknown(
        scenes,
        ("enabled_scenes", "deliver_strategy", "enable_feedback_email", "guardrails"),
        "scenes",
    )
    _reject_unknown(
        shortcuts,
        ("enabled_entry", "custom_entry", "custom_comma", "template_overrides"),
        "shortcuts",
    )

    mode = autopilot.get("mode")
    if mode not in MODES:
        raise ProfileError(f"autopilot.mode 必须是 {MODES} 之一，实际为 {mode!r}")
    max_turns = autopilot.get("max_consecutive_auto_turns")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not 1 <= max_turns <= 50:
        raise ProfileError("autopilot.max_consecutive_auto_turns 必须是 1..50 的整数")

    strategy = scenes.get("deliver_strategy")
    if strategy not in DELIVER_STRATEGIES:
        raise ProfileError(
            f"scenes.deliver_strategy 必须是 {DELIVER_STRATEGIES} 之一，实际为 {strategy!r}"
        )
    feedback_email = scenes.get("enable_feedback_email")
    if not isinstance(feedback_email, bool):
        raise ProfileError("scenes.enable_feedback_email 必须是布尔值")
    guardrails = _string_list(
        scenes.get("guardrails"), "scenes.guardrails", allow_empty=True
    )
    enabled_scenes = _string_list(
        scenes.get("enabled_scenes"),
        "scenes.enabled_scenes",
        allow_empty=True,
        allowed=SCENE_ORDER,
    )
    worker_agents = _string_list(
        orchestration.get("worker_agents"),
        "orchestration.worker_agents",
        allowed=WORKER_AGENT_ORDER,
    )
    review_agents = _string_list(
        orchestration.get("review_agents"),
        "orchestration.review_agents",
        allowed=REVIEW_AGENT_ORDER,
    )
    enabled_entry = _string_list(
        shortcuts.get("enabled_entry"),
        "shortcuts.enabled_entry",
        allow_empty=True,
        allowed=specs['entry_order'],
    )
    custom_entry = _normalize_custom_entries(shortcuts.get("custom_entry"), project=project)
    custom_comma, custom_aliases = _normalize_custom_comma(shortcuts.get("custom_comma"), enabled_scenes)
    entry_aliases = {f",{key}" for key in enabled_entry}
    entry_aliases |= {f",{key}" for key in custom_entry}
    for alias in custom_aliases:
        if alias in entry_aliases:
            raise ProfileError(f"自定义口令 {alias} 与开局口令冲突")
    override_aliases: dict[str, str] = {}
    for key in enabled_entry:
        canonical = key
        override_aliases[key] = canonical
        override_aliases[f",{key}"] = canonical
        override_aliases[specs['entry_specs'][key]["command_id"]] = canonical
    for key, spec in custom_entry.items():
        canonical = key
        override_aliases[key] = canonical
        override_aliases[f",{key}"] = canonical
        override_aliases[spec["command_id"]] = canonical
    for key, spec in custom_comma.items():
        canonical = key
        override_aliases[key] = canonical
        override_aliases[f",{key}"] = canonical
        override_aliases[spec["command_id"]] = canonical
        for alias in spec["aliases"]:
            override_aliases[str(alias).lower()] = canonical
    template_overrides = _normalize_overrides(
        shortcuts.get("template_overrides"), override_aliases
    )
    return {
        "schema": PROFILE_SCHEMA,
        "orchestration": {
            "worker_agents": worker_agents,
            "review_agents": review_agents,
        },
        "autopilot": {"mode": mode, "max_consecutive_auto_turns": max_turns},
        "scenes": {
            "enabled_scenes": [item for item in SCENE_ORDER if item in enabled_scenes],
            "deliver_strategy": strategy,
            "enable_feedback_email": feedback_email,
            "guardrails": guardrails,
        },
        "shortcuts": {
            "enabled_entry": [item for item in specs['entry_order'] if item in enabled_entry],
            "custom_entry": {key: custom_entry[key] for key in sorted(custom_entry)},
            "custom_comma": {key: custom_comma[key] for key in sorted(custom_comma)},
            "template_overrides": {
                key: template_overrides[key] for key in sorted(template_overrides)
            },
        },
    }


def _normalize_custom_entries(value: Any, *, project: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    specs = project_specs(project)
    if value is None:
        return {}
    mapping = _require_mapping(value, "shortcuts.custom_entry")
    entries: dict[str, dict[str, Any]] = {}
    command_ids: dict[str, str] = {}
    for key, item in mapping.items():
        if not isinstance(key, str) or not ENTRY_KEY_RE.match(key):
            raise ProfileError(f"custom_entry 的键 {key!r} 必须匹配 {ENTRY_KEY_RE.pattern}")
        if key in specs['entry_order']:
            raise ProfileError(f"custom_entry 不能覆盖内置开局口令 {key!r}；请使用 template_overrides")
        if key in RESERVED_COMMA_ALIASES:
            raise ProfileError(f"custom_entry 的键 {key!r} 会生成保留口令 ,{key}")
        if isinstance(item, str):
            item = {"template": item}
        spec = _require_mapping(item, f"custom_entry[{key!r}]")
        _reject_unknown(
            spec,
            ("template", "description", "usage", "requires_arg", "command_id"),
            f"custom_entry[{key!r}]",
        )
        template = str(spec.get("template") or "").strip()
        if not template:
            raise ProfileError(f"custom_entry[{key!r}].template 不能为空")
        requires_arg = spec.get("requires_arg", "{arg}" in template)
        if not isinstance(requires_arg, bool):
            raise ProfileError(f"custom_entry[{key!r}].requires_arg 必须是布尔值")
        command_id = f"CMD_CUSTOM_ENTRY_{key.upper().replace('-', '_')}"
        if command_id in command_ids:
            raise ProfileError(
                f"custom_entry 命令身份冲突：{key!r} 与 {command_ids[command_id]!r}"
            )
        provided = str(spec.get("command_id") or "")
        if provided and provided != command_id:
            raise ProfileError(
                f"custom_entry[{key!r}].command_id 必须为 {command_id}"
            )
        command_ids[command_id] = key
        entries[key] = {
            "command_id": command_id,
            "description": str(spec.get("description") or f"自定义开局口令 `{key}`"),
            "requires_arg": requires_arg,
            "usage": str(spec.get("usage") or f"{key} [参数]"),
            "template": template,
        }
    return entries


def _normalize_custom_comma(
    value: Any, enabled_scenes: list[str]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    if value is None:
        return {}, set()
    mapping = _require_mapping(value, "shortcuts.custom_comma")
    commands: dict[str, dict[str, Any]] = {}
    aliases: set[str] = set()
    command_ids: dict[str, str] = {}
    for raw_alias, item in mapping.items():
        alias = normalize_comma_alias(raw_alias)
        if alias in aliases:
            raise ProfileError(f"custom_comma 别名重复：{alias}")
        if isinstance(item, str):
            item = {"template": item}
        spec = _require_mapping(item, f"custom_comma[{raw_alias!r}]")
        _reject_unknown(
            spec,
            ("template", "title", "aliases", "scene", "command_id"),
            f"custom_comma[{raw_alias!r}]",
        )
        template = str(spec.get("template") or "").strip()
        if not template:
            raise ProfileError(f"custom_comma[{raw_alias!r}].template 不能为空")
        scene = str(spec.get("scene") or "")
        if scene and scene not in enabled_scenes:
            raise ProfileError(
                f"custom_comma[{raw_alias!r}].scene 必须是已启用的场景，实际为 {scene!r}"
            )
        extra = spec.get("aliases") or []
        if not isinstance(extra, list):
            raise ProfileError(f"custom_comma[{raw_alias!r}].aliases 必须是字符串数组")
        all_aliases = [alias]
        seen = {alias}
        for entry in extra:
            normalized = normalize_comma_alias(entry)
            if normalized in seen:
                if normalized == alias:
                    continue
                raise ProfileError(f"custom_comma 别名重复：{normalized}")
            seen.add(normalized)
            all_aliases.append(normalized)
        for entry in all_aliases:
            if entry in aliases:
                raise ProfileError(f"custom_comma 别名重复：{entry}")
        aliases.update(all_aliases)
        command_id = f"CMD_CUSTOM_COMMA_{alias.lstrip(',').upper().replace('-', '_')}"
        if command_id in command_ids:
            raise ProfileError(
                f"custom_comma 命令身份冲突：{alias} 与 {command_ids[command_id]}"
            )
        provided = str(spec.get("command_id") or "")
        if provided and provided != command_id:
            raise ProfileError(
                f"custom_comma[{raw_alias!r}].command_id 必须为 {command_id}"
            )
        command_ids[command_id] = alias
        commands[alias.lstrip(",")] = {
            "command_id": command_id,
            "aliases": all_aliases,
            "scene": scene,
            "title": str(spec.get("title") or f"自定义口令 {alias}"),
            "template": template,
        }
    return commands, aliases


def normalize_comma_alias(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ProfileError("自定义逗号口令必须是非空字符串")
    text = raw.strip().lower()
    if not text.startswith(","):
        text = "," + text
    name = text[1:]
    if name in RESERVED_COMMA_ALIASES:
        raise ProfileError(f"口令 {text} 是保留别名（,1 / ,2 / ,3 / ,mode / ,worker / ,agent）")
    if not COMMA_ALIAS_RE.match(name):
        raise ProfileError(f"口令别名 {raw!r} 只能包含小写字母、数字、下划线和横杠")
    return text


def _normalize_overrides(value: Any, aliases: dict[str, str]) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _require_mapping(value, "shortcuts.template_overrides")
    overrides: dict[str, str] = {}
    for raw_key, item in mapping.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ProfileError("template_overrides 的键必须是非空字符串")
        key = raw_key.strip()
        normalized_key = normalize_comma_alias(key) if key.startswith(",") else key
        lookup = key if key in aliases else normalized_key
        canonical = aliases.get(lookup)
        if canonical is None:
            raise ProfileError(f"template_overrides 指向不存在的口令 {raw_key!r}")
        if not isinstance(item, str) or not item.strip():
            raise ProfileError(f"template_overrides[{raw_key!r}] 必须是非空字符串")
        template = item.strip()
        if canonical in overrides and overrides[canonical] != template:
            raise ProfileError(
                f"template_overrides 的多个键指向同一口令 {canonical} 且模板不同"
            )
        overrides[canonical] = template
    return overrides


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _copy_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{where} 必须是 JSON 对象")
    return copy.deepcopy(value)


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{where} 必须是 JSON 对象")
    return value


def _string_list(
    value: Any,
    where: str,
    *,
    allowed: tuple[str, ...] | None = None,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ProfileError(f"{where} 必须是非空字符串数组")
    items = [item.strip() for item in value]
    if len(set(items)) != len(items):
        raise ProfileError(f"{where} 不能包含重复项")
    if allowed is not None:
        unknown = [item for item in items if item not in allowed]
        if unknown:
            raise ProfileError(f"{where} 含未知项 {unknown}；允许的值为 {list(allowed)}")
    if not items and not allow_empty:
        raise ProfileError(f"{where} 至少需要一项")
    return items


def _reject_unknown(mapping: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ProfileError(f"{where} 含未知字段 {unknown}；允许的字段为 {list(allowed)}")

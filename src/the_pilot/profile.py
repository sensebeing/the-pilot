"""Normalize durable user preferences."""
from __future__ import annotations

import copy
from typing import Any

from .defaults import is_comma_alias, DELIVER_STRATEGIES, is_entry_key, LEGACY_PROFILE_SCHEMA, MODES, PROFILE_SCHEMA, RESERVED_COMMA_ALIASES, REVIEW_AGENT_ORDER, SCENE_ORDER, WORKER_AGENT_ORDER
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
    merged = _normalize_pilot_section(_copy_mapping(base, "base profile"))
    if preset is not None:
        if preset not in presets:
            raise ProfileError(f"未知预设 {preset!r}；允许的预设为 {sorted(presets)}")
        merged = _deep_merge(merged, presets[preset])
    if overrides:
        merged = _deep_merge(
            merged,
            _normalize_pilot_section(_copy_mapping(overrides, "profile overrides")),
        )
    return validate_profile(merged, project=project)


def _shortcut_override_aliases(
    shortcuts: dict[str, Any], specs: dict[str, Any]
) -> dict[str, str]:
    """Map every alias ``template_overrides`` accepts to its canonical key.

    Accepts fresh normalized sections or raw installed profile state. Values
    that cannot be read contribute no alias here; validation reports them with
    a precise error instead of silently treating them as absent shortcuts.
    """
    aliases: dict[str, str] = {}

    def add(alias: str, canonical: str) -> None:
        if alias:
            aliases.setdefault(alias, canonical)

    enabled_entry = shortcuts.get("enabled_entry")
    if isinstance(enabled_entry, list):
        for key in enabled_entry:
            spec = specs["entry_specs"].get(key) if isinstance(key, str) else None
            if not isinstance(spec, dict):
                continue
            add(key, key)
            add(f",{key}", key)
            add(str(spec.get("command_id") or ""), key)
    custom_entry = shortcuts.get("custom_entry")
    if isinstance(custom_entry, dict):
        for key, spec in custom_entry.items():
            if not isinstance(key, str):
                continue
            add(key, key)
            add(f",{key}", key)
            add(_custom_entry_command_id(key, spec), key)
    custom_comma = shortcuts.get("custom_comma")
    if isinstance(custom_comma, dict):
        for raw_key, spec in custom_comma.items():
            if not isinstance(raw_key, str):
                continue
            try:
                normalized_key = normalize_comma_alias(raw_key)
            except ProfileError:
                continue
            key = normalized_key[1:]
            add(key, key)
            add(normalized_key, key)
            add(_custom_comma_command_id(key, spec), key)
            if isinstance(spec, dict):
                for alias in spec.get("aliases") or ():
                    if not isinstance(alias, str):
                        continue
                    try:
                        normalized_alias = normalize_comma_alias(alias)
                    except ProfileError:
                        continue
                    add(normalized_alias, key)
    return aliases


def _custom_entry_command_id(key: str, spec: Any = None) -> str:
    provided = spec.get("command_id") if isinstance(spec, dict) else None
    return str(provided) if provided else (
        f"CMD_CUSTOM_ENTRY_{key.upper().replace('-', '_')}"
    )


def _custom_comma_command_id(alias: str, spec: Any = None) -> str:
    provided = spec.get("command_id") if isinstance(spec, dict) else None
    return str(provided) if provided else (
        f"CMD_CUSTOM_COMMA_{alias.lstrip(',').upper().replace('-', '_')}"
    )


def migrate_project_shortcuts(
    base: dict[str, Any],
    installed_project: dict[str, Any] | None,
    project: dict[str, Any] | None,
    *,
    explicit_entries: list[str] | None = None,
) -> dict[str, Any]:
    """Keep installed shortcut state valid when the effective project changes.

    Reusing an installed profile must preserve every personal preference while
    the project tier adds, removes, renames or rebinds shortcut entries. Custom
    shortcuts that a new overlay now owns become built-in entries, entries the
    user already decided about keep their decision, and template overrides
    follow the shortcut identity they were written for.
    """
    migrated = copy.deepcopy(base)
    shortcuts = migrated.get("shortcuts")
    if not isinstance(shortcuts, dict):
        return migrated
    explicit_entry_override = explicit_entries is not None
    previous_specs = project_specs(installed_project)
    effective_specs = project_specs(project)
    previous_order = set(previous_specs["entry_order"])
    entry_order_set = set(effective_specs["entry_order"])
    retired_overlay_entries = previous_order - entry_order_set
    previous_aliases = _shortcut_override_aliases(shortcuts, previous_specs)
    enabled_entry = shortcuts.get("enabled_entry")
    custom_entry = shortcuts.get("custom_entry")

    # A custom shortcut whose key now names a built-in entry is promoted, so
    # the user keeps using the same word for the overlay-provided workflow.
    if isinstance(custom_entry, dict):
        for key in list(custom_entry):
            if key not in entry_order_set:
                continue
            del custom_entry[key]
            if (
                not explicit_entry_override
                and isinstance(enabled_entry, list)
                and key not in enabled_entry
            ):
                enabled_entry.append(key)

    if retired_overlay_entries and not explicit_entry_override and isinstance(
        enabled_entry, list
    ):
        enabled_entry[:] = [
            key for key in enabled_entry if key not in retired_overlay_entries
        ]

    # Recommended entries that a newly attached overlay introduces are adopted;
    # keys the user already had stay exactly as enabled or disabled.
    if not explicit_entry_override and isinstance(enabled_entry, list):
        recommended_enabled = effective_specs["presets"]["recommended"]["shortcuts"].get(
            "enabled_entry", ()
        )
        for key in recommended_enabled:
            if (
                key in entry_order_set
                and key not in previous_order
                and key not in enabled_entry
            ):
                enabled_entry.append(key)

    # Overrides follow the shortcut that owned them in the installed profile,
    # not whichever shortcut happens to reuse their alias next.
    template_overrides = shortcuts.get("template_overrides")
    if isinstance(template_overrides, dict):
        shortcuts["template_overrides"] = _reconcile_template_overrides(
            template_overrides,
            shortcuts,
            previous_aliases,
            effective_specs,
            explicit_entries if explicit_entries is not None else enabled_entry,
            retired_overlay_entries,
        )
    return migrated


def _reconcile_template_overrides(
    template_overrides: dict[Any, Any],
    shortcuts: dict[str, Any],
    previous_aliases: dict[str, str],
    effective_specs: dict[str, Any],
    effective_enabled_entry: Any,
    retired_overlay_entries: set[str],
) -> dict[Any, Any]:
    """Rebind installed overrides to the shortcut that owned them before.

    An alias can be reused by a different entry after a project switch, so a
    raw key is resolved against the installed profile first. Overrides whose
    previous owner retired are dropped, overrides whose owner survived are
    re-keyed to that owner, and genuinely invalid keys are left in place for
    validate_profile to reject.
    """
    surviving_aliases = _shortcut_override_aliases(
        {**shortcuts, "enabled_entry": effective_enabled_entry}, effective_specs
    )
    surviving_targets = set(surviving_aliases.values())
    passthrough: dict[Any, Any] = {}
    canonical_overrides: dict[str, str] = {}
    for raw_key, item in template_overrides.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            passthrough[raw_key] = item
            continue
        try:
            previous_owner = _resolve_override_alias(raw_key, previous_aliases)
            new_owner = _resolve_override_alias(raw_key, surviving_aliases)
        except ProfileError:
            passthrough[raw_key] = item
            continue
        if previous_owner is None:
            passthrough[raw_key] = item
        elif (
            previous_owner in retired_overlay_entries
            and previous_owner not in surviving_targets
        ):
            if not isinstance(item, str) or not item.strip():
                passthrough[raw_key] = item
        elif previous_owner not in surviving_targets:
            if new_owner is not None:
                passthrough[previous_owner] = item
            else:
                passthrough[raw_key] = item
        else:
            _record_override(canonical_overrides, previous_owner, raw_key, item)
    return {**passthrough, **canonical_overrides}


def validate_profile(raw: Any, *, project: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a normalized copy of ``raw`` or raise ``ProfileError``."""
    specs = project_specs(project)
    profile = _copy_mapping(raw, "profile")
    _reject_unknown(
        profile,
        ("schema", "orchestration", "pilot", "autopilot", "scenes", "shortcuts"),
        "profile",
    )
    if profile.get("schema", PROFILE_SCHEMA) not in {PROFILE_SCHEMA, LEGACY_PROFILE_SCHEMA}:
        raise ProfileError(
            f"profile.schema 必须是 {PROFILE_SCHEMA}（兼容旧版 {LEGACY_PROFILE_SCHEMA}），"
            f"实际为 {profile.get('schema')!r}"
        )
    profile = _normalize_pilot_section(profile)
    orchestration = _require_mapping(profile.get("orchestration"), "orchestration")
    pilot = _require_mapping(profile.get("pilot"), "pilot")
    scenes = _require_mapping(profile.get("scenes"), "scenes")
    shortcuts = _require_mapping(profile.get("shortcuts"), "shortcuts")
    _reject_unknown(orchestration, ("worker_agents", "review_agents"), "orchestration")
    _reject_unknown(pilot, ("mode", "max_consecutive_auto_turns"), "pilot")
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

    mode = pilot.get("mode")
    if mode not in MODES:
        raise ProfileError(f"pilot.mode 必须是 {MODES} 之一，实际为 {mode!r}")
    max_turns = pilot.get("max_consecutive_auto_turns")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not 1 <= max_turns <= 50:
        raise ProfileError("pilot.max_consecutive_auto_turns 必须是 1..50 的整数")

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
    override_aliases = _shortcut_override_aliases(
        {
            "enabled_entry": enabled_entry,
            "custom_entry": custom_entry,
            "custom_comma": custom_comma,
        },
        specs,
    )
    template_overrides = _normalize_overrides(
        shortcuts.get("template_overrides"), override_aliases
    )
    return {
        "schema": PROFILE_SCHEMA,
        "orchestration": {
            "worker_agents": worker_agents,
            "review_agents": review_agents,
        },
        "pilot": {"mode": mode, "max_consecutive_auto_turns": max_turns},
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


def _normalize_pilot_section(profile: dict[str, Any]) -> dict[str, Any]:
    """Accept a legacy ``autopilot`` section once, then emit only ``pilot``.

    A mapping that carries both keys is ambiguous: two owners would decide the
    same mode and turn cap, so the install fails closed instead of guessing.
    """
    has_pilot = "pilot" in profile
    has_legacy = "autopilot" in profile
    if has_pilot and has_legacy:
        raise ProfileError("profile 同时包含 pilot 与旧版 autopilot 段；请只保留 pilot")
    if has_legacy:
        profile["pilot"] = profile.pop("autopilot")
    return profile


def _normalize_custom_entries(value: Any, *, project: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    specs = project_specs(project)
    if value is None:
        return {}
    mapping = _require_mapping(value, "shortcuts.custom_entry")
    entries: dict[str, dict[str, Any]] = {}
    command_ids: dict[str, str] = {}
    for key, item in mapping.items():
        if not isinstance(key, str) or not is_entry_key(key):
            raise ProfileError(f"custom_entry 的键 {key!r} 必须匹配 ASCII 小写字母开头，后接小写字母、数字、下划线或连字符")
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
        command_id = _custom_entry_command_id(key)
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
        command_id = _custom_comma_command_id(alias)
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
        reserved = " / ".join(f",{item}" for item in sorted(RESERVED_COMMA_ALIASES))
        raise ProfileError(f"口令 {text} 是保留别名（{reserved}）")
    if not is_comma_alias(name):
        raise ProfileError(f"口令别名 {raw!r} 只能包含小写字母、数字、下划线和横杠")
    return text


def _resolve_override_alias(raw_key: str, aliases: dict[str, str]) -> str | None:
    """Resolve one ``template_overrides`` key to its canonical shortcut key.

    Whitespace and comma-alias case are normalized exactly as validation does,
    so hand-edited installed state and project-switch migration share one
    resolution rule.
    """
    key = raw_key.strip()
    normalized_key = normalize_comma_alias(key) if key.startswith(",") else key
    lookup = key if key in aliases else normalized_key
    return aliases.get(lookup)


def _record_override(
    overrides: dict[str, str], canonical: str, raw_key: Any, item: Any
) -> None:
    if not isinstance(item, str) or not item.strip():
        raise ProfileError(f"template_overrides[{raw_key!r}] 必须是非空字符串")
    template = item.strip()
    if canonical in overrides and overrides[canonical] != template:
        raise ProfileError(
            f"template_overrides 的多个键指向同一口令 {canonical} 且模板不同"
        )
    overrides[canonical] = template


def _normalize_overrides(value: Any, aliases: dict[str, str]) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _require_mapping(value, "shortcuts.template_overrides")
    overrides: dict[str, str] = {}
    for raw_key, item in mapping.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ProfileError("template_overrides 的键必须是非空字符串")
        canonical = _resolve_override_alias(raw_key, aliases)
        if canonical is None:
            raise ProfileError(f"template_overrides 指向不存在的口令 {raw_key!r}")
        _record_override(overrides, canonical, raw_key, item)
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

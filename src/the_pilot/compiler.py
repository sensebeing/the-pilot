"""Pure in-memory compilation of profiles and project overlays."""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .defaults import (
    AGENT_LABELS,
    COORDINATOR_AGENTS,
    GEMINI_BEGIN,
    GEMINI_END,
    HARNESS_ORCHESTRATION_SENTENCE,
    HOOK_GROUP,
    LEGACY_GEMINI_BEGIN,
    LEGACY_GEMINI_END,
    LEGACY_HOOK_GROUP,
    LEGACY_SCENES_BEGIN,
    LEGACY_SCENES_END,
    ORCHESTRATION_BEGIN,
    ORCHESTRATION_END,
    SCENES_BEGIN,
    SCENES_END,
    SHELL_ENV_BEGIN,
    SHELL_ENV_END,
    UNMANAGED_SCENES_MARKERS_ERROR,
)
from .project import ProfileError, project_specs
from .profile import _copy_mapping, validate_profile

LEGACY_ORCHESTRATION_HEADING = "## 子 Agent 与多任务编排规范（Orca Orchestration："


@dataclass(frozen=True)
class CompiledBundle:
    """Everything one install needs, already rendered in memory."""

    profile_json: dict[str, Any]
    config_json: dict[str, Any]
    scenes: dict[str, str]
    gemini_block: str


def compile_bundle(
    profile: dict[str, Any],
    *, project: dict[str, Any] | None = None,
) -> CompiledBundle:
    """Compile one validated profile into every file the installer writes."""
    specs = project_specs(project)
    normalized = validate_profile(profile, project=project)
    entry_shortcuts = build_entry_shortcuts(normalized, project=project)
    comma_commands = build_comma_commands(normalized, project=project)
    scenes = {
        specs['scene_specs'][name]["file"]: _render_scene_file(name, normalized, project=project)
        for name in normalized["scenes"]["enabled_scenes"]
    }
    config: dict[str, Any] = {
        "entry_shortcuts": entry_shortcuts,
        "comma_commands": comma_commands,
    }
    return CompiledBundle(
        profile_json=normalized,
        config_json=config,
        scenes=scenes,
        gemini_block=render_gemini_block(normalized, scenes),
    )


def build_entry_shortcuts(profile: dict[str, Any], *, project: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    specs = project_specs(project)
    overrides = profile["shortcuts"]["template_overrides"]
    enabled = set(profile["shortcuts"]["enabled_entry"])
    shortcuts: dict[str, dict[str, Any]] = {}
    for key in specs['entry_order']:
        if key not in enabled:
            continue
        spec = specs['entry_specs'][key]
        template = _override(overrides, key) or spec["template"]
        shortcuts[key] = {
            "command_id": spec["command_id"],
            "canonical_key": key,
            "description": spec["description"],
            "requires_arg": spec["requires_arg"],
            "usage": spec["usage"],
            "template": template,
            **({"workflow_type": spec["workflow_type"]} if "workflow_type" in spec else {}),
        }
    for key in sorted(profile["shortcuts"]["custom_entry"]):
        spec = profile["shortcuts"]["custom_entry"][key]
        template = _override(overrides, key) or spec["template"]
        shortcuts[key] = {
            "command_id": spec["command_id"],
            "canonical_key": key,
            "description": spec["description"],
            "requires_arg": spec["requires_arg"],
            "usage": spec["usage"],
            "template": template,
        }
    return shortcuts


def build_comma_commands(profile: dict[str, Any], *, project: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    specs = project_specs(project)
    overrides = profile["shortcuts"]["template_overrides"]
    commands: dict[str, dict[str, Any]] = {}
    for key, shortcut in build_entry_shortcuts(profile, project=project).items():
        command_id = shortcut["command_id"]
        if command_id in commands:
            raise ProfileError(f"命令身份冲突：{command_id}")
        commands[command_id] = {
            "command_id": command_id,
            "canonical_key": key,
            "aliases": [f",{key}"],
            "scene": "",
            "title": shortcut["description"],
            "template": shortcut["template"],
            **({"workflow_type": shortcut["workflow_type"]} if "workflow_type" in shortcut else {}),
        }
    for alias in sorted(profile["shortcuts"]["custom_comma"]):
        spec = profile["shortcuts"]["custom_comma"][alias]
        template = _override(overrides, alias) or spec["template"]
        command_id = spec["command_id"]
        if command_id in commands:
            raise ProfileError(f"命令身份冲突：{command_id}")
        commands[command_id] = {
            "command_id": command_id,
            "canonical_key": alias,
            "aliases": list(spec["aliases"]),
            "scene": spec["scene"],
            "title": spec["title"],
            "template": template,
        }
    _assert_unique_aliases(commands)
    return commands


def render_gemini_block(profile: dict[str, Any], scenes: dict[str, str]) -> str:
    parts = [
        GEMINI_BEGIN,
        render_orchestration_section(profile),
        render_pilot_section(profile, len(scenes)),
        SCENES_BEGIN,
        render_scenes_block(scenes),
        SCENES_END,
        GEMINI_END,
    ]
    return "\n\n".join(part for part in parts if part) + "\n"


def render_orchestration_section(profile: dict[str, Any]) -> str:
    worker_agents = profile["orchestration"]["worker_agents"]
    review_agents = profile["orchestration"]["review_agents"]
    coordinator = (
        profile["orchestration"].get("coordinator_agent") or COORDINATOR_AGENTS[0]
    )
    coordinator_label = AGENT_LABELS[coordinator]
    priority = " > ".join(AGENT_LABELS[item] for item in worker_agents)
    primary = worker_agents[0]
    secondary = worker_agents[1] if len(worker_agents) > 1 else ""
    executor = f"优先使用 {AGENT_LABELS[primary]}（`--agent {primary}`）"
    if secondary:
        executor += f"，其次使用 {AGENT_LABELS[secondary]}（`--agent {secondary}`）"
    secondary_hint = f"次选 `--agent {secondary}`；" if secondary else ""
    review_priority = " > ".join(AGENT_LABELS[item] for item in review_agents)
    return f"""{LEGACY_ORCHESTRATION_HEADING}{priority}）

编排前执行 `orca skills get orchestration`，遵循技能及按需 references。

1. **职责分离**：
   - **Tier-1 主会话（Coordinator，无 Orca Task ID / Dispatch ID preamble）**：原生 **{coordinator_label}**（`antigravity`），直接 `agy --dangerously-skip-permissions`，账户池由 Orca 与 `agy-switch` 管理，禁用 `magpie agy`。顶层工作区先登记到 `~/.gemini/antigravity-cli/settings.json` 的 `trustedWorkspaces`。**严禁以 `claude`/`codex` 启动或降级充当 Tier-1 主会话**。{HARNESS_ORCHESTRATION_SENTENCE}。排障查同链路历史与端到端拓扑，禁叠黑白名单、局部重试或特判。
   - **Tier-2 子会话（Dispatched Worker，有 Orca live Task ID / Dispatch ID preamble）**：除极简单改动，实施交 Orca worker。独立分支修改、定向验证、提交并报 `worker_done`；禁止嵌套派工。
2. **Orca 编排（执行器优先级：{priority}）**：禁用 `invoke_subagent` 和无监督裸终端。先 `orca status --json`，再 `orca orchestration run-create --objective "<目标>" --json` 绑定 Run。{executor}。Task spec 自包含 Target、Contracts、Files、Boundaries、Ownership、Acceptance，载明业务目标、Anchor 0、最简模型及「未经主会话 Review 不得合入或推送 `main`」。
3. **工作区层级与回调**：
   - 同工作区 `worker-start --spec "<任务契约>" --worktree current --agent {primary} --json`（{secondary_hint}或 `task-create` 后带 `--task <task_id>`）。
   - 同仓子工作区 `worker-start --spec "<任务契约>" --worktree new-child --name <name> --agent {primary} --setup run --json`（{secondary_hint}`orca worktree create` 预建须指定 `--parent-worktree active` 或 `path:<当前路径>`）。
   - 跨仓回传 `worker-start --spec "<任务契约>" --worktree new-child --repo path:<目标仓库根> --name <name> --agent {primary} --setup run --json`，`worker_done` 唤醒主会话。
   - 禁止平级 `--worktree new-top-level`/`--no-parent`。仅用户明确要求独立长期 Tier-1 可用 `--no-parent`（原生 {coordinator_label}），需回执则绑定 Dealer thread 或 `orca orchestration send`。
4. **交互与验收**：
   - `orca orchestration check --wait --types "worker_done,escalation,question" --timeout-ms 900000 --json` 等回执，`reply`/`send` 裁决和指导，`--ack` 确认消费。
   - `worker_done` 后主会话亲审：对照业务目标与 Anchor 0 蓝图，核查真实 diff 与改动后模块全貌。按 `architecture-re-review` 重读起点、盘点现状、零基对账，并检查 `peq.*`（唯一真源、无循环依赖/函数内联导入/行内抑制、职责、测试、门禁）。独立架构审查优先级：**`{review_priority}`**。
   - 未达标用 `worker-show` 取已验证终端，建整改 Task，`worker-start --worktree path:<子工作区路径> --terminal <handle>` 派回；通过后同终端 follow-up 通知合入并推送 `main`。结算后 `worker-release`（禁用 `orca terminal close` 替代），确认 `worker-list --terminal-state reclaimable` 无遗留后清理子工作区及已合入分支。"""


def render_orchestration_block(profile: dict[str, Any]) -> str:
    """Wrap the orchestration rules in the managed block used by agent files."""
    return "\n\n".join(
        (ORCHESTRATION_BEGIN, render_orchestration_section(profile), ORCHESTRATION_END)
    ) + "\n"


def render_shell_env_block() -> str:
    """Disable AGY's built-in OSC 7501 emitter by default."""
    return (
        f"{SHELL_ENV_BEGIN}\n"
        'export AGY_CLI_OSC7501="${AGY_CLI_OSC7501:-off}"\n'
        f"{SHELL_ENV_END}\n"
    )


def merge_shell_env(existing: str, block: str | None = None) -> str:
    """Replace the managed block or append it after the user's shell text."""
    bounds = _shell_env_block_bounds(existing)
    rendered = render_shell_env_block() if block is None else block
    if not rendered.endswith("\n"):
        rendered += "\n"
    if bounds is not None:
        start, end = bounds
        return existing[:start] + rendered + existing[end:]
    if not existing or existing.endswith(("\n\n", "\n\r\n")):
        return existing + rendered
    if existing.endswith("\n"):
        newline = "\r\n" if existing.endswith("\r\n") else "\n"
        return existing + newline + rendered
    return existing + "\n\n" + rendered


def strip_shell_env(existing: str) -> str:
    """Remove the managed block and trim blank lines only at the seam."""
    bounds = _shell_env_block_bounds(existing)
    if bounds is None:
        return existing
    start, end = bounds
    left = existing[:start].rstrip("\r\n")
    right = existing[end:].lstrip("\r\n")
    if not left:
        return right
    newline = "\r\n" if existing.startswith("\r\n", len(left)) else "\n"
    return left + (newline * 2 + right if right else newline)


def _shell_env_block_bounds(text: str) -> tuple[int, int] | None:
    """Locate the two complete marker lines, including the closing newline."""
    begins = _find_all_positions(text, SHELL_ENV_BEGIN)
    ends = _find_all_positions(text, SHELL_ENV_END)
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise ValueError("THE_PILOT_SHELL_ENV 托管标记必须恰好各出现一次且顺序正确")
    start = begins[0]
    end = ends[0] + len(SHELL_ENV_END)
    for offset, marker in ((start, SHELL_ENV_BEGIN), (ends[0], SHELL_ENV_END)):
        after = offset + len(marker)
        if (offset and text[offset - 1] != "\n") or (
            after < len(text) and not text.startswith(("\n", "\r\n"), after)
        ):
            raise ValueError("THE_PILOT_SHELL_ENV 托管标记必须独占一行")
    if text.startswith("\r\n", end):
        end += 2
    elif text.startswith("\n", end):
        end += 1
    return start, end


def merge_orchestration_md(existing: str, block: str) -> str:
    """Replace our managed orchestration block, migrating one legacy section."""
    text = existing or ""
    bounds = _orchestration_block_bounds(text)
    rendered = block if block.endswith("\n") else block + "\n"
    if bounds is None:
        legacy = _legacy_orchestration_heading_positions(text)
        if len(legacy) > 1:
            raise ValueError(
                "Agent 全局指引存在多处未托管的「## 子 Agent 与多任务编排规范"
                "（Orca Orchestration：」章节，无法安全迁移；请只保留一处后重试"
            )
        if legacy:
            start = legacy[0]
            end = _markdown_section_end(text, start)
            prefix = text[:start].rstrip("\n")
            suffix = text[end:].lstrip("\n")
            parts = [part for part in (prefix, rendered.rstrip("\n"), suffix.rstrip("\n")) if part]
            return "\n\n".join(parts) + "\n"
        if not text.strip():
            return rendered
        return text.rstrip("\n") + "\n\n" + rendered
    start, end = bounds
    suffix = text[end:]
    if suffix.startswith("\n"):
        suffix = suffix[1:]
    merged = text[:start] + rendered + suffix
    return merged if merged.endswith("\n") else merged + "\n"


def strip_orchestration_md(existing: str) -> str:
    """Remove our managed orchestration block and tidy only its seam."""
    text = existing or ""
    bounds = _orchestration_block_bounds(text)
    if bounds is None:
        return text
    start, end = bounds
    left = text[:start].rstrip("\n")
    right = text[end:].lstrip("\n")
    if left and right:
        return left + "\n\n" + right.rstrip("\n") + "\n"
    remainder = left or right
    return remainder.rstrip("\n") + "\n" if remainder else ""


def _orchestration_block_bounds(text: str) -> tuple[int, int] | None:
    """Return the managed orchestration block, failing closed on ambiguity."""
    begins = _find_all_positions(text, ORCHESTRATION_BEGIN)
    ends = _find_all_positions(text, ORCHESTRATION_END)
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        raise ValueError(
            "Agent 全局指引的 THE_PILOT_ORCHESTRATION 托管标记必须恰好各出现一次且顺序正确"
        )
    return begins[0], ends[0] + len(ORCHESTRATION_END)


def _legacy_orchestration_heading_positions(text: str) -> list[int]:
    """Return offsets of the pre-managed orchestration heading outside fences."""
    positions: list[int] = []
    fence: str | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if fence is None:
            if stripped.startswith(("```", "~~~")):
                fence = stripped[:3]
            elif line.startswith(LEGACY_ORCHESTRATION_HEADING):
                positions.append(offset)
        elif stripped.startswith(fence):
            fence = None
        offset += len(line)
    return positions


def _markdown_section_end(text: str, start: int) -> int:
    """Return where the level-2 section starting at ``start`` ends, outside fences."""
    fence: str | None = None
    offset = start
    for index, line in enumerate(text[start:].splitlines(keepends=True)):
        stripped = line.strip()
        if index and fence is None and (line.startswith("# ") or line.startswith("## ")):
            return offset
        if fence is None:
            if stripped.startswith(("```", "~~~")):
                fence = stripped[:3]
        elif stripped.startswith(fence):
            fence = None
        offset += len(line)
    return len(text)


def render_pilot_section(profile: dict[str, Any], scene_count: int) -> str:
    max_turns = profile["pilot"]["max_consecutive_auto_turns"]
    if scene_count:
        routing = (
            f"回合结束按下方 {scene_count} 个场景路由"
            "（需等外部输入或已结案则输出 `scene=NONE`），构造"
            "**一句自然、贴合上下文、不重复已完成动作**的下一步指令"
            "（展示与预填同源）。"
        )
    else:
        routing = (
            "本机未启用场景；回合结束只输出 `<!-- pilot: scene=NONE -->`，"
            "不做自动预填。"
        )
    return f"""## 个人研发状态机与 Turn Pilot 规范（Agent 单一语义 Owner 三步判断模型）

仅对**主会话（Coordinator，无 Orca Task ID / Dispatch ID preamble）**生效，子会话忽略。模式真源 `profile.json`，用 `,mode status|gate|confirm|off` 切换；`gate` 连续自动推进上限 {max_turns} 轮。引擎仅预填与回车，**主会话裁决意图、场景与 `gate=auto|pause`**：

1. **第一步：任务性质二分类**：
   - **前置铁律**：先查同链路近 7~14 天修复史与端到端拓扑，判断唯一 owner 和最简根治路径；复发不得叠黑白名单、局部重试或表象补丁。
   - **A 类 — 确定性收敛**：正确行为与边界已由产品设计、模块契约、`peq.*`、事实或当前明确授权唯一决定，不改变未获授权的安全门禁或跨模块/外部合同边界；含内部根因修复、授权内冗余清理、工具回执、并发/超时及质量收敛。`gate=auto` 按实际进度逐步推进（定位 → 出详细方案 → 方案级架构 review → 编排实施），不得跳过方案设计或方案复审，不重复提问。
   - **B 类 — 方案与设计裁决**：0→1 新功能/机制/工作流、未定义产品/交互取舍，或未经确认改变安全门禁、内部跨模块/外部合同及状态机边界。按 AGENTS.md 与 `peq.architecture-simplicity` 说明不变量与替代保护，最简方案置首标 `(Recommended)`，同步澄清直接调 `ask_question`（同一步不写标签，禁止停在悬空 `：`），异步`提问未答`输出 `scene=NONE`；所有进度均沿用此边界：B 类方案/裁决未确认标 `gate=pause`，已确认边界内 `gate=auto` 单步递进，prod 发布、未授权真实 E2E 除外。
   - **简化边界**：历史提交/合同不能豁免最简架构；说明不变量、复发成因及唯一 owner 的替代保护，授权边界按 A/B 分类，不重复裁决。
2. **第二步：场景与 Prompt（真源 `~/.gemini/config/the-pilot/scenes/*.md`）**：{routing}
3. **第三步：正文可见建议与唯一隐藏标签**：正文必须显示 `- **,1（主推）**：<主推文本>`，可选 `- **,2（备选）**：<备选文本>`、`- **,3（备选）**：<备选文本>`，不可只藏在注释里。
   - 正文仅供阅读；回复末尾唯一 JSON 标签是执行真源，由 `json.loads` 解析：`<!-- pilot: {{"scene": "<场景名>", "gate": "auto|pause", "1": "<主推文本>", "2": "<备选文本>"}} -->`（无备选时省略 "2"）。主推非空；候选只读标签，不解析或交叉校验 Markdown；文本须合法 JSON 转义。
   - 场景缺失/未知/多标签或 gate 缺失/非法则暂停；标签非法、重复或字段冲突则清空建议。`gate=auto` 仅在场景默认 auto 且为 A 类或已确认边界内的 B 类时使用；B 类方案未确认或需人工确认时标 `gate=pause`。异步提问未答或结案输出 `<!-- pilot: {{"scene": "NONE"}} -->`（兼容 `<!-- pilot: scene=NONE -->`）。
   - 暂停/确认时可输入 `,1 [补充说明]` 触发主推，与 `,2 [补充说明]` 对称。

场景全文真源 `scenes/*.md`；改动后 `pilot sync`。"""


def render_scenes_block(scenes: dict[str, str]) -> str:
    entries: list[tuple[int, str, str]] = []
    for filename, text in scenes.items():
        meta, body = split_frontmatter(text)
        name = str(meta.get("name") or "")
        if not name:
            continue
        order = int(meta.get("order") or 0)
        gate = "auto" if bool(meta.get("auto_in_gate_mode", True)) else "pause"
        heading = (
            f"### 场景 `{name}` — {meta.get('title') or name}\n"
            f"- 触发：{meta.get('description') or ''}\n"
            f"- 门禁：{gate}\n\n"
            f"{body.strip()}"
        )
        entries.append((order, name, heading))
    if not entries:
        return "（未启用场景：本机只使用 Orca 编排规则，不做回合结束预填。）"
    entries.sort(key=lambda item: (item[0], item[1]))
    return "\n\n".join(item[2] for item in entries)


def _assert_scenes_markers_managed(
    text: str, bounds: tuple[int, int] | None
) -> None:
    """Reject scene markers the tool did not write.

    Only the managed profile block may own `THE_PILOT_SCENES` (or the legacy
    `AUTOPILOT SCENES` block): appending a second, managed scenes segment
    around a legacy manual one leaves two blocks that both the truncating CLI
    and the runtime `sync` treat as one, so the install fails closed and says
    what to clean up first.
    """
    outside = text if bounds is None else text[: bounds[0]] + text[bounds[1] :]
    markers = (
        SCENES_BEGIN,
        SCENES_END,
        LEGACY_SCENES_BEGIN,
        LEGACY_SCENES_END,
    )
    if any(marker in outside for marker in markers):
        raise ValueError(UNMANAGED_SCENES_MARKERS_ERROR)


def _find_all_positions(text: str, marker: str) -> list[int]:
    positions: list[int] = []
    start = text.find(marker)
    while start >= 0:
        positions.append(start)
        start = text.find(marker, start + len(marker))
    return positions


def merge_gemini_md(existing: str, block: str) -> str:
    """Replace our managed block in ``existing`` or append it, preserving the rest."""
    text = existing or ""
    bounds = _block_bounds(text)
    _assert_scenes_markers_managed(text, bounds)
    rendered = block if block.endswith("\n") else block + "\n"
    if bounds is None:
        if not text.strip():
            return rendered
        return text.rstrip("\n") + "\n\n" + rendered
    start, end = bounds
    suffix = text[end:]
    if suffix.startswith("\n"):
        suffix = suffix[1:]
    merged = text[:start] + rendered + suffix
    return merged if merged.endswith("\n") else merged + "\n"


def strip_gemini_md(existing: str) -> str:
    """Remove our managed block and tidy only the seam around it."""
    text = existing or ""
    bounds = _block_bounds(text)
    if bounds is None:
        return text
    start, end = bounds
    left = text[:start].rstrip("\n")
    right = text[end:].lstrip("\n")
    if left and right:
        return left + "\n\n" + right.rstrip("\n") + "\n"
    remainder = left or right
    return remainder.rstrip("\n") + "\n" if remainder else ""


def merge_hooks_json(existing: dict[str, Any], engine_path: str) -> dict[str, Any]:
    """Install our hook group without touching the registry's other groups.

    The legacy `orca-agy-autopilot` group is removed in the same write: leaving
    both groups installed would run every hook twice.
    """
    registry = _copy_mapping(existing, "hooks registry") if existing else {}
    quoted_engine = shlex.quote(str(Path(engine_path).expanduser().resolve()))
    commands = {
        event: [
            {
                "type": "command",
                "command": f"python3 {quoted_engine} hook --event {event}",
                "timeout": 10,
            }
        ]
        for event in ("PreInvocation", "Stop")
    }
    registry.pop(LEGACY_HOOK_GROUP, None)
    registry[HOOK_GROUP] = commands
    return registry


def strip_hooks_json(existing: dict[str, Any]) -> dict[str, Any]:
    registry = _copy_mapping(existing, "hooks registry") if existing else {}
    registry.pop(HOOK_GROUP, None)
    registry.pop(LEGACY_HOOK_GROUP, None)
    return registry


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Read the small YAML subset used by scene files, without a YAML dependency."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        value = raw.strip()
        if value.startswith('"'):
            try:
                meta[key.strip()] = json.loads(value)
            except ValueError:
                meta[key.strip()] = value.strip('"')
        elif value in ("true", "false"):
            meta[key.strip()] = value == "true"
        else:
            meta[key.strip()] = value
    return meta, text[end + 5 :]


def _render_scene_file(name: str, profile: dict[str, Any], *, project: dict[str, Any] | None = None) -> str:
    specs = project_specs(project)
    spec = specs['scene_specs'][name]
    strategy = profile["scenes"]["deliver_strategy"]
    deliver = specs['deliver_content'][strategy]
    feedback_enabled = profile["scenes"]["enable_feedback_email"]
    guardrails = profile["scenes"]["guardrails"]
    description = str(spec["description"])
    description = description.replace("{{DELIVER_COMPLETION}}", deliver["completion"])
    description = description.replace(
        "{{FEEDBACK_TRIGGER}}", specs["feedback_closeout"][str(feedback_enabled).lower()]["trigger"]
    )
    fallback = str(spec["fallback_prompt"]).replace(
        "{{DELIVER_FALLBACK_PROMPT}}", deliver["fallback"]
    )
    body = str(spec["body"])
    body = body.replace("{{DELIVER_STRATEGY_BINDING}}", deliver["binding"])
    body = body.replace("{{DELIVER_GUIDANCE}}", deliver["guidance"])
    body = body.replace("{{DELIVER_ALTERNATIVES}}", deliver["alternatives"])
    body = body.replace("{{DELIVER_EXAMPLES}}", deliver["examples"])
    body = body.replace("{{RE_REVIEW_PROGRESS}}", deliver["re_review_progress"])
    body = body.replace("{{RE_REVIEW_ALT}}", deliver["re_review_alt"])
    body = body.replace(
        "{{FEEDBACK_CLOSEOUT_BLOCK}}",
        specs["feedback_closeout"][str(feedback_enabled).lower()]["block"],
    )
    body = body.replace(
        "{{FEEDBACK_REPLY_EXAMPLES}}",
        specs["feedback_closeout"][str(feedback_enabled).lower()]["examples"],
    )
    body = body.replace("{{CUSTOM_GUARDRAILS}}", _custom_guardrails(guardrails))
    meta = [
        "---",
        f"name: {name}",
        f"order: {spec['order']}",
        f"title: {_yaml_string(spec['title'])}",
        f"description: {_yaml_string(description)}",
        f"auto_in_gate_mode: {'true' if spec['auto_in_gate_mode'] else 'false'}",
        f"required_guardrail: {_yaml_string(spec['required_guardrail'])}",
        f"fallback_prompt: {_yaml_string(fallback)}",
        "---",
    ]
    return "\n".join(meta) + "\n\n" + body.rstrip() + "\n"


def _custom_guardrails(guardrails: list[str]) -> str:
    if not guardrails:
        return ""
    lines = "\n".join(f"- {item}" for item in guardrails)
    return f"\n\n## 自定义质量底线\n\n{lines}"


def _yaml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _block_bounds(text: str) -> tuple[int, int] | None:
    """Return the managed profile block, accepting one legacy block in place.

    A file carrying both the canonical and the legacy outer block is ambiguous:
    replacing only one would leave two profile owners, so it fails closed.
    """
    bounds: list[tuple[int, int]] = []
    for begin_marker, end_marker in (
        (GEMINI_BEGIN, GEMINI_END),
        (LEGACY_GEMINI_BEGIN, LEGACY_GEMINI_END),
    ):
        begins = _find_all_positions(text, begin_marker)
        ends = _find_all_positions(text, end_marker)
        if not begins and not ends:
            continue
        if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
            raise ValueError("GEMINI.md 托管标记必须恰好各出现一次且顺序正确")
        bounds.append((begins[0], ends[0] + len(end_marker)))
    if not bounds:
        return None
    if len(bounds) > 1:
        raise ValueError("GEMINI.md 同时包含新旧托管块；请先删除旧版 ORCA_AUTOPILOT_PROFILE 段")
    return bounds[0]


def _assert_unique_aliases(commands: dict[str, dict[str, Any]]) -> None:
    owners: dict[str, str] = {}
    for command_id, command in commands.items():
        for alias in command.get("aliases") or []:
            normalized = str(alias).lower()
            owner = owners.get(normalized)
            if owner and owner != command_id:
                raise ProfileError(f"口令别名 {alias} 同时属于 {owner} 与 {command_id}")
            owners[normalized] = command_id


def _override(overrides: dict[str, str], key: str) -> str:
    return overrides.get(key, "")

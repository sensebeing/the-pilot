"""Pure in-memory compilation of profiles and project overlays."""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .assets.runtime import (
    AGENT_LABELS,
    entry_comma_command,
    format_agent_order,
    format_worker_executor_clause,
    format_worker_launch_flags,
    format_worker_secondary_hint,
)
from .defaults import (
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
        commands[command_id] = entry_comma_command(key, shortcut)
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
    worker_models = profile["orchestration"].get("worker_models")
    review_agents = profile["orchestration"]["review_agents"]
    coordinator = (
        profile["orchestration"].get("coordinator_agent") or COORDINATOR_AGENTS[0]
    )
    coordinator_label = AGENT_LABELS[coordinator]
    priority = format_agent_order(worker_agents)
    primary = format_worker_launch_flags(worker_agents[0], worker_models)
    executor = format_worker_executor_clause(worker_agents, worker_models)
    secondary_hint = format_worker_secondary_hint(worker_agents, worker_models)
    review_priority = format_agent_order(review_agents)
    return f"""{LEGACY_ORCHESTRATION_HEADING}{priority}）

编排前读 `orca skills get orchestration` 及按需 references。

1. **职责分离**：
   - **Tier-1 主会话（Coordinator，无 Orca Task ID / Dispatch ID preamble）**：原生 **{coordinator_label}**（`antigravity`），直接 `agy --dangerously-skip-permissions`，账户池由 Orca 与 `agy-switch` 管理，禁用 `magpie agy`。顶层工作区先登记到 `~/.gemini/antigravity-cli/settings.json` 的 `trustedWorkspaces`。**严禁以 `claude`/`codex` 启动或降级充当 Tier-1 主会话**。{HARNESS_ORCHESTRATION_SENTENCE}。排障查同链路历史与端到端拓扑，禁叠黑白名单、局部重试或特判。
   - **Tier-2 子会话（Dispatched Worker，有 live Task ID / Dispatch ID preamble）**：除极简单改动，实施交 Orca worker。独立分支完成修改、定向测试与秒级本地静态门禁后报 `worker_done`，待主会话零基复审；通过后由 worker 执行模型级 `guard review`、仓库原子合入流程与长构建/部署/探测等待，再 `worker_done` 唤醒。禁止嵌套派工。
2. **Orca 编排（执行器优先级：{priority}）**：禁用 `invoke_subagent` 和无监督裸终端。先 `orca status --json`，再 `orca orchestration run-create --objective "<目标>" --json` 绑定 Run。{executor}。Task spec 自包含 Target/Contracts/Files/Boundaries/Ownership/Acceptance、业务目标、Anchor 0、最简模型，载明「未经主会话 Review 不得合入或推送 `main`」。
3. **工作区层级与回调**：
   - 同工作区 `worker-start --spec "<任务契约>" --worktree current {primary} --json`（{secondary_hint}或 `task-create` 后带 `--task <task_id>`）。
   - 同仓子工作区 `worker-start --spec "<任务契约>" --worktree new-child --name <name> {primary} --setup run --json`（{secondary_hint}`orca worktree create` 预建须 `--parent-worktree active` 或 `path:<当前路径>`）。
   - 跨仓回传 `worker-start --spec "<任务契约>" --worktree new-child --repo path:<目标仓库根> --name <name> {primary} --setup run --json`，`worker_done` 唤醒主会话。
   - 禁止平级 `--worktree new-top-level`/`--no-parent`。仅用户明确要求独立长期 Tier-1 可用 `--no-parent`，需回执绑定 Dealer thread 或 `orca orchestration send`。
   - 单段契约用 `--spec "<任务契约>"` 一轮发出；多段才用 `--spec-file <路径>`（与最终只读核查同轮写入，不单独起草）；纯文档/只读子任务可用 `--setup skip`。
4. **交互与验收**：
   - `worker-start`、`reply` 或 `send` 后即以 `scene=NONE` 结束回合，等待 Orca 原生唤醒（严禁后台挂 `--wait` 查信、`manage_task` 轮询或 `sleep`）；唤醒后执行 `orca orchestration check --environment local --json`（消费即自动标记已处理），用 `reply --id <msg_id>` 或 `send` 裁决指导：单段正文用 `--body "<正文>"` 一轮发出；多段才用 `--body-file <路径>`，与最终只读核查同轮写入，不单独起草。
   - 主会话并行批量执行只读核查；查 diff 先 `git diff --stat` 再按 `-- <paths>` 限定，禁用整分支无界 `git show`/`git diff`；Orca 查询用有界选择器（`--terminal-state reclaimable`、`--worktree path:<路径>`）；多行 Python 用 `uv run --project ...`/`.py`，禁嵌套引号 `python3 -c`；不挂后台 `guard review`、测试套件/构建轮询。
   - `worker_done` 后主会话亲审：对照业务目标与 Anchor 0 蓝图，核查真实 diff 与改动后模块全貌。按 `architecture-re-review` 重读起点、盘点现状、零基对账，并核对仓库工程门禁与架构规则（唯一真源、无循环依赖/内联导入/行内抑制、职责、测试）。代码落地后的独立架构审查优先级：**`{review_priority}`**（文档/方案阶段在当前主会话内完成，不拉起单独审查子会话）。
   - 未达标整改或审查通过通知合入并推送 `main`，均使用 `orca orchestration send --environment local --to dispatch:<dispatch_id> --subject "<主题>" --body-file <路径> --json` 派回（续跑用 `--terminal <handle>` 省略 `--worktree`）。结算后 `worker-release`，确认 `worker-list --terminal-state reclaimable` 无遗留后清理子工作区及已合入分支。"""


def render_orchestration_block(profile: dict[str, Any]) -> str:
    """Wrap the orchestration rules in the managed block used by agent files."""
    return "\n\n".join(
        (ORCHESTRATION_BEGIN, render_orchestration_section(profile), ORCHESTRATION_END)
    ) + "\n"


def render_shell_env_block() -> str:
    """Delegate interactive boot status to the installed runtime by default."""
    return (
        f"{SHELL_ENV_BEGIN}\n"
        'export AGY_CLI_OSC7501="${AGY_CLI_OSC7501:-off}"\n'
        'case "$-" in\n'
        '  *i*)\n'
        '    agy() {\n'
        '      if [ "${AGY_CLI_OSC7501:-off}" != "on" ] && [ -r "$HOME/.gemini/config/the-pilot/runtime.py" ]; then\n'
        '        python3 "$HOME/.gemini/config/the-pilot/runtime.py" announce-boot -- "$@" >/dev/null 2>&1 || true\n'
        '      fi\n'
        '      command agy "$@"\n'
        '    }\n'
        '    ;;\n'
        'esac\n'
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
            f"按下方 {scene_count} 个场景路由"
            "（等外部输入或结案输出 `scene=NONE`），构造"
            "**一句贴合上下文、不重复已完成动作**的下一步指令。"
        )
    else:
        routing = (
            "本机未启用场景；回合结束只输出 `<!-- pilot: scene=NONE -->`，"
            "不做自动预填。"
        )
    return f"""## 个人研发状态机与 Turn Pilot 规范（Agent 单一语义 Owner 三步判断模型）

仅对**主会话（无 Orca Task/Dispatch ID preamble）**生效，子会话忽略。模式真源 `profile.json`（`,mode status|gate|confirm|off`，`gate` 上限 {max_turns} 轮）；**主会话裁决意图、场景与 `gate=auto|pause`**：

1. **第一步：任务性质二分类**：
   - **前置铁律**：先查同链路近 7~14 天修复史与端到端拓扑，定位唯一 owner 与最简根治；复发禁叠黑白名单、局部重试或补丁。
   - **A 类 — 确定性收敛**：行为边界已由设计、契约、工程门禁、事实或明确授权唯一决定，不改变未获授权的安全门禁或跨模块/外部合同边界（含根因修复、冗余清理、回执/并发/超时及质量收敛）。`gate=auto` 逐步推进（定位 → 出详细方案 → 方案级架构 review → 编排实施），不得跳过方案设计或方案复审，不重复提问。
   - **B 类 — 方案与设计裁决**：0→1 新功能/机制/工作流、未定义产品/交互取舍，或未经确认改变安全门禁、内部跨模块/外部合同及状态机边界。按当前仓库 AGENTS.md 与最简架构规则主动澄清：讲清背景、利弊对比、不变量、替代保护与推荐理由（禁纯文本列选项停在 `gate=pause` 等催促），最简方案置首标 `(Recommended)`，同步澄清直接调 `ask_question`（同一步不写标签，禁止停在悬空 `：`），异步`提问未答`输出 `scene=NONE`；所有进度均沿用此边界：B 类方案/裁决未确认标 `gate=pause`，已确认边界内 `gate=auto` 单步递进，prod 发布、未授权真实 E2E 除外。
   - **简化边界**：历史提交/合同不豁免最简架构；讲清不变量与唯一 owner 替代保护，授权按 A/B 分类不重复裁决。
2. **第二步：场景与 Prompt（真源 `scenes/*.md`）**：{routing}
3. **第三步：可见建议与唯一隐藏标签**：正文显示 `- **,1（主推）**：<主推>`，可选 `- **,2（备选）**：<备选>`/`- **,3（备选）**：<备选>`。
   - 末尾唯一 JSON 标签是执行真源（`json.loads` 解析）：`<!-- pilot: {{"scene": "<场景>", "gate": "auto|pause", "1": "<主推>", "2": "<备选>"}} -->`（主推非空，无备选省 "2"，非法/多标签/冲突清空建议）。`gate=auto` 仅用于默认 auto 的 A 类或已确认 B 类；未确认 B 类标 `gate=pause`。异步提问未答或结案输出 `<!-- pilot: {{"scene": "NONE"}} -->`（兼容 `scene=NONE`）；暂停时 `,1`/`,2 [补充]` 触发候选。
   - `,rr [聚焦]` 单波次复审并清循环；`,loop [1-5] [目标]`（默认 3，含 `status`/`off`）有界多波收敛，标签可附 `"loop": "next"|"done"`（等唤醒 `scene=NONE` 保留循环；清零/微瑕/末波 `"scene":"NONE","loop":"done"`；有实质改动收尾且有余波 `plan-and-orchestrate`+`"loop":"next"`，运行时硬限）。

场景真源 `scenes/*.md`；改动后 `pilot sync`。"""


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
        for event in ("SessionStart", "PreInvocation", "Stop")
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
    guardrails = profile["scenes"]["guardrails"]
    description = str(spec["description"])
    description = description.replace("{{DELIVER_COMPLETION}}", deliver["completion"])
    description = description.replace("{{FEEDBACK_TRIGGER}}", "")
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
    body = body.replace("{{FEEDBACK_CLOSEOUT_BLOCK}}", "").replace("{{FEEDBACK_REPLY_EXAMPLES}}", "")
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

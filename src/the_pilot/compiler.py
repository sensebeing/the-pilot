"""Pure in-memory compilation of profiles and project overlays."""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .defaults import AGENT_LABELS, GEMINI_BEGIN, GEMINI_END, HARNESS_ORCHESTRATION_SENTENCE, HOOK_GROUP, LEGACY_GEMINI_BEGIN, LEGACY_GEMINI_END, LEGACY_HOOK_GROUP, LEGACY_SCENES_BEGIN, LEGACY_SCENES_END, SCENES_BEGIN, SCENES_END, UNMANAGED_SCENES_MARKERS_ERROR
from .project import ProfileError, project_specs
from .profile import _copy_mapping, validate_profile


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
    priority = " > ".join(AGENT_LABELS[item] for item in worker_agents)
    primary = worker_agents[0]
    secondary = worker_agents[1] if len(worker_agents) > 1 else ""
    executor = f"优先使用 {AGENT_LABELS[primary]}（`--agent {primary}`）"
    if secondary:
        executor += f"，其次使用 {AGENT_LABELS[secondary]}（`--agent {secondary}`）"
    secondary_hint = f"次选 `--agent {secondary}`；" if secondary else ""
    review_priority = " > ".join(AGENT_LABELS[item] for item in review_agents)
    return f"""## 子 Agent 与多任务编排规范（Orca Orchestration：{priority}）

必须遵循 `~/.agents/skills/orchestration/SKILL.md`：编排前执行 `orca skills get orchestration`（按需加载 coordinator-loop、placement-and-remote、recovery-and-cleanup 等 references），并遵守 5 项准则：

1. **主-子会话职责分离（主会话设计且 Review，实施交子会话）**：
   - **主会话（Coordinator，提示词不含 Orca Task ID / Dispatch ID preamble）**：聚焦调研、工程架构级方案设计与受控编排，严格 Code Review 子会话产出；派发前必须给出工程架构级方案（模块职责、预期文件结构、接口契约、读写收敛路径、设计边界）。{HARNESS_ORCHESTRATION_SENTENCE}。排障、审查或设计须结合近期同链路修改史与端到端调用拓扑，**从整个架构去看，是否应该有更简洁、更加根本的修复方案**，严禁只修表象、枚举黑白名单或叠局部重试/特判；除极简单小改动可顺手处理外，实现统一经 Orca `orchestration` 交子工作区/子会话。
   - **子会话（Dispatched Worker，提示词含 Orca live Task ID / Dispatch ID preamble）**：在分配的工作区与分支内按方案修改、本地定向验证并提交，用 `worker_done` 汇报，不向下套娃派发；架构级分歧回传主会话。
2. **统一用 Orca Orchestration 替代内置 Subagent（执行器优先级：{priority}）**：有代码实施或并行任务时**禁用内置 `invoke_subagent`** 与无监督裸终端移交，统一经 `orca orchestration`：先 `orca status --json`，再 `orca orchestration run-create --objective "<目标>" --json` 绑定受控 Run（持久化命名空间与收件箱）；{executor}。Task spec 自包含且带工程架构设计（Target、Contracts、Files、Boundaries、Ownership、Acceptance）。
3. **工作区父子层级（禁止建成平级工作区）**：同工作区开新会话用 `worker-start --spec "<任务契约>" --worktree current --agent {primary} --json`（{secondary_hint}或 `task-create` 后带 `--task <task_id>`）；新建子工作区用 `worker-start --spec "<任务契约>" --worktree new-child --name <name> --agent {primary} --setup run --json`（{secondary_hint}`orca worktree create` 预建时也必须显式 `--parent-worktree active` 或 `path:<当前路径>`），确保挂为当前工作区的**子工作区**；除非明确要求无关独立顶层任务，**严禁 `--worktree new-top-level` 或 `--no-parent`** 建平行顶层工作区。
4. **子工作区分支隔离（先分支开发与 Review，通过后再合 `main`）**：子会话必须在独立功能分支上修改、本地定向验证与提交；初版实施及打回阶段**严禁自行合入或推送 `main`**（派发 Task spec 时必须声明），避免与主会话 Review 重复触发合入门禁；经主会话严格 Code Review 判定「审查通过」后才可合入 `main` 并推送。
5. **主-子会话交互闭环：受控等待、严格审查、打回迭代与通过后收尾**：
   - 主会话用 `orca orchestration check --wait --types "worker_done,escalation,question" --timeout-ms 900000 --json` 阻塞等待，用 `reply` 回复阻塞问题、`send` 补充中途指导、`--ack` 确认消费（参数见 skill 协议）。
   - 收到 `worker_done` 后不得草率结案：主会话必须亲自对子工作区分支结果做**彻底、不留情面的 Code Review**，首先**从整个架构去看，是否应该有更简洁、更加根本的修复方案**，并严格对照工程质量规范（`peq.*`、唯一真源、无循环依赖、无函数内联导入、无行内抑制注释、边界职责、测试覆盖与门禁脚本）逐项核查真实代码与分支 diff。（独立 Agent 架构审查门禁优先级：**`{review_priority}`**。）
   - **同终端复用打回与受控释放**：有未达标项时按 `orchestration` 同终端复用机制，用 `worker-show` 取已验证终端句柄，再建整改 Task 并 `worker-start --terminal <agent_terminal_handle>` 派回原终端修改并复审至达标；审查通过则用同终端 follow-up Dispatch 通知「审查通过，可合入 `main` 并推送收尾」，全部结算后 `worker-release` 释放受控 worker（**严禁用 `orca terminal close` 替代 `worker-release`**），结束前用 `worker-list --terminal-state reclaimable` 确认无遗留可回收终端并清理子工作区与已合入分支。"""


def render_pilot_section(profile: dict[str, Any], scene_count: int) -> str:
    max_turns = profile["pilot"]["max_consecutive_auto_turns"]
    if scene_count:
        routing = (
            f"每个回合结束时，按下方 {scene_count} 个正交场景的触发条件路由"
            "（需等外部输入或已结案则输出 `scene=NONE`），并构造"
            "**一句自然、贴合上下文、不重复已完成动作**的下一步指令"
            "（展示文本与预填文本 100% 同源一致）。"
        )
    else:
        routing = (
            "本机未启用场景；回合结束只输出 `<!-- pilot: scene=NONE -->`，"
            "不做自动预填。"
        )
    return f"""## 个人研发状态机与 Turn Pilot 规范（Agent 单一语义 Owner 三步判断模型）

仅对**主会话（Coordinator，不含 Orca Task ID / Dispatch ID preamble）**生效，子会话静默忽略。模式以本机 `profile.json` 为唯一真源，可用 `,mode status|gate|confirm|off` 切换；`gate` 连续自动推进上限 {max_turns} 轮。引擎只做机械预填与回车，**意图分类、场景路由与 `gate=auto|pause` 由主会话按三步模型裁决**：

1. **第一步：任务性质二分类（要不要问、要不要停下审方案）**：
   - **前置铁律**：先查同链路近 7~14 天修复史与端到端拓扑，判断唯一 owner 和最简根治路径；复发不得叠黑白名单、局部重试或表象补丁。
   - **A 类 — 确定性收敛**：正确行为与边界已由产品设计、模块契约、`peq.*`、事实或当前明确授权唯一决定，不改变未获授权的安全门禁或跨模块/外部合同边界；含单点及跨内部服务根因修复、已授权或合同内冗余清理、工具参数/回执修正、并发/超时治理、批量问题族和质量收敛。直接 `gate=auto`，不重复提问。
   - **B 类 — 方案与设计裁决**：0→1 新功能/机制/工作流、未定义产品/交互取舍，或未经确认改变安全门禁、内部跨模块/外部合同及状态机边界。按 AGENTS.md 与 `peq.architecture-simplicity` 说明不变量与替代保护，最简方案置首标 `(Recommended)`，同步澄清直接调 `ask_question`（同一步不写标签，禁止停在悬空 `：`），异步`提问未答`输出 `scene=NONE`；同回合获答后按确认边界收敛，首版完整方案未确认标 `gate=pause`。获准后复审/交付/结案 `gate=auto`，prod 发布、未授权真实 E2E 除外。
   - **简化边界**：历史提交/合同不能豁免最简架构。说明真实不变量、复发成因及唯一 owner 的确定性替代保护；未经授权改变安全门禁、跨模块/内外部合同或状态机边界须先确认，已授权范围内直接推进。
2. **第二步：场景路由与 Prompt 构造（所见即所填：真源 `~/.gemini/config/the-pilot/scenes/*.md`）**：{routing}
3. **第三步：输出可见建议与唯一隐藏标签**：可见建议行必须打印在正文中（绝不能只藏在 HTML 注释里）；推荐 `**下一步建议：**` 后跟 `- **,1（主推）**：<主推文本>` 与 `- **,2（备选）**：<备选文本>`，对称使用 `,1`/`,2`：
   - 主推行（二选一）：`💡 **下一步建议 ,1（已自动预填，按 Enter 或输 ,1 发送；输 ,2 切备选）**：<主推文本>`；或 `- **,1（主推）**：<主推文本>`。
   - 备选行（可选）：`- **,2（备选）**：<备选文本>`；第三选项用 `- **,3（备选）**：<备选文本>`。
   - 正文中的 `,1` / `,2` 仅供人类阅读。回复末尾唯一的结构化 JSON 标签是唯一执行真源，由 `json.loads` 解析：`<!-- pilot: {{"scene": "<场景名>", "gate": "auto|pause", "1": "<主推文本>", "2": "<备选文本>"}} -->`（无备选时省略 "2"）。主推必须非空，候选只从标签读取，不解析或交叉校验 Markdown；使用合法 JSON 转义保留文本。
   - 场景名缺失/未知/多标签、gate 缺失或非法时按暂停处理；标签不合法、重复或字段冲突时清空建议。`gate=auto` 仅在场景默认 auto 且为 A 类或已确认实施的 B 类时使用；B 类方案未确认或需人工确认时标 `gate=pause`。异步提问未答或结案输出 `<!-- pilot: {{"scene": "NONE"}} -->`（兼容 `<!-- pilot: scene=NONE -->`）。
   - 暂停/确认回合中，用户可随时输入 `,1 [补充说明]` 触发主推（与 `,2 [补充说明]` 完全对称）。

场景全文由 `scenes/*.md` 唯一持有；改动后运行 `pilot sync` 刷新本段。"""


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

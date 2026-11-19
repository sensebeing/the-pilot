"""Universal profile vocabulary and project-neutral scene templates."""
from pathlib import Path

PROFILE_SCHEMA = "the-pilot-profile/v1"

LEGACY_PROFILE_SCHEMA = "orca-autopilot-profile/v1"

PILOT_SUBDIR = Path("config") / "the-pilot"

LEGACY_AUTOPILOT_SUBDIR = Path("config") / "autopilot"

ENGINE_NAME = "runtime.py"

LEGACY_ENGINE_NAME = "autopilot.py"

LOCK_NAME = ".pilot.lock"

LEGACY_LOCK_NAME = ".autopilot.lock"

HOOK_GROUP = "the-pilot"

LEGACY_HOOK_GROUP = "orca-agy-autopilot"

MODES = ("gate", "confirm", "off")

WORKER_AGENT_ORDER = ("claude", "codex")

REVIEW_AGENT_ORDER = ("codex", "claude", "agy")

COORDINATOR_AGENTS = ("agy",)

COORDINATOR_AGENT_ALIASES = {"agy": "agy", "antigravity": "agy"}

AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex", "agy": "AGY"}

SCENE_ORDER = (
    "plan-and-orchestrate",
    "architecture-re-review",
    "deliver-and-verify",
    "closeout-and-sync",
)

PIPELINE_STAGE_CONTRACTS = {
    "plan-and-orchestrate": {
        "deliverable": "从业务目标零基推导最简领域模型，按详细方案 → 方案级最简架构 Review → 编排实施单步递进。",
        "quality_bar": "明确唯一 owner、核心实体与状态机、原子写入、直接读投影、调用拓扑及验证路径。",
        "simplicity_rule": "扩展纳入权威实体生命周期或读投影；简化从源头移除冗余表、拆分状态机与跨表副作用。",
    },
    "architecture-re-review": {
        "deliverable": "显式重读 Anchor 0，盘点改动后模块全貌，从业务目标零基对账并记录问题。",
        "quality_bar": "逐项核对存活表、状态机与调用链的必要性，确认唯一 owner、边界、契约与风险闭合。",
        "simplicity_rule": "旁路扩展、保留双轨或以 Python 封装和排版代替结构收敛，均打回源头重构。",
    },
    "deliver-and-verify": {
        "deliverable": "按授权完成合入、环境验证和所需的定向实跑证据。",
        "quality_bar": "每个交付动作都有与当前改动匹配的验证结果、剩余风险和可追溯证据。",
        "simplicity_rule": "只执行尚未完成且有证据价值的交付步骤，不重复部署或扩大测试范围。",
    },
    "closeout-and-sync": {
        "deliverable": "同步必要文档、结清授权义务并释放可回收的协作资源。",
        "quality_bar": "收尾记录与实际状态一致，未完成义务有 owner，全部结清后才宣布完成。",
        "simplicity_rule": "收尾只保留真实义务和资源清理，不新增流程证明或重复记录。",
    },
}

DELIVER_STRATEGIES = ("merge_dev_e2e", "merge_dev_only", "local_verify_only")

RESERVED_COMMA_ALIASES = frozenset({"1", "2", "3", "mode", "worker", "agent", "pilot", "autopilot"})

def is_comma_alias(value: str) -> bool:
    return bool(value) and value[0] in "abcdefghijklmnopqrstuvwxyz0123456789" and all(
        char in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in value
    )


def is_entry_key(value: str) -> bool:
    return is_comma_alias(value) and value[0] in "abcdefghijklmnopqrstuvwxyz"


GEMINI_BEGIN = "<!-- BEGIN THE_PILOT_PROFILE -->"

GEMINI_END = "<!-- END THE_PILOT_PROFILE -->"

SCENES_BEGIN = "<!-- BEGIN THE_PILOT_SCENES -->"

SCENES_END = "<!-- END THE_PILOT_SCENES -->"

ORCHESTRATION_BEGIN = "<!-- BEGIN THE_PILOT_ORCHESTRATION -->"

ORCHESTRATION_END = "<!-- END THE_PILOT_ORCHESTRATION -->"

LEGACY_GEMINI_BEGIN = "<!-- BEGIN ORCA_AUTOPILOT_PROFILE -->"

LEGACY_GEMINI_END = "<!-- END ORCA_AUTOPILOT_PROFILE -->"

LEGACY_SCENES_BEGIN = "<!-- BEGIN AUTOPILOT SCENES -->"

LEGACY_SCENES_END = "<!-- END AUTOPILOT SCENES -->"

UNMANAGED_SCENES_MARKERS_ERROR = (
    "GEMINI.md 托管块外存在未托管的场景标记 <!-- BEGIN/END THE_PILOT_SCENES -->"
    "（或旧版 <!-- BEGIN/END AUTOPILOT SCENES -->）；请先清理旧手工场景段再安装"
)

HARNESS_ORCHESTRATION_SENTENCE = (
    "主会话从业务目标设计最简领域模型并严格评审；子会话按模型实施，仅文件组织可调整"
    "（不死板锁死文件清单，调整理由在 worker_done 汇报），不得降低最简领域模型的要求。架构分歧必须用 "
    "`orca orchestration ask` 回传；主会话 `reply` 不得默许双表/双状态机、用 Python 封装或排版代替"
    "结构收敛、在核心实体外加旁路表或状态机。锁定废弃流程表的白盒测试改测领域入口与核心实体真值"
)

DEFAULT_SPECS = {'entry_order': ['r', 'rt', 'd', 'pull'],
 'entry_specs': {'r': {'command_id': 'CMD_MOD_REVIEW',
                       'description': '对指定功能/模块进行完整代码与最简架构 Review',
                       'requires_arg': True,
                       'usage': 'r <module>',
                       'template': '详细 review 一下目前 {arg} '
                                   '的整个代码和设计，从整个架构去看是否有更简洁、更加根本的设计或收敛方案，判断是否有过度设计、工程质量是否达标、是否已经达到最简架构要求'},
                 'rt': {'command_id': 'CMD_TOOL_REVIEW',
                        'description': '以 Anthropic 内部架构师视角对指定类别工具进行整体 Review',
                        'requires_arg': True,
                        'usage': 'rt <工具类别>',
                        'template': '再整体 review 下 {arg} 相关工具，以 anthropic '
                                    '内部架构师的视角，工具的设计是否最佳，是否已经最简洁'},
                 'd': {'command_id': 'CMD_CLARIFY_DESIGN',
                       'description': '先澄清需求并产出最符合工程架构的详细设计方案，再用 Orca 编排实施',
                       'requires_arg': False,
                       'usage': 'd [需求描述]',
                       'template': '你先和我澄清需求和产出最符合工程架构的详细设计方案。然后再用项目的 orca 编排模式来推进子会话/子工作区实施'},
                 'pull': {'command_id': 'CMD_GIT_PULL',
                          'description': '先拉下代码到最新 origin/main',
                          'requires_arg': False,
                          'usage': 'pull',
                          'template': '先拉下代码到最新 origin/main'}},
 'presets': {'recommended': {'pilot': {'mode': 'gate', 'max_consecutive_auto_turns': 8},
                             'scenes': {'enabled_scenes': ['plan-and-orchestrate',
                                                           'architecture-re-review',
                                                           'deliver-and-verify',
                                                           'closeout-and-sync'],
                                        'deliver_strategy': 'merge_dev_e2e',
                                        'enable_feedback_email': False},
                             'shortcuts': {'enabled_entry': ['r', 'rt', 'd', 'pull']}},
             'conservative': {'pilot': {'mode': 'confirm', 'max_consecutive_auto_turns': 4},
                              'scenes': {'enabled_scenes': ['plan-and-orchestrate',
                                                            'architecture-re-review',
                                                            'deliver-and-verify',
                                                            'closeout-and-sync'],
                                         'deliver_strategy': 'merge_dev_e2e',
                                         'enable_feedback_email': False},
                              'shortcuts': {'enabled_entry': ['r', 'rt', 'd', 'pull']}},
             'orchestration-only': {'pilot': {'mode': 'off', 'max_consecutive_auto_turns': 4},
                                    'scenes': {'enabled_scenes': [],
                                               'deliver_strategy': 'merge_dev_e2e',
                                               'enable_feedback_email': False},
                                    'shortcuts': {'enabled_entry': [],
                                                  'custom_entry': {},
                                                  'custom_comma': {},
                                                  'template_overrides': {}}}},
 'scene_specs': {'plan-and-orchestrate': {'file': '01-plan-and-orchestrate.md',
                                          'order': 10,
                                          'title': '方案收敛与 Orca 编排实施',
                                          'description': '当主会话刚完成初版架构审查报告或问题根因定位（待产出详细方案）、刚产出初版详细修复/设计方案（待执行方案级最简架构 review），或方案已通过 review 定稿（待启动 Orca 编排实施，含上一轮代码复审列出明确待改项但本轮尚未改代码）时触发。',
                                          'auto_in_gate_mode': True,
                                          'required_guardrail': '确保达到设计目标、最简架构设计要求、工程质量要求',
                                          'fallback_prompt': '按当前实际进度仅推进下一步：未出详细方案则先出方案，未经方案级最简架构 Review 则先复审定稿，'
                                                             '已审定才通过 Orca 编排子工作区实施并严格 review，确保达到设计目标、最简架构设计要求、工程质量要求',
                                          'body': '## 场景构造说明\n'
                                                  '\n'
                                                  '1. **方案准入（拒绝表象补丁）**：从业务目标零基推导改动后最简领域模型，明确唯一 owner、核心实体与状态机、'
                                                  '写入原子收敛、读取直接投影。扩展纳入权威实体生命周期或读投影，不得新增平行状态机或旁路流程表。'
                                                  '简化/修复从源头消除冗余表、拆分状态机与跨表副作用。列明模块边界、文件结构、数据/接口契约与验证路径。\n'
                                                  '2. **主推与备选构造（按当前实际进度单步递进，严禁跳步合并）**：\n'
                                                  '   - **进度 ①（根因定位或初版审查完成）**：严禁直接编排子工作区改代码。主推 `,1` 按真实待改项产出详细的工程架构修复/收敛方案，需留存时先保存审查报告。备选 `,2` 先确认模块边界与契约草案。\n'
                                                  '   - **进度 ②（详细方案待审）**：严禁未经方案 review 直接启动实施。主推 `,1` 做独立的方案级最简架构 Review，查表象补丁及过度设计并定稿。备选 `,2` 对方案做最简架构 review 后先停下等我确认再进入实施。\n'
                                                  '   - **进度 ③（方案已审定或上轮复审给出明确根治改法，本轮未改代码）**：主推 `,1` 按已审定方案通过 Orca 编排子工作区推进实施并严格 review。新机制/多阶段/跨服务改动的备选 `,2` 先实施并验证首阶段/核心模块，暂缓外围副作用。\n'
                                                  '   - 句末自然包含质量底线：**「确保达到设计目标、最简架构设计要求、工程质量要求」**。\n'
                                                  '3. **门禁判定**：进度 ① → ② → ③ 均按「Agent 单一语义 Owner 三步判断模型」第一步分类；A 类及已确认边界内的 B 类 `gate=auto` 单步递进，B 类方案/裁决未确认 `gate=pause`，同步提问直接调 `ask_question`（不写标签），异步待答输出 `scene=NONE`。沿用统一授权边界，不重复裁决。\n'
                                                  '\n'
                                                  '## 回复示例\n'
                                                  '\n'
                                                  '- 进度 ① **,1（主推）**：`基于根因产出详细工程架构方案，确保达到设计目标、最简架构设计要求、工程质量要求`\n'
                                                  '- 进度 ② **,1（主推）**：`对详细方案做最简架构 review 并定稿，确保达到设计目标、最简架构设计要求、工程质量要求`\n'
                                                  '- 进度 ③ **,1（主推）**：`按审定方案编排子工作区实施并严格 review，确保达到设计目标、最简架构设计要求、工程质量要求`\n'
                                                  '\n'
                                                  '{{CUSTOM_GUARDRAILS}}'},
                 'architecture-re-review': {'file': '02-architecture-re-review.md',
                                            'order': 20,
                                            'title': '实施后全任务最简架构复审循环',
                                            'description': '本轮实施或整改本任务代码后（含子工作区、已合入 main），必须全局复审至实际行为与风险核查无架构或质量问题，才可发布或同步文档。',
                                            'auto_in_gate_mode': True,
                                            'required_guardrail': '确保达到最简架构与工程质量要求',
                                            'fallback_prompt': '重读 Anchor 0，盘点改动后模块全貌，从业务目标零基对账并列出待改项，确保达到最简架构与工程质量要求',
                                            'body': '## 场景判断与构造说明\n'
                                                    '\n'
                                                    '1. **复审范围与治本判断**：按三步复审本会话整个任务涉及的代码。\n'
                                                    '   - ① 显式重读起点文档（Anchor 0：最初审查报告、原始业务需求或初版架构设计），严禁仅看子计划、指标表或凭记忆。\n'
                                                    '   - ② 盘点改动后受影响模块全貌（Whole-Module Reality：存活表/Schema、状态机、子包职责、触发器、读写主链），严禁只看本轮 diff。\n'
                                                    '   - ③ 从业务目标做零基对账：若从零设计，每张表、每个状态机和调用链是否最简？扩展另起旁路表/平行状态机，'
                                                    '简化/修复只合函数不合表、压代码排版凑数或以特判/重试掩盖根因，均判未达标并打回源头重构。\n'
                                                    '   三步复审通过且架构与质量问题清零后才可进入交付（含 `,2`）。\n'
                                                    '2. **主推 `,1`**：绑定任务起点与业务目标，对账后给出复审结论及待改项。\n'
                                                    '3. **备选 `,2`（交付验证）**：\n'
                                                    '   - {{RE_REVIEW_PROGRESS}}\n'
                                                    '\n'
                                                    '## 回复示例\n'
                                                    '\n'
                                                    '- **,1（主推）**：`重读 Anchor 0，盘点模块全貌并做业务目标零基对账，确保达到最简架构与工程质量要求`\n'
                                                    '- **,2（备选）**：`{{RE_REVIEW_ALT}}`\n'
                                                    '\n'
                                                    '{{CUSTOM_GUARDRAILS}}'},
                 'deliver-and-verify': {'file': '03-deliver-and-verify.md',
                                        'order': 30,
                                        'title': '按实际进度合入、发 dev 与端到端验证',
                                        'description': '当且仅当按当前代码实际行为与未覆盖风险核查后，最简架构与工程质量问题为零（不以有无代码改动、是否刚复审来判定），且尚未走完交付与环境验证闭环（合入 '
                                                       'main / 发布 dev / E2E 或实跑验证）时触发，优先级高于文档同步核查。',
                                        'auto_in_gate_mode': True,
                                        'required_guardrail': '过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告',
                                        'fallback_prompt': '{{DELIVER_FALLBACK_PROMPT}}',
                                        'body': '## 场景判断与构造说明\n'
                                                '\n'
                                                '1. **本机交付策略（唯一真源：`profile.json` 的 '
                                                '`scenes.deliver_strategy`，已注入本场景）**：\n'
                                                '{{DELIVER_STRATEGY_BINDING}}\n'
                                                '进入 `/ship` 发布 prod 前必须 '
                                                '`gate=pause`，人工确认一次；`prod` 真实 E2E 与全量/批量套件 E2E 须获当轮明确要求。\n'
                                                '2. **主推建议（动态核对实际交付进度，绝不重复已完成动作）**：\n'
                                                '{{DELIVER_GUIDANCE}}\n'
                                                '3. **备选 `,2` 构造方向（分步交付或跳过未授权动作）**：\n'
                                                '{{DELIVER_ALTERNATIVES}}\n'
                                                '\n'
                                                '## 回复示例\n'
                                                '\n'
                                                '{{DELIVER_EXAMPLES}}\n'
                                                '\n'
                                                '{{CUSTOM_GUARDRAILS}}'},
                 'closeout-and-sync': {'file': '04-closeout-and-sync.md',
                                       'order': 40,
                                       'title': '文档同步与工作区收尾',
                                       'description': '当{{DELIVER_COMPLETION}}，且尚有文档同步、已授权结案或工作区释放义务时触发。全部结清后输出 '
                                                      'scene=NONE。',
                                       'auto_in_gate_mode': True,
                                       'required_guardrail': '',
                                       'fallback_prompt': '同步任务文档，完成已授权收尾，释放受控 worker '
                                                          '并按项目生命周期回收工作区',
                                       'body': '## 收尾清单\n'
                                               '\n'
                                               '1. 同步本任务设计、契约与操作文档，记录尚未完成的义务及其 owner。\n'
                                               '2. 完成已授权且证据充分的结案操作；外部发送仍遵循当前授权。当尚有已授权的结案、通知或工作区释放义务未在本轮直接执行完毕时，必须路由到 closeout-and-sync 并标 gate=auto，生成并自动推进 ,1；严禁在正文反问确认并输出 scene=NONE 或 gate=pause。\n'
                                               '{{FEEDBACK_CLOSEOUT_BLOCK}}\n'
                                               '3. 释放受控 Orca worker，按项目生命周期清理工作区；保留共享工作区和归属不明的文件。\n'
                                               '4. 将已授权改动交付到目标分支；全部义务结清后输出 `<!-- pilot: '
                                               'scene=NONE -->`。\n'
                                               '\n'
                                               '## 回复示例\n'
                                               '\n'
                                               '- **,1（主推）**：`同步任务文档，完成已授权收尾并释放受控工作区资源`\n'
                                               '{{FEEDBACK_REPLY_EXAMPLES}}\n'
                                               '\n'
                                               '{{CUSTOM_GUARDRAILS}}'}},
 'deliver_content': {'merge_dev_e2e': {'binding': '   - **`merge_dev_e2e`**：主推按「合入 main → 发 dev → 相关链路定向 '
                                                 'E2E」推进；已完成步骤直接跳过，**绝不重复**。部署后默认执行受影响链路的定向 dev E2E，除非 Agent 判断本次改动无需 '
                                                 'E2E（如纯文档、脚本或离线契约已完整覆盖）或当前环境不具备执行条件（如实说明原因与剩余风险后直接进入收尾）。',
                                      'guidance': '   - 未合入 `main`：主推「合入 main 并发 dev，然后执行相关链路的定向 E2E 测试（若判断无需做或无条件做则说明原因跳过）」；已合入未发 '
                                                  'dev：「发 dev 并执行本次改动链路的定向 E2E 测试」；已发 dev（或纯客户端改动无需发后端 dev）：「执行本次改动链路的定向 dev E2E 测试」。\n'
                                                  '   - 若已核实本次改动无需 E2E 或当前环境无条件执行，说明具体原因与剩余风险后直接转入 `closeout-and-sync`，不生成空转 E2E '
                                                  '指令；全量/批量套件与 `prod` E2E 仍须当轮明确要求。\n'
                                                  '   - 句末带上「过程中若发现问题遵循最简架构设计修复，最后给我整体报告」。',
                                      'alternatives': '   - 主推包含 E2E 时，备选 `,2` 给「先完成合入 / 发 dev 的部署核验，暂不执行 E2E 测试」。\n'
                                                      '   - 主推已是 E2E 最后一步时，备选 `,2` 给「跳过 E2E，再整体核查本会话整个任务涉及的相关文档」。',
                                      'examples': '- **,1（主推）**：`合入 main 并发 dev，然后执行相关链路的定向 E2E 测试；过程中若发现问题按最简架构修复，最后给我整体报告`\n'
                                                  '- **,2（备选）**：`先合入 main 并发 dev 完成部署核验，暂不执行 E2E`',
                                      'fallback': '合入 main 并发 dev，然后执行相关链路的定向 E2E 测试；过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告',
                                      'completion': '已经完成 main 合入、dev 发布及定向 E2E 验证（或已核实无需/无条件执行 E2E 并说明原因）',
                                      're_review_alt': '合入 main 并发 dev，然后执行相关链路的定向 E2E 测试；过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告',
                                      're_review_progress': '按当前交付状态（未合入 main 从「合入 main 并发 dev」开始；已合入从「发 dev」开始）给出推进交付与相关链路定向 E2E '
                                                            '验证的指令（无需或无条件执行时说明原因跳过）。'},
                     'merge_dev_only': {'binding': '   - **`merge_dev_only`**：主推按「合入 main → 发 dev '
                                                   '完成部署核验」推进；**不生成 E2E 主推**，`dev` '
                                                   '完成即交付闭环，已完成步骤直接跳过。',
                                        'guidance': '   - 未合入 `main`：「合入 main 并发 dev 完成部署核验（不执行 '
                                                    'E2E）」；已合入未发 dev：「发 dev 完成环境部署核验（不执行 E2E）」；已发 '
                                                    'dev：进入文档收尾，不重复交付动作。\n'
                                                    '   - **本策略不生成 E2E '
                                                    '指令**，也不把历史偏好当成用户授权；只保留未完成的步骤。\n'
                                                    '   - 句末带上「过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告」。',
                                        'alternatives': '   - 主推是合入后发 dev 时，备选 `,2` 给「先完成 main '
                                                        '合入，等部署核验通过后再继续」。\n'
                                                        '   - 主推是发 dev 时，备选 `,2` '
                                                        '给「再整体核查本会话整个任务涉及的相关文档」。',
                                        'examples': '- **,1（主推）**：`合入 main 并发 dev 完成部署核验（不执行 '
                                                    'E2E）；过程中若发现问题遵循最简架构设计修复，最后给我整体报告`\n'
                                                    '- **,2（备选）**：`先合入 main，等部署核验通过后再继续`',
                                        'fallback': '合入 main 并发 dev 完成部署核验（不执行 '
                                                    'E2E）；过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告',
                                        'completion': '已经完成 main 合入、dev 发布及部署核验（不执行 E2E）',
                                        're_review_alt': '合入 main 并发 dev 完成部署核验（不执行 '
                                                         'E2E）；过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告',
                                        're_review_progress': '按当前实际交付状态（未合入 `main` 则从「合入 main 并发 '
                                                              'dev」开始；已合入 `main` 则从「发 '
                                                              'dev」开始）给出推进部署核验的指令（不执行 '
                                                              'E2E）。'},
                     'local_verify_only': {'binding': '   - **`local_verify_only`**：不发布 `dev`、不执行 '
                                                      'E2E；主推只构造「合入 main → 本地实跑验收 → '
                                                      '整体报告」，已完成步骤直接跳过。',
                                           'guidance': '   - 未合入 `main`：「合入 main 并完成本地实跑验收（不发布 '
                                                       'dev、不执行 E2E）」；已合入：补做本地实跑验收与整体报告。\n'
                                                       '   - **本策略不发布 dev、不生成 E2E 指令**；只保留未完成的步骤。\n'
                                                       '   - 句末带上「过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告」。',
                                           'alternatives': '   - 主推是合入并实跑验收时，备选 `,2` '
                                                           '给「再整体核查本会话整个任务涉及的相关文档，确认是否已按最新代码和设计更新到最新」。',
                                           'examples': '- **,1（主推）**：`合入 main 并完成本地实跑验收（不发布 '
                                                       'dev、不执行 '
                                                       'E2E）；过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告`\n'
                                                       '- '
                                                       '**,2（备选）**：`再整体核查一下本会话整个任务涉及的相关文档，确认是否已按最新代码和设计更新到最新`',
                                           'fallback': '合入 main 并完成本地实跑验收（不发布 dev、不执行 '
                                                       'E2E）；过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告',
                                           'completion': '已经完成 main 合入与本地实跑验收（不发布 dev、不执行 E2E）',
                                           're_review_alt': '合入 main 并完成本地实跑验收（不发布 dev、不执行 '
                                                            'E2E）；过程中若发现问题遵循最简架构设计进行修复，最后给我整体报告',
                                           're_review_progress': '按当前实际交付状态给出「合入 main → 本地实跑验收 → '
                                                                 '整体报告」的指令（不发布 dev、不执行 '
                                                                 'E2E）。'}},
 'feedback_closeout': {'false': {'block': '', 'examples': '', 'trigger': ''},
                       'true': {'block': '   - 在已启用反馈通知授权的范围内，验证通过后按 gate=auto 自动完成结案与通知闭环，禁止在正文反问确认后输出 scene=NONE 或 gate=pause；缺少证据时等待用户。\n',
                                'examples': '',
                                'trigger': ''}}}

# The Pilot

**让 Agent 每一轮结束后，安全地推进到贴合上下文的下一步。**

[English](README.md) | 简体中文

Python 3.11+，零外部运行时依赖，支持 macOS / Linux 的 `fcntl` 文件锁。
真实终端投递依赖 Orca 和兼容 Antigravity 生命周期 Hook 的宿主；编译与测试可离线运行。

## 30 秒上手：复制给你的 Agent

```text
请在保留现有配置的前提下安装 The Pilot。
使用 ~/the-pilot 的可信检出；项目发布后可从 https://github.com/sensebeing/the-pilot 获取。
先确认 Python >=3.11，阅读 skills/the-pilot-setup/SKILL.md，运行 bin/pilot probe --json。
向我确认执行器优先级、模式与轮数、场景与交付策略、快捷口令四个维度。
若项目有 .pilot/project.json，使用 --project <项目目录>。
按确认结果 apply，再用 status 检查 drift 为空，给我个性化速查表。
安装不代表授权发信、生产发布或真实 E2E。
```

## 安装与技能

```sh
~/the-pilot/bin/pilot probe --no-project --json
~/the-pilot/bin/pilot apply --no-project --preset recommended --json
~/the-pilot/bin/pilot status --json
# 也可安装 Python 包：python3 -m pip install /path/to/the-pilot
```

源码 launcher 不需要 pip 或第三方依赖。把 `skills/the-pilot`（日常操作）和
`skills/the-pilot-setup`（交互式安装定制）注册到 Agent 技能加载器。
仅显式 `apply` 写入配置，安装包本身不自动改动用户目录。

## 架构与唯一职责

通用规格 + 项目 `.pilot/project.json` + 用户 `profile.json` → 纯编译器 →
`config.json`、四场景文件、GEMINI.md 托管块和 Hook 组 → 独立单文件运行时。
PreInvocation 处理口令与执行器热加载；Stop 只读取回复末尾的结构化 JSON 标签并机械投递到 Orca；可见建议仅供阅读。

主会话负责语义判断、架构设计和复审，子会话在分支内实施并汇报。
运行时负责提问结算、会话锁、轮数上限和投递状态，不再做第二次模型裁决。
不确定投递保留占用，直到新用户回合或显式恢复；不能盲目重发。
The Guard 负责工程审查，The Dealer 负责跨会话/跨机传输，The Pilot 负责当前回合推进。
模块职责见 [模块说明](src/the_pilot/README.md)。

## 四场景状态机

| 场景 | 触发条件 | 推进行为 |
|---|---|---|
| plan-and-orchestrate | 已确认方案或根因需实施 | 按契约编排子会话 |
| architecture-re-review | 实施或整改后需复审 | 核查全任务与最简架构 |
| deliver-and-verify | 审查通过但交付义务未完 | 完成授权内交付验证 |
| closeout-and-sync | 交付完成但收尾未完 | 同步文档并释放资源 |

复审有问题回实施，等待用户或全部完成输出 `scene=NONE`。
`gate` 模式下 `gate=auto` 才自动提交，`gate=pause` 仅预填；`confirm` 必须人工回车，
`off` 停止回合末预填。生产发布、外部发送及真实 E2E 仍遵循明确授权边界。

## 项目扩展层

将 [示例](examples/project.example.json) 放到项目 `.pilot/project.json`。
`the-pilot-project/v1` 支持项目标识/说明、入口顺序和规格、预设、场景、交付文案与反馈收尾文案。
只需覆盖变化字段，无需复制编译器。新增入口必须进入 `entry_order`，命令身份须唯一；
内置场景文件名固定，禁止通过配置写出托管目录。未知字段报错，不静默回退。

```sh
pilot apply --project /path/to/repo --preset recommended --json
pilot apply --upgrade --json
pilot apply --no-project --json
```

选择顺序：`--no-project` → 显式 `--project` 仓库或 JSON 文件 → 已安装项目副本 →
当前目录向上最近的项目叠加 → 通用默认。apply 保存项目副本，所以任意目录升级都保留项目关联。
修改仓库叠加后需显式 `--project` 刷新，status 默认对照已安装副本；通用 apply 删除关联。
通用入口仅 `r`、`rt`、`d`、`pull`，项目可增加业务口令和收尾规则。
个人长期偏好仍由 `profile.json` 唯一持有。预设推荐 gate/8轮，保守 confirm/4轮，仅编排 off。

## 操作速查

| 命令 | 用途 |
|---|---|
| `pilot probe --json` | 只读探测 |
| `pilot apply --preset conservative --json` | 编译安装 |
| `pilot apply --upgrade --json` | 保留偏好升级 |
| `pilot status --json` | 安装与漂移核验 |
| `pilot uninstall --json` | 删除托管产物及项目关联 |
| `pilot worker codex` / `,worker codex` | 持久化执行器优先级，下轮热加载 |
| `pilot mode confirm` / `,mode confirm` | 切换模式 |
| `pilot hook --event PreInvocation` | Hook 入口，另支持 Stop |
| `pilot sync` | 从场景刷新指导段 |
| `pilot set r 'Review {arg}'` | 保存口令模板 |
| `pilot log` | 查看投递记录 |
| `,1` / `,2` / `,3` | 选择结构化标签中的候选 |
| `,autopilot recover` | 显式恢复不确定投递 |

安装命令接受 `--gemini-dir`，运行时使用 `ORCA_AUTOPILOT_GEMINI_DIR`。
保留其他 Hook 组、非托管 GEMINI.md 正文和自定义场景。
文件锁下逐文件原子替换，并非跨文件事务；文件系统中断后重新 apply。
场景文件可运行时编辑并 sync；升级会按 profile/项目叠加重新生成。

## 验证

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
guard check --repo-root . --policy-only
guard check --repo-root . --base origin/main
guard review --repo-root .
```

所有测试用临时目录、显式隔离和 fake Orca，不触碰真实个人配置。
迁移测试完整保留提问结算、转录水合、投递锁和执行器热加载覆盖。
旧实现冻结哈希守住预设、交付策略、通知偏好与自定义口令组合的字节级输出；不保留第二套编译器。

MIT 许可证，见 [LICENSE](LICENSE)。

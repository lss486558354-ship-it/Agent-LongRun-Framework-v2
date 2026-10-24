# CLAUDE.md · Agent-LongRun-Framework

本仓库把「执行状态机」与「MCP 记忆路由」耦合成一条长任务工作流。
Agent 在本仓库工作，或使用本框架执行任务时，必须遵守以下规则。

细则：`rules/00-entry.md`、`rules/01-memory-first.md`、`rules/02-checkpoint-archive.md`、`rules/03-execution-boundary.md`
技能：`skills/auto-dev-workflow/SKILL.md`
架构：`README.md`

## 项目边界（v0.2）

- 每个任务绑定一个**项目根**：`LongRunAgent(root=项目根)`
- 热状态：`<项目根>/.longrun/state.json`
- 冷记忆：`<项目根>/memory.db` 与 `<项目根>/raw_data/`
- MCP 子进程必须由 `MCPMemoryBackend(project_path=项目根)` 启动，
  参数带 `--project_path <项目根>`
- 一个任务只读写本项目记忆；需要跨项目时显式切换 `root`，不要混用两个库
- 默认后端与 `StateStore` 的项目根必须一致，否则 `resume()` 会读错历史

## 两条铁律

### 铁律一

**在处理任何长任务或涉及历史代码修改时，必须先调用 `search_memory` 检索相关上下文。**

### 铁律二

**在完成一个执行阶段或触发中断（如 token 预警）时，必须先调用 `save_memory` 存入当前进度和关键决策，然后再输出断点信息。**

## 执行顺序（不可调换）

```text
start_task
  → search_memory（铁律一）
  → 建立状态 in_progress / read_only
  → 调研
  → enable_writes(原因)
  → 实施
  → checkpoint / complete_phase
      → 生成断点（含机器标记）
      → save_memory（铁律二）
      → 成功后才更新状态、输出断点
  → resume：search_memory → 校验状态 → read_raw_data → 继续
```

## 读写门禁

| 模式 | 用途 |
|------|------|
| `read_only`（默认） | 读取、检索、只读分析 |
| `writable` | 改文件、删除、提交、装依赖、外部副作用 |

- `enable_writes(原因)` / `disable_writes(原因)` 显式切换
- 写操作前必须 `require_writable(动作)`
- 不允许绕过门禁直接调用 Write/Edit

## 状态机

`pending → in_progress → {paused, blocked, waiting_verification, completed, failed}`

- `paused` / `blocked` / `waiting_verification` 可回到 `in_progress`
- `completed` / `failed` 为终态
- 非法流转抛 `TransitionError`
- 未完成任务不允许被新任务覆盖

## 工具映射

| 动作 | MCP 工具 | Python |
|------|----------|--------|
| 查记忆 | `search_memory` | `agent.start_task()` / `_safe_search` |
| 读原文 | `read_raw_data` | `agent.resume()` / `recover_from_memory()` |
| 写断点/决策 | `save_memory` | `agent.checkpoint()` / `complete_phase()` / `finish()` / `save_decision()` |
| 建关系 | `link_memory` | `save_decision(relates_to=...)` |

## 本仓库开发约定

- 修改源码前：复述边界 → 备份到 `.backups/{日期}_{关键词}/` → 写 `BUG_DESCRIPTION.md` → 征询确认
- 修改后：更新 `PROJECT_INDEX.md` 的修改历史
- 测试：`python -m pytest tests/ -q`，新增行为必须有测试
- 记忆路由器默认找同级的 `agent-memory-router-v2`，回退 v0.1；可用 `AGENT_MEMORY_ROUTER_PATH` 覆盖
- 改后端时要保证 `project_path` 被传给 `memory_core.configure()` 或 MCP `--project_path`
- 中文注释，代码标识符用英文
- 不把 `.longrun/`、`*.db`、`raw_data/` 提交进仓库

## 禁止

- 未检索记忆就下「无历史方案」结论
- 先输出断点再补写记忆
- 只读模式下写文件
- 把 A 项目的状态与 B 项目的记忆混在一起
- 用量化描述代替可验证结果（「基本完成」不算完成）

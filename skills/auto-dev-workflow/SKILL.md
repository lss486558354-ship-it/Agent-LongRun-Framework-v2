---
name: auto-dev-workflow
description: 长任务自动开发工作流。任务开始先查记忆，执行受读写门禁约束，中断或阶段完成时先写记忆再输出断点。包含 interrupted-task-recovery（中断恢复）与 phase-completion-archive（阶段归档）两个 SOP。
---

# auto-dev-workflow

长任务的「记忆 + 状态机」标准作业流程。
适用于跨会话、跨阶段、可能被 token 中断的开发任务。

## 前置条件

- **项目根已确定**：`LongRunAgent(root=项目根)`，状态与记忆都绑定它
- 记忆服务可用：`agent-memory-router-v2` MCP Server（`--project_path` 指向同一根），
  或 `LocalMemoryBackend(project_path=项目根)`
- 状态目录可写：`<项目根>/.longrun/`
- 记忆库可写：`<项目根>/memory.db` 与 `raw_data/`
- Python 胶水层：`workflow_manager.LongRunAgent`

## 总流程

```text
LongRunAgent(root=项目根)
  └─ start_task
       ├─ 铁律一：search_memory（只查本项目）
       ├─ 按需 read_raw_data
       └─ 状态 pending → in_progress，门禁 read_only
             │
             ├─ 调研完成 → enable_writes(原因)
             ├─ 实施
             ├─ 阶段完成 → phase-completion-archive
             ├─ 中断     → checkpoint(kind="interrupt")
             └─ 任务完成 → finish
```

---

## SOP 1 · interrupted-task-recovery

**场景**：上次被 token、会话、机器切换打断，需要接着做。

### 步骤

1. **确认项目根，再读取记忆**
   - 用当初同一个 `root` 创建 Agent：`LongRunAgent(root=项目根)`
   - 有本地状态：`.resume()`（内部会读断点原文并检索关联）
   - 没有本地状态：`.recover_from_memory(关键词)`，或手工
     `search_memory(keyword="[断点] ...")` → `read_raw_data(id)`
   - 根换错时表现为「本地状态属于 A、记忆里找不到断点」；不要继续猜，
     先核对 `.longrun/state.json` 与 `memory.db` 是否在同一项目下

2. **校验状态机当前状态**

   | 状态 | 含义 | 动作 |
   |------|------|------|
   | `paused` | 正常中断 | 恢复到 `in_progress`，按 `next_step` 继续 |
   | `blocked` | 外部阻塞 | 先确认阻塞是否解除，再 resume |
   | `waiting_verification` | 等待验收 | 先完成验收，不要继续开发 |
   | `in_progress` | 上次没写断点就断了 | 核对已完成步骤，补一次 checkpoint |
   | `completed` / `failed` | 终态 | 不要恢复，开新任务 |

3. **按需加载局部上下文**
   - 先看断点里的「已完成 / 关键决策 / 下一步」
   - 只对涉及的模块调用 `read_raw_data`，不要全量加载
   - 用 `link_memory` 的关系找相关决策与历史教训

4. **恢复执行**
   - 核对断点中的 `next_step` 与当前代码是否一致（代码可能已被改动）
   - 不一致时先说明差异，再决定沿用还是修正
   - 恢复后第一次写操作前，让用户确认一次
   - 继续按阶段执行，每个阶段结束走 SOP 2

### 失败分支

| 情况 | 处理 |
|------|------|
| 本地状态缺失 | `recover_from_memory()` 从机器标记重建 |
| 状态与记忆不在同一项目 | 停止恢复，核对 `root`；不要跨项目拼进度 |
| 状态文件损坏 | 不静默重建；先报 `StateCorruptError`，再尝试从记忆恢复 |
| 记忆里也找不到 | 如实说明，按新任务 `start_task`，不要假装恢复 |
| `.longrun/pending_checkpoint.json` 存在 | 上次断点未写入记忆，先补写 `save_memory` 再继续 |
| 断点里的下一步已过时 | 停下来向用户确认，不要自行改道 |

---

## SOP 2 · phase-completion-archive

**场景**：一个阶段完成，准备进入下一阶段；或任务收尾。

### 步骤

1. **确认阶段真的完成**
   - 有可验证结果（测试通过、命令输出、文件变更）
   - 「基本完成」「应该可以」不算完成，继续做或标记 `blocked`

2. **结构化写记忆**（先写，后汇报）

   ```python
   agent.complete_phase(
       "阶段2：接入 Xometry",
       "只读探测跑通；首页无法拿到匿名报价",
       decisions=["标记为需登录，不做自动化绕过"],
       next_step="阶段3：接入 Protolabs",
   )
   ```

   等价的手工 MCP 调用：

   ```text
   save_memory(
     type="session",
     title="[阶段完成] 阶段2：接入 Xometry —— <一句话>",
     content="<阶段 / 已完成 / 关键决策 / 下一步 / 机器标记>",
     keywords="longrun,checkpoint,<task_id>"
   )
   ```

3. **写入成功后**才向用户汇报阶段结果
   - 汇报内容与记忆内容必须一致
   - 附上 memory_id，便于追溯

4. **清理阶段上下文**
   - 只保留下一阶段需要的摘要与决策
   - 不再需要的中间产物写入记忆后可从上下文移除

### 归档内容模板

```text
[LongRun Checkpoint]
任务: <任务描述>
任务ID: <task_id>
类型: phase_complete
状态: <status>
读写门禁: <access_mode>
阶段: <阶段名>
触发原因: 阶段完成

已完成:
  - <可验证结果 1>
  - <可验证结果 2>

关键决策:
  - <决策 + 为什么>

下一步: <具体到可执行>

<!-- longrun-state: {"task_id":"...","status":"in_progress",...} -->
```

### 失败分支

| 情况 | 处理 |
|------|------|
| `save_memory` 失败 | 不汇报「已归档」；写 `pending_checkpoint.json`；说明失败原因 |
| 阶段完成但未验证 | 不要调用 `complete_phase`，标 `waiting_verification` |
| 决策依赖用户拍板 | 先 `note_blocked()`，等确认后再归档 |

---

## 反模式

- ❌ 先输出断点，再 `save_memory`
- ❌ 只写「继续做」，没写下一步和已完成
- ❌ 恢复时跳过状态校验，直接按聊天记忆干活
- ❌ 只读模式下改文件
- ❌ 记忆服务报错，静默按「无历史」推进
- ❌ 把整段正文塞进摘要，破坏冷热分离
- ❌ 终态任务继续写断点

## 验收清单

- [ ] `LongRunAgent(root=...)` 与 MCP `--project_path` 指向同一个项目
- [ ] 任务开始前有 `search_memory` 调用记录
- [ ] 新任务门禁为 `read_only`
- [ ] 写操作前调用过 `require_writable`
- [ ] 每个断点都有 memory_id
- [ ] 断点含单行机器标记
- [ ] 恢复时校验过状态机状态
- [ ] 阶段汇报与记忆内容一致

# PROJECT_INDEX

> 最后更新：2026-10-25（v0.2 项目自治）
> 维护规则：每次代码修改后追加「修改历史」，并刷新此处。

## 1. 项目基本信息

| 项 | 内容 |
|----|------|
| 名称 | Agent-LongRun-Framework |
| 版本 | 0.2.0 |
| 定位 | 把 Agent 执行状态机与项目级 MCP 记忆路由耦合的长任务工作流 |
| 语言 | Python 3.10+（开发环境 3.12.0） |
| 依赖 | `mcp>=2.0,<3.0`；开发依赖 pytest、ruff |
| 入口 | `workflow_manager.LongRunAgent` |
| 上游 | `engineering-workflow`（状态语义/断点格式）、`agent-memory-router-v2`（项目级 MCP 记忆） |
| 远端 | https://github.com/lss486558354-ship-it/Agent-LongRun-Framework-v2 |
| License | MIT |
| 项目数据 | `<项目根>/.longrun/state.json`、`<项目根>/memory.db`、`<项目根>/raw_data/` |

## 2. 目录结构

```text
Agent-LongRun-Framework-v2/
├── state_machine.py        # TaskStatus(7)、AccessMode、TRANSITIONS、TaskState、StateStore
├── memory_client.py        # MemoryBackend 协议 + MCP/Local/InMemory 三实现 + 项目根解析
├── workflow_manager.py     # LongRunAgent 门面与断点/恢复逻辑
├── tests/
│   ├── conftest.py         # 把仓库根加入 sys.path
│   ├── test_state_machine.py
│   └── test_workflow_manager.py
├── examples/
│   ├── 01_interrupted_task.py
│   ├── 02_phase_archive.py
│   └── README.md
├── rules/
│   ├── 00-entry.md
│   ├── 01-memory-first.md
│   ├── 02-checkpoint-archive.md
│   └── 03-execution-boundary.md
├── skills/auto-dev-workflow/SKILL.md
├── .cursorrules / CLAUDE.md
├── README.md
├── PROJECT_INDEX.md
├── pyproject.toml
├── requirements.txt / requirements-dev.txt
├── .gitignore / .gitattributes
└── LICENSE
```

## 3. 核心模块

### state_machine.py

- `TaskStatus`：`pending / in_progress / paused / blocked / waiting_verification / completed / failed`
- `AccessMode`：`read_only` / `writable`
- `TRANSITIONS`：白名单流转；`completed`、`failed` 无出口
- `TaskState`：任务上下文（描述、阶段、已完成步骤、决策、断点 id、history）
- `StateStore(root)`：`<root>/.longrun/state.json` 原子读写（tmp → replace），损坏抛 `StateCorruptError`
- `transition()` 校验流转并记录 history，最多保留 20 条

### memory_client.py

- `MemoryBackend` Protocol：`search / read_raw / save / link`
- `_resolve_project_path()`：`project_path` > `home`（v0.1 别名）> `MEMORY_ROUTER_HOME` > cwd
- `_default_router_path()`：`AGENT_MEMORY_ROUTER_PATH` > 同级 `agent-memory-router-v2` > v0.1
- `MCPMemoryBackend(project_path=...)`：stdio 拉起 `mcp_server.py`，参数带
  `--project_path <项目根>`，cwd 设为项目根；强制 UTF-8；`ok=false` 抛 `MemoryBackendError`
- `LocalMemoryBackend(project_path=...)`：import `memory_core` 后调用
  `configure(project_path)` + `init_db()`；同进程切换项目会报错
- `InMemoryBackend`：测试用，记录 `calls` 顺序，ID 前缀 D/C/I/N/S/R/M

### workflow_manager.py

- `LongRunAgent(root=...)`：
  - `project_root = store.root`（传入 store 时）或 `root/LONGRUN_HOME/cwd`
  - `StateStore(project_root)`
  - 默认 `MCPMemoryBackend(project_path=project_root)`
  - 保证热状态与冷记忆属于同一个项目
- `start_task()`：拒绝覆盖未完成任务 → 检索 → hydrate → `PENDING→IN_PROGRESS` → 落盘
- `checkpoint()`：副本上改状态 → 渲染（含 `<!-- longrun-state: {...} -->`）→
  `save_memory` → 成功后更新状态与 `checkpoint_id` → 返回
- 失败：写 `.longrun/pending_checkpoint.json`，抛 `CheckpointWriteError`，不返回断点
- `complete_phase()`：状态保持 `in_progress`，阶段写入 `completed_steps`
- `finish()`：归档后进 `completed`
- `note_blocked()`：`blocked`
- `resume()`：读本地状态 → 修到 `in_progress` → 读断点原文 → 搜索关联
- `recover_from_memory()`：搜索 session → 读原文 → 正则解析机器标记 → 重建状态
- 门禁：`enable_writes / disable_writes / require_writable`
- `save_decision()`：写 `decision`，与断点建 `related_to`
- `extract_keywords()`：拉丁词 + 中文二字滑窗，过滤停用词与全虚词片段

## 4. 数据流

```text
LongRunAgent(root=项目根)
  → MCPMemoryBackend(project_path=项目根) + StateStore(项目根)
  → start_task
      → Memory.search（只查 <项目根>/memory.db）
      → Memory.read_raw（top 1）
      → StateStore.save（<项目根>/.longrun/state.json）
  → 执行（read_only → enable_writes → writable）
  → checkpoint
      → Memory.save（session，正文含机器标记）
      → StateStore.save（status / checkpoint_id）
      → 返回断点
  → resume / recover_from_memory（必须使用同一个 root）
```

MCP 子进程启动参数：

```text
<python> <router>/mcp_server.py --project_path <项目根>
cwd = <项目根>
env = PYTHONIOENCODING=utf-8, PYTHONUTF8=1
```

## 5. 用户需求推断

- 用户要把「记忆机制」固化进 Rule + Skill，而不仅是做成工具
- v0.2 要求记忆按项目自治：每个项目独立 `.mcp.json`、数据库、原文目录
- 要求状态与记忆必须配套：同一个 `root` 同时决定 `.longrun/` 与默认后端
- 要求「换机器恢复」仍然可用：热状态可丢，冷记忆必须能重建
- 重视可验证性：README 有 Mermaid 架构图、示例可运行、测试通过
- 重视简历可展示性：命名、结构、文档都要能直接公开
- 偏好中文文档与注释，代码标识符英文

## 6. 编码习惯

- 中文 docstring 与行内注释，先说明「为什么」
- 小模块优先，单文件承担单一职责
- 数据类 + Enum 而非裸 dict
- 显式异常类型，失败路径写测试
- 原子写（tmp → replace）
- 测试使用 InMemoryBackend 断言调用顺序，不 mock 内部实现
- 兼容旧参数时保留别名（`home`），但新代码优先用新名字（`project_path`）

## 7. 修改历史

### 2026-10-24 · 初始化

- 新建状态机、记忆后端协议、`LongRunAgent` 胶水层
- 38 个测试通过（状态机 10 + 工作流 28）
- 示例 1（中断恢复）在 Local 与真实 MCP stdio 下均跑通
- 示例 2（阶段归档 + 换机器恢复）在 Local 下跑通
- 补齐 rules、skill、`.cursorrules`、`CLAUDE.md`、README、工程元文件
- 初始化 git 并推送远端

### 2026-10-25 · v0.2 项目自治

- `LongRunAgent(root=...)` 先解析项目根，再创建 `StateStore` 与默认后端
- 默认 `MCPMemoryBackend(project_path=...)`，子进程参数带 `--project_path`
- `LocalMemoryBackend` 改用 `memory_core.configure(project_path)`，删除对
  `MEMORY_ROUTER_HOME` 的强依赖（保留为兼容回退）
- `home=` 保留为 `project_path=` 的旧别名
- `_default_router_path()` 优先同级 `agent-memory-router-v2`，回退 v0.1
- 示例改为 `project_path=临时项目根`，验证 `memory.db` / `raw_data/` 落在项目内
- 新增 4 个项目绑定测试，测试总数 42
- 更新 README / CLAUDE.md / `.cursorrules` / rules / skill / examples / 本索引

## 8. 技术债务

| 级别 | 项 | 说明 |
|------|----|------|
| 中 | MCP 每次调用拉子进程 | 高频检索需长连接或 Local 后端 |
| 中 | 状态文件无并发锁 | 同一 `<项目根>/.longrun` 不支持多进程同时写 |
| 中 | 关键词提取为启发式 | 无向量/语义召回，长尾同义词会漏 |
| 中 | `LocalMemoryBackend` 项目根是模块全局 | 同进程不能并行切项目；生产走多个 MCP 子进程 |
| 低 | 同步 API | 异步场景需线程池包装 |
| 低 | 状态 schema 无版本号 | 未来字段变更需要迁移策略 |
| 低 | 无 CI | 目前仅本地 pytest |
| 低 | `mcp` 版本上限 `<3.0` | 升级需回归 stdio 调用 |
| 低 | 默认路由器路径依赖同级目录 | 换目录需设 `AGENT_MEMORY_ROUTER_PATH` 或传参 |

## 9. 验证命令

```bash
python -m pytest tests/ -q
python examples/01_interrupted_task.py
python examples/01_interrupted_task.py --mcp
python examples/02_phase_archive.py
python examples/02_phase_archive.py --mcp
python -m ruff check .
```

验收时额外确认：

- 临时项目目录下出现 `memory.db`、`raw_data/`、`.longrun/`
- 路由器仓库的 `data/` 不再被写入
- MCP 模式下 `MCPMemoryBackend._server_args()` 含 `--project_path`

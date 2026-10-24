# examples/

两个可直接运行的端到端示例。数据写到系统临时目录，不污染正式项目。

## 环境

```bash
pip install -r requirements.txt
```

默认使用 `LocalMemoryBackend(project_path=临时目录)`，进程内直连
`agent-memory-router-v2/memory_core.py`。加 `--mcp` 则真正走 MCP stdio 子进程，
并带 `--project_path <临时目录>`。需要：

- 同级目录下的 `agent-memory-router-v2`（或设置 `AGENT_MEMORY_ROUTER_PATH`）
- Python 环境里已安装 `mcp>=2.0`
- 路由器支持 `--project_path`（v0.2+）

示例中的「项目根」就是临时目录本身，运行后会看到：

```text
<临时目录>/
├── memory.db
├── raw_data/
└── .longrun/state.json
```

## 1. 中断 → 断点 → 恢复

```bash
python examples/01_interrupted_task.py
python examples/01_interrupted_task.py --mcp
```

演示：

1. 预置一条历史决策
2. `start_task` 自动检索并加载原文
3. 只读门禁拦截写操作
4. `enable_writes` 解锁
5. token 预警 → `checkpoint`（先写记忆，再输出断点）
6. 新 `LongRunAgent(root=同一个临时目录)` 读取本地状态并 `resume`
7. `finish` 写入完成归档

## 2. 阶段归档 → 换机器恢复

```bash
python examples/02_phase_archive.py
python examples/02_phase_archive.py --mcp
```

演示：

1. 阶段 1 完成 → `complete_phase` 归档
2. 单独沉淀一条决策 `save_decision`
3. 阶段 2 完成 → 再次归档
4. 模拟状态文件丢失，`recover_from_memory` 从断点机器标记重建状态

注意：示例 2 的「换机器」只换 `.longrun/` 的目录，**记忆后端仍指向同一个临时项目根**。
这正是 `recover_from_memory` 的使用前提 —— 记忆库还在，热状态丢了。

## 观察点

- 断点文本最后一行是 `<!-- longrun-state: {...} -->`
- 每次断点都有 `S_xxxx` 形式的 memory_id
- `resume()` 后状态从 `paused` 变回 `in_progress`，门禁保持
- 恢复出来的 `next_step` / `decisions` 与断点时一致
- 临时目录下出现 `memory.db` / `raw_data/`，而不是路由器仓库的 `data/`

## 自定义路径

| 环境变量 | 用途 |
|----------|------|
| `AGENT_MEMORY_ROUTER_PATH` | 指定 `agent-memory-router-v2` 仓库位置 |
| `LONGRUN_HOME` | 未显式传 `root=` 时，作为项目根（状态 + 默认记忆库） |
| `MEMORY_ROUTER_HOME` | v0.1 兼容；v2 推荐用 `project_path` |
| `LONGRUN_MCP_DEBUG` | 设为 1 时保留 MCP 子进程 stderr |

# 03 · 执行边界：状态机 + 读写门禁

> 状态机负责「能不能从这里到那里」，门禁负责「现在能不能动文件」。
> 两者都不允许被自然语言绕过。

## 项目根

`LongRunAgent(root=项目根)` 同时决定：

- `.longrun/state.json` 的位置（热状态）
- 默认 MCP 后端的 `--project_path`（冷记忆）

两者必须指向同一个项目。`resume()` 跨项目读取会得到错误的 `next_step`，
且大概率在错误的记忆库里找不到断点。

## 状态机

```text
pending ──▶ in_progress ──▶ paused ──▶ in_progress
                 │            ▲
                 ├──▶ blocked ┘
                 ├──▶ waiting_verification ──▶ completed
                 ├──▶ completed（终态）
                 └──▶ failed（终态）
```

| 当前状态 | 允许流向 |
|----------|----------|
| `pending` | `in_progress` |
| `in_progress` | `paused` / `blocked` / `waiting_verification` / `completed` / `failed` |
| `paused` | `in_progress` / `failed` |
| `blocked` | `in_progress` / `failed` |
| `waiting_verification` | `in_progress` / `completed` / `failed` |
| `completed` / `failed` | 无出口 |

**非法流转必须抛 `TransitionError`，不允许「大概齐」跳过去。**

其它硬约束：

- 已有未完成任务时，禁止 `start_task` 覆盖；先 `resume()` 或 `finish()`
- 终态任务禁止再写断点
- 状态文件损坏必须报错，不能静默当成「没有任务」

## 读写门禁

| 模式 | 允许 | 禁止 |
|------|------|------|
| `read_only`（默认） | 读代码、检索记忆、跑只读分析 | 改源码、删文件、`git commit`、外部副作用 |
| `writable` | 上述 + 修改/创建/删除、提交 | 越过用户明确边界的高风险操作 |

规则：

1. 新任务一律从 `read_only` 开始
2. 调研完成、方案确认后，调用 `enable_writes("原因")` 显式解锁
3. 每次写操作前调用 `require_writable("动作")`
4. 回到调研/等待确认时，调用 `disable_writes("原因")` 重新上锁
5. `enable_writes` 必须带原因，原因会写进 `history`，便于事后审计
6. 门禁是基于「当前任务状态」的，不是基于「用户的某句话」的；
   用户说「动手吧」之后仍应先更新状态，再执行写操作

## 哪些算写操作

- 修改/创建/删除仓库文件
- `git add / commit / push`
- 安装依赖、改配置
- 发外部请求产生副作用（下单、提交表单、发消息）
- 任何不可逆操作

以下不算：读取文件、`search_memory`、`read_raw_data`、只读查询、`git status`。

## 恢复边界

- `.longrun/state.json` 是热状态，丢失后可用 `recover_from_memory()` 从断点标记重建
- 重建后默认仍为 `read_only` 还是 `writable`，以标记里的 `access_mode` 为准，
  但恢复后的第一次写操作仍应让用户确认
- `pending_checkpoint.json` 存在，说明上次断点没写进记忆，先补写再继续

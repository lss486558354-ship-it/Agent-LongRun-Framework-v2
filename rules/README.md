# rules/

四份规则，按顺序生效，后面的补充前面的：

| 文件 | 解决的问题 |
|------|-----------|
| `00-entry.md` | 什么时候算长任务 |
| `01-memory-first.md` | 动手前必须查记忆 |
| `02-checkpoint-archive.md` | 中断时必须先写记忆再输出断点 |
| `03-execution-boundary.md` | 状态机与读写门禁 |

## 用法

- **Cursor**：复制到项目的 `.cursor/rules/`，或直接把内容合并进 `.cursorrules`
- **Claude Code**：合并进 `CLAUDE.md`，或在 `CLAUDE.md` 里 `@` 引用本目录
- **纯 MCP 客户端**：把 `01` 和 `02` 的铁律写进 system prompt 即可

根目录的 `.cursorrules` 与 `CLAUDE.md` 已经是压缩版，单独可用；
需要细则时再展开本目录。

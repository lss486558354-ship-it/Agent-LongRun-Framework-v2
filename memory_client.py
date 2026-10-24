"""记忆后端适配层 —— 把三种调用方式统一成同一个接口。

    MCPMemoryBackend   通过 MCP stdio 调 agent-memory-router-v2（默认，推荐）
    LocalMemoryBackend 直接 import agent-memory-router-v2 的 memory_core（最快，示例/离线）
    InMemoryBackend    纯内存实现（测试用，不依赖任何外部项目）

统一接口刻意只保留四个方法：

    search(keyword, type, limit) -> list[dict]   只返回摘要
    read_raw(record_id, max_chars) -> dict       只取一条原文
    save(type, title, content, ...) -> dict      写入并返回记录
    link(source_id, target_id, relation_type)    建立关系

上层 `LongRunAgent` 只依赖这个接口，因此以后换成别的记忆系统
（向量库、Redis、HTTP 服务）时，只需要再写一个后端。

**失败策略**：后端错误一律抛 `MemoryBackendError`。
不允许静默返回空列表 —— 那会让 Agent 误以为"没有历史"，
然后在没有上下文的情况下重新踩一遍坑。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol


class MemoryBackendError(RuntimeError):
    """记忆后端不可用或调用失败。"""


class MemoryBackend(Protocol):
    """四个方法的契约。不继承任何基类，方便 duck typing。"""

    def search(
        self, keyword: str = "", type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    def read_raw(self, record_id: str, max_chars: int = 0) -> dict[str, Any]: ...

    def save(
        self,
        *,
        type: str,
        title: str,
        content: str,
        keywords: str = "",
        relates_to: str = "",
    ) -> dict[str, Any]: ...

    def link(
        self, source_id: str, target_id: str, relation_type: str
    ) -> dict[str, Any]: ...


def _parse_relates_to(raw: str) -> list[tuple[str, str]]:
    """把 `D_0002:supersedes` 解析成 [(ID, 关系)]。格式错就不传关系。"""
    if not raw or ":" not in raw:
        return []
    target, _, relation = raw.partition(":")
    target, relation = target.strip(), relation.strip()
    return [(target, relation)] if target and relation else []


def _default_router_path() -> Path:
    """默认找同级目录下的 agent-memory-router-v2，回退到 v0.1。

    允许用 AGENT_MEMORY_ROUTER_PATH 覆盖 —— 换机器 / 换目录时不必改代码。
    """
    env = os.environ.get("AGENT_MEMORY_ROUTER_PATH")
    if env:
        return Path(env).expanduser().resolve()

    base = Path(__file__).resolve().parent.parent
    for name in ("agent-memory-router-v2", "agent-memory-router"):
        candidate = base / name
        if (candidate / "mcp_server.py").exists():
            return candidate
    return base / "agent-memory-router-v2"


def _resolve_project_path(
    project_path: str | Path | None, home: str | Path | None = None
) -> Path:
    """解析项目根目录。

    优先级：project_path > home（v0.1 兼容别名）> MEMORY_ROUTER_HOME > 当前工作目录。
    """
    candidate = project_path if project_path is not None else home
    if candidate is None:
        candidate = os.environ.get("MEMORY_ROUTER_HOME")
    resolved = (
        Path(candidate).expanduser().resolve() if candidate else Path.cwd().resolve()
    )
    if not resolved.is_dir():
        raise MemoryBackendError(f"项目目录不存在：{resolved}")
    return resolved


# ---------------------------------------------------------------------------
# 纯内存后端（测试 / 演示，不依赖外部项目）
# ---------------------------------------------------------------------------


class InMemoryBackend:
    """最小可用的假记忆库。

    行为与 SQLite 版保持一致：只返回摘要、按 ID 取原文、ID 带类型前缀。
    `calls` 记录调用顺序，测试用它来验证「先检索、后落库」这类硬约束。
    """

    _PREFIXES = {
        "decision": "D",
        "code_change": "C",
        "issue": "I",
        "note": "N",
        "session": "S",
        "reference": "R",
    }

    def __init__(self) -> None:
        self.entities: dict[str, dict[str, Any]] = {}
        self.raw: dict[str, str] = {}
        self.relations: list[tuple[str, str, str]] = []
        self.calls: list[tuple[str, Any]] = []
        self._counters: dict[str, int] = {}
        self._tick = 0

    def _now(self) -> str:
        # 递增秒数，保证排序稳定且不依赖真实时钟
        self._tick += 1
        return f"2026-01-01T00:00:{self._tick:02d}+00:00"

    def _next_id(self, type_: str) -> str:
        prefix = self._PREFIXES.get(type_, "M")
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}_{self._counters[prefix]:04d}"

    def search(
        self, keyword: str = "", type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        self.calls.append(("search", keyword))
        needle = keyword.strip().lower()
        rows = []
        for entity in self.entities.values():
            if type and entity["type"] != type:
                continue
            haystack = " ".join(
                [entity["id"], entity["title"], entity["keywords"]]
            ).lower()
            if needle and needle not in haystack:
                continue
            # 只返回摘要字段，不带 content —— 与真实后端保持一致
            rows.append({k: v for k, v in entity.items() if k != "content"})
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return rows[: max(1, min(limit, 50))]

    def read_raw(self, record_id: str, max_chars: int = 0) -> dict[str, Any]:
        self.calls.append(("read_raw", record_id))
        if record_id not in self.entities:
            raise MemoryBackendError(f"记录不存在：{record_id}")
        text = self.raw[record_id]
        truncated = bool(max_chars and 0 < max_chars < len(text))
        return {
            **self.entities[record_id],
            "content": text[:max_chars] if truncated else text,
            "size_bytes": len(text.encode("utf-8")),
            "truncated": truncated,
        }

    def save(
        self,
        *,
        type: str,
        title: str,
        content: str,
        keywords: str = "",
        relates_to: str = "",
    ) -> dict[str, Any]:
        self.calls.append(("save", title))
        if not title.strip():
            raise MemoryBackendError("title 不能为空")
        record_id = self._next_id(type)
        entity = {
            "id": record_id,
            "type": type,
            "title": title.strip(),
            "keywords": keywords,
            "created_at": self._now(),
            "update_version": 1,
        }
        self.entities[record_id] = entity
        self.raw[record_id] = content
        for target, relation in _parse_relates_to(relates_to):
            self.relations.append((record_id, target, relation))
        return dict(entity)

    def link(
        self, source_id: str, target_id: str, relation_type: str
    ) -> dict[str, Any]:
        self.calls.append(("link", f"{source_id}->{target_id}"))
        if source_id not in self.entities or target_id not in self.entities:
            raise MemoryBackendError("关系两端的记录都必须存在")
        self.relations.append((source_id, target_id, relation_type))
        return {
            "source_id": source_id,
            "target_id": target_id,
            "relation_type": relation_type,
        }


# ---------------------------------------------------------------------------
# 本地直连后端（进程内调用 memory_core）
# ---------------------------------------------------------------------------


class LocalMemoryBackend:
    """直接 import agent-memory-router-v2 的 memory_core。

    适合：示例、离线环境、批处理脚本。
    不适合：Agent 运行时 —— 那应该走 MCP，让记忆服务独立于工作进程。

    `project_path` 是项目根目录，数据库写到 `<project_path>/memory.db`。
    `home` 是 v0.1 的兼容别名；两者同时给出时以 `project_path` 为准。

    注意 `memory_core` 的项目根是**模块级全局配置**，所以同一进程内
    不能同时指向两个不同的项目。需要多项目并行时用 MCPMemoryBackend
    （每个子进程有自己的 `--project_path`）。
    """

    def __init__(
        self,
        router_path: str | Path | None = None,
        project_path: str | Path | None = None,
        home: str | Path | None = None,
    ) -> None:
        self.router_path = (
            Path(router_path).resolve() if router_path else _default_router_path()
        )
        self.project_path = _resolve_project_path(project_path, home)
        self._core = self._import_core()
        try:
            self._core.configure(str(self.project_path))
            self._core.init_db()
        except self._core.MemoryRouterError as exc:
            raise MemoryBackendError(str(exc)) from exc

    def _import_core(self) -> Any:
        core_file = self.router_path / "memory_core.py"
        if not core_file.exists():
            raise MemoryBackendError(
                f"找不到记忆路由核心：{core_file}。"
                "请用 router_path 指定 agent-memory-router-v2 的位置，"
                "或设置环境变量 AGENT_MEMORY_ROUTER_PATH。"
            )

        wanted = self.project_path

        existing = sys.modules.get("memory_core")
        if existing is not None:
            current = Path(getattr(existing, "ROOT", wanted)).resolve()
            if current != wanted:
                raise MemoryBackendError(
                    f"memory_core 已绑定项目 {current}，无法切换到 {wanted}。"
                    "请在新进程中创建后端，或改用 MCPMemoryBackend。"
                )
            return existing

        if str(self.router_path) not in sys.path:
            sys.path.insert(0, str(self.router_path))
        try:
            return importlib.import_module("memory_core")
        except Exception as exc:  # noqa: BLE001 - 依赖方错误需原样暴露
            raise MemoryBackendError(
                f"导入 memory_core 失败：{exc}"
            ) from exc

    def _wrap(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except self._core.MemoryRouterError as exc:
            raise MemoryBackendError(str(exc)) from exc

    def search(
        self, keyword: str = "", type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        return self._wrap(
            self._core.search_memory, keyword, type, limit=limit
        )

    def read_raw(self, record_id: str, max_chars: int = 0) -> dict[str, Any]:
        return self._wrap(
            self._core.read_raw_data, record_id, max_chars=max_chars or None
        )

    def save(
        self,
        *,
        type: str,
        title: str,
        content: str,
        keywords: str = "",
        relates_to: str = "",
    ) -> dict[str, Any]:
        return self._wrap(
            self._core.add_entity,
            type=type,
            title=title,
            content=content,
            keywords=keywords,
            relations=_parse_relates_to(relates_to),
        )

    def link(
        self, source_id: str, target_id: str, relation_type: str
    ) -> dict[str, Any]:
        return self._wrap(
            self._core.add_relation, source_id, target_id, relation_type
        )


# ---------------------------------------------------------------------------
# MCP 后端（真正走 MCP 协议）
# ---------------------------------------------------------------------------


class MCPMemoryBackend:
    """通过 MCP stdio 调用 agent-memory-router-v2 的 4 个工具。

    每次调用会拉起一个 `mcp_server.py` 子进程，并通过 `--project_path`
    绑定到指定项目根目录。不同项目使用不同后端实例，互不共享记忆。

    `home` 是 v0.1 的兼容别名，等价于 `project_path`。

    为什么不做长连接？

    - start_task / checkpoint 都是低频操作（一个阶段才几次）
    - 短连接不会留下僵尸进程，异常恢复更简单
    - 真需要高频调用时，可以在子进程里直接算，而不是反复握手

    调用失败一律抛 MemoryBackendError，绝不返回空结果。
    """

    def __init__(
        self,
        server_path: str | Path | None = None,
        router_path: str | Path | None = None,
        project_path: str | Path | None = None,
        home: str | Path | None = None,
        python: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.router_path = (
            Path(router_path).resolve() if router_path else _default_router_path()
        )
        self.project_path = _resolve_project_path(project_path, home)
        self.server_path = (
            Path(server_path).resolve()
            if server_path
            else self.router_path / "mcp_server.py"
        )
        if not self.server_path.exists():
            raise MemoryBackendError(
                f"找不到 MCP Server：{self.server_path}。"
                "请用 server_path 指定，或设置 AGENT_MEMORY_ROUTER_PATH。"
            )

        self.command = python or sys.executable
        self.timeout_seconds = timeout_seconds
        self.env = {
            **os.environ,
            # 子进程强制 UTF-8：中文 Windows 默认 GBK，会把 JSON 里的中文打坏
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }

    def _server_args(self) -> list[str]:
        return [
            str(self.server_path),
            "--project_path",
            str(self.project_path),
        ]

    # -- 同步封装 -----------------------------------------------------------

    def _call_sync(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return asyncio.run(self._call(tool, arguments))
        except MemoryBackendError:
            raise
        except RuntimeError as exc:
            if "asyncio.run() cannot be called from a running event loop" in str(exc):
                raise MemoryBackendError(
                    "检测到已有事件循环。LongRunAgent 是同步 API，"
                    "请在同步上下文调用，或使用 MCPMemoryBackend._call 的异步版本。"
                ) from exc
            raise MemoryBackendError(f"MCP 调用 {tool} 失败：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 子进程错误类型不可控
            raise MemoryBackendError(f"MCP 调用 {tool} 失败：{exc}") from exc

    async def _call(
        self, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            from mcp.client.session import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError as exc:  # pragma: no cover
            raise MemoryBackendError(
                "缺少 mcp 依赖：pip install 'mcp>=2.0'"
            ) from exc

        params = StdioServerParameters(
            command=self.command,
            args=self._server_args(),
            env=self.env,
            # 显式 --project_path 已经绑定项目；cwd 也指向项目根，
            # 避免 Server 里任何相对路径回落到路由器目录。
            cwd=str(self.project_path),
        )

        debug = bool(os.environ.get("LONGRUN_MCP_DEBUG"))
        errlog = sys.stderr if debug else open(os.devnull, "w", encoding="utf-8")
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        tool,
                        arguments,
                        read_timeout_seconds=self.timeout_seconds,
                    )
                    text = _extract_text(result)
                    payload = json.loads(text)
                    if not payload.get("ok", False):
                        raise MemoryBackendError(
                            payload.get("error") or f"MCP 工具 {tool} 返回失败"
                        )
                    return payload
        finally:
            if not debug:
                errlog.close()

    # -- 契约实现 -----------------------------------------------------------

    def search(
        self, keyword: str = "", type: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        payload = self._call_sync(
            "search_memory",
            {"keyword": keyword, "type": type, "limit": limit},
        )
        return list(payload.get("results") or [])

    def read_raw(self, record_id: str, max_chars: int = 0) -> dict[str, Any]:
        payload = self._call_sync(
            "read_raw_data",
            {"record_id": record_id, "max_chars": max_chars},
        )
        return {k: v for k, v in payload.items() if k != "ok"}

    def save(
        self,
        *,
        type: str,
        title: str,
        content: str,
        keywords: str = "",
        relates_to: str = "",
    ) -> dict[str, Any]:
        payload = self._call_sync(
            "save_memory",
            {
                "type": type,
                "title": title,
                "content": content,
                "keywords": keywords,
                "relates_to": relates_to,
            },
        )
        return {k: v for k, v in payload.items() if k != "ok"}

    def link(
        self, source_id: str, target_id: str, relation_type: str
    ) -> dict[str, Any]:
        payload = self._call_sync(
            "link_memory",
            {
                "source_id": source_id,
                "target_id": target_id,
                "relation_type": relation_type,
            },
        )
        return {k: v for k, v in payload.items() if k != "ok"}


def _extract_text(result: Any) -> str:
    """从 CallToolResult 里取出文本内容。

    MCP 2.0 的 content 是「内容块」列表，不一定是纯 TextContent；
    这里只拼接带 text 属性的块，其余块（图片等）忽略。
    """
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    if not parts:
        raise MemoryBackendError("MCP 工具没有返回文本内容")
    return "\n".join(parts)

"""胶水层：把「状态机」和「记忆路由」串成一个可恢复的长任务 Agent。

核心不是多一个类，而是三条**顺序约束**：

1. `start_task()` 必须先查记忆，再创建任务状态。
   —— 否则 Agent 会在没有历史的情况下重新踩坑。

2. `checkpoint()` 必须先把进度写进记忆库，写入成功后才返回断点文本。
   —— 写入失败就抛异常并落本地待补，绝不"先输出断点、回头再存"。
   顺序反了，一次进程崩溃就会让断点永远消失。

3. 读写门禁默认是 READ_ONLY。
   —— 只有显式 `enable_writes()` 之后才能做写操作。
   调研阶段误改代码，比慢一点危险得多。

用法：

    agent = LongRunAgent(root="D:/my-project")   # 默认 MCP 后端绑定该项目
    start = agent.start_task("实现报价平台适配器", keywords="报价,适配器")
    agent.enable_writes("已确认方案，进入实施")
    ...
    cp = agent.checkpoint(reason="token 预警", next_step="接入第二个平台")
    print(cp.text)                               # 此时记忆已经落库

    # 新进程里恢复（同一个项目根）
    agent2 = LongRunAgent(root="D:/my-project")
    bundle = agent2.resume()
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from memory_client import (  # noqa: F401 - 重新导出，调用方只需 import 本模块
    InMemoryBackend,
    LocalMemoryBackend,
    MCPMemoryBackend,
    MemoryBackend,
    MemoryBackendError,
)
from state_machine import (
    AccessMode,
    StateCorruptError,  # noqa: F401 - 方便调用方捕获
    StateStore,
    TaskState,
    TaskStatus,
    TERMINAL_STATUSES,
    TransitionError,
    now_iso,
)

__all__ = [
    "AccessMode",
    "Checkpoint",
    "CheckpointError",
    "CheckpointWriteError",
    "InMemoryBackend",
    "LocalMemoryBackend",
    "LongRunAgent",
    "LongRunError",
    "MCPMemoryBackend",
    "MemoryBackendError",
    "NoActiveTask",
    "RecoveryBundle",
    "TaskStart",
    "TaskState",
    "TaskStatus",
    "TransitionError",
    "WriteGateViolation",
    "extract_keywords",
]


class LongRunError(RuntimeError):
    """本模块所有可预期错误的基类。"""


class NoActiveTask(LongRunError):
    """没有可恢复的任务。"""


class CheckpointError(LongRunError):
    """断点相关错误。"""


class CheckpointWriteError(CheckpointError):
    """记忆写入失败 —— 断点文本被刻意扣住，不返回给调用方。"""


class WriteGateViolation(LongRunError):
    """只读模式下尝试写操作。"""


# ---------------------------------------------------------------------------
# 返回结构
# ---------------------------------------------------------------------------


@dataclass
class TaskStart:
    state: TaskState
    memories: list[dict[str, Any]]
    context: list[dict[str, Any]]
    summary: str


@dataclass
class Checkpoint:
    memory_id: str
    state: TaskState
    text: str
    kind: str


@dataclass
class RecoveryBundle:
    state: TaskState
    checkpoint: dict[str, Any] | None
    related: list[dict[str, Any]]
    summary: str


# ---------------------------------------------------------------------------
# 关键词提取
# ---------------------------------------------------------------------------

# 这些词单独作为检索词几乎没有区分度
_STOPWORDS = {
    "实现", "修复", "完成", "处理", "支持", "增加", "修改", "优化",
    "一个", "这个", "那个", "如何", "怎么", "以及", "并且", "需要",
    "进行", "我们", "你们", "他们", "可以", "问题", "功能", "任务",
    "the", "and", "for", "with", "fix", "add", "update", "implement",
    "task", "issue", "code", "test",
}

# 含这些字的二字词多半是虚词片段（"断的""被中"），检索价值低
_WEAK_CHARS = set("的了是在和与把对从到为着过")

_CJK_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,}|[一-鿿]{2,}")


def extract_keywords(text: str, limit: int = 6) -> list[str]:
    """从任务描述里提取检索词。

    真实项目里更推荐调用方显式传 keywords —— 记忆库是关键词语义，
    不是分词器。这里只是给 `start_task()` 一个不至于空转的兜底。

    中文没有分词，用二字滑窗近似。命中率不如人工关键词，但比整句
    塞进 LIKE 查询（永远匹配不到）强得多。
    """
    candidates: list[str] = []
    for run in _CJK_RUN_RE.findall(text or ""):
        if run[0].isascii():
            token = run.lower()
            if token not in _STOPWORDS:
                candidates.append(token)
            continue

        if len(run) <= 3:
            # 全是"的了是"这类虚词字符的片段没有检索价值
            if run not in _STOPWORDS and not all(ch in _WEAK_CHARS for ch in run):
                candidates.append(run)
            continue

        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram in _STOPWORDS:
                continue
            if any(ch in _WEAK_CHARS for ch in bigram):
                continue
            candidates.append(bigram)

    seen: set[str] = set()
    ordered: list[str] = []
    for token in candidates:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(token)
        if len(ordered) >= limit:
            break
    return ordered


def _normalize_keywords(keywords: str | Iterable[str] | None) -> list[str]:
    if keywords is None:
        return []
    if isinstance(keywords, str):
        parts = re.split(r"[,，;；\s]+", keywords)
    else:
        parts = [str(k) for k in keywords]
    return [p.strip() for p in parts if p and p.strip()]


def _short(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


_STATE_MARKER_RE = re.compile(r"<!--\s*longrun-state:\s*(\{[^\n]*\})\s*-->")


# ---------------------------------------------------------------------------
# LongRunAgent
# ---------------------------------------------------------------------------


class LongRunAgent:
    """状态机 + 记忆路由的统一门面。"""

    def __init__(
        self,
        memory: MemoryBackend | None = None,
        *,
        root: str | Path | None = None,
        store: StateStore | None = None,
        max_memories: int = 5,
        max_search_terms: int = 4,
        hydrate_top: int = 1,
        strict_memory: bool = True,
    ) -> None:
        """
        Args:
            memory: 记忆后端。默认 `MCPMemoryBackend(project_path=项目根)`，
                    即真正走 MCP，并绑定到同一个项目。
            root: 项目根目录。状态文件写到 `<root>/.longrun/`，
                  默认记忆库写到 `<root>/memory.db`。默认取环境变量
                  LONGRUN_HOME 或当前工作目录。
            store: 直接传入 StateStore，优先级高于 root。
            max_memories: 单次任务最多带多少条摘要进上下文。
            max_search_terms: 最多用几个关键词去搜（每个关键词一次后端调用）。
            hydrate_top: 自动加载多少条原文。0 表示只给摘要，原文交给 Agent 按需取。
            strict_memory: True 时记忆后端报错直接失败（铁律默认值）；
                           False 时记录为空继续，适合首次搭建记忆库的场景。
        """
        if store is not None:
            project_root = Path(store.root).expanduser().resolve()
            self.store = store
        else:
            project_root = Path(
                root or os.environ.get("LONGRUN_HOME") or Path.cwd()
            ).expanduser().resolve()
            self.store = StateStore(project_root)

        # 状态与默认记忆库必须指向同一个项目，否则 resume 会读错历史。
        self.memory: MemoryBackend = (
            memory if memory is not None else MCPMemoryBackend(project_path=project_root)
        )
        self.max_memories = max(1, int(max_memories))
        self.max_search_terms = max(1, int(max_search_terms))
        self.hydrate_top = max(0, int(hydrate_top))
        self.strict_memory = strict_memory

        # 启动即加载上一次的状态。状态文件损坏时直接报错 ——
        # 静默当成「没有任务」会让 Agent 覆盖掉真实进度。
        self._state: TaskState | None = self.store.load()

    # -- 任务生命周期 -------------------------------------------------------

    def start_task(
        self,
        task_desc: str,
        *,
        keywords: str | Iterable[str] | None = None,
        access_mode: AccessMode = AccessMode.READ_ONLY,
        hydrate_top: int | None = None,
        task_id: str | None = None,
    ) -> TaskStart:
        """开始长任务。**先检索记忆，再建状态。**

        默认进入 READ_ONLY：调研阶段不应该改代码。
        确认方案后调用 `enable_writes()`。
        """
        task_desc = (task_desc or "").strip()
        if not task_desc:
            raise LongRunError("task_desc 不能为空。")

        if self._state and self._state.status not in TERMINAL_STATUSES:
            raise LongRunError(
                f"已有未完成任务 {self._state.task_id}"
                f"（{self._state.status.value}）。"
                "请先 resume() 继续，或 finish() 结束，不要直接覆盖。"
            )

        # 铁律 1：任务开始先查记忆。
        terms = _normalize_keywords(keywords) or extract_keywords(task_desc)
        memories = self._search_for_task(terms)

        # 只自动加载最相关的少量原文；剩下的留给 Agent 显式 read_raw_data。
        top_n = self.hydrate_top if hydrate_top is None else max(0, int(hydrate_top))
        context = self._hydrate(memories, top_n)

        state = TaskState(
            task_id=task_id or self._new_task_id(),
            description=task_desc,
            access_mode=AccessMode(access_mode),
        )
        state.transition(TaskStatus.IN_PROGRESS, "start_task")
        self.store.save(state)
        self._state = state

        summary = self._start_summary(state, memories, context)
        return TaskStart(state=state, memories=memories, context=context, summary=summary)

    def checkpoint(
        self,
        *,
        reason: str = "",
        summary: str = "",
        next_step: str = "",
        decisions: Iterable[str] | None = None,
        kind: str = "interrupt",
        phase: str | None = None,
        status: TaskStatus | str | None = None,
    ) -> Checkpoint:
        """写断点。**先写记忆，成功后才返回断点文本。**

        Args:
            reason: 为什么触发断点（如「token 预警」「等待用户确认」）。
            summary: 本阶段完成了什么。
            next_step: 下一步做什么。恢复时 Agent 第一眼看的就是它。
            decisions: 本阶段做出的关键决策，会一起写进记忆原文。
            kind: interrupt / phase_complete / blocked / complete。
            phase: 覆盖当前阶段名。
            status: 显式指定目标状态，优先级最高。
        """
        if kind not in {"interrupt", "phase_complete", "blocked", "complete"}:
            raise CheckpointError(f"未知断点类型：{kind}")

        state = self._require_state()
        if state.status in TERMINAL_STATUSES:
            raise CheckpointError(
                f"任务 {state.task_id} 已处于终态 {state.status.value}，不能再写断点。"
            )

        # 在副本上改，记忆写失败时不污染内存中的真实状态。
        working = TaskState.from_dict(state.to_dict())

        if phase:
            working.phase = phase
        if next_step:
            working.next_step = next_step
        if decisions:
            working.decisions.extend(str(d) for d in decisions)

        if summary:
            if kind == "phase_complete":
                working.completed_steps.append(f"{working.phase}: {summary}")
            else:
                working.completed_steps.append(summary)

        target = self._target_status(kind, status)
        if target is not None and target is not working.status:
            working.transition(target, f"checkpoint:{kind}")

        content = self._render_checkpoint(working, kind=kind, reason=reason, summary=summary)
        title = self._checkpoint_title(working, kind)

        # 铁律 2：save_memory 必须先于「输出断点」。
        try:
            record = self.memory.save(
                type="session",
                title=title,
                content=content,
                keywords=f"longrun,checkpoint,{working.task_id}",
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都不能放行断点
            fallback = self._write_fallback(working, kind, reason, summary, content)
            raise CheckpointWriteError(
                "记忆写入失败，断点未输出（规则：必须先写记忆，再输出断点）。"
                f"已暂存到 {fallback}，恢复记忆服务后重新调用 checkpoint()。"
            ) from exc

        record_id = str(record["id"])
        working.checkpoint_id = record_id
        working.checkpoint_memory_ids.append(record_id)
        working.updated_at = now_iso()
        self.store.save(working)
        self._state = working

        return Checkpoint(
            memory_id=record_id, state=working, text=content, kind=kind
        )

    def complete_phase(
        self,
        phase_name: str,
        summary: str,
        *,
        decisions: Iterable[str] | None = None,
        next_step: str = "",
    ) -> Checkpoint:
        """阶段完成归档。状态保持 in_progress，阶段写入 completed_steps。"""
        return self.checkpoint(
            kind="phase_complete",
            phase=phase_name,
            summary=summary,
            decisions=decisions,
            next_step=next_step,
            reason="阶段完成",
        )

    def finish(self, summary: str, *, next_step: str = "") -> Checkpoint:
        """结束任务。写完结档记忆后进入 completed 终态。"""
        return self.checkpoint(
            kind="complete",
            summary=summary,
            next_step=next_step,
            reason="任务完成",
        )

    def note_blocked(self, reason: str, *, next_step: str = "") -> Checkpoint:
        """外部依赖阻塞时使用。进入 blocked，解除后 resume()。"""
        return self.checkpoint(
            kind="blocked", reason=reason, next_step=next_step, summary=""
        )

    # -- 恢复 ---------------------------------------------------------------

    def resume(self) -> RecoveryBundle:
        """从本地状态恢复。

        顺序：加载状态 → 校验/修复状态机 → 按需读最近断点原文 → 返回。
        """
        state = self.store.load()
        if state is None:
            raise NoActiveTask(
                "没有本地任务状态。若换过机器/删过 .longrun，"
                "请用 recover_from_memory(关键词或任务ID) 从记忆库重建。"
            )
        if state.status in TERMINAL_STATUSES:
            raise NoActiveTask(
                f"任务 {state.task_id} 已结束（{state.status.value}），没有可恢复的进度。"
            )

        working = TaskState.from_dict(state.to_dict())
        if working.status in {
            TaskStatus.PENDING,
            TaskStatus.PAUSED,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_VERIFICATION,
        }:
            working.transition(TaskStatus.IN_PROGRESS, "resume")

        checkpoint = None
        if working.checkpoint_id:
            try:
                checkpoint = self.memory.read_raw(working.checkpoint_id)
            except MemoryBackendError:
                # 断点原文缺失不应阻断恢复：本地状态里已有 next_step / phase。
                checkpoint = None

        related = self._safe_search(working.task_id)
        self.store.save(working)
        self._state = working

        summary = (
            f"已恢复任务 {working.task_id}（{working.status.value}）。"
            f"阶段：{working.phase}；下一步：{working.next_step or '未指定'}。"
        )
        return RecoveryBundle(
            state=working, checkpoint=checkpoint, related=related, summary=summary
        )

    def recover_from_memory(self, keyword: str, *, limit: int = 20) -> RecoveryBundle:
        """本地状态丢失时，从记忆库里的断点标记重建状态。

        断点原文末尾有一行 `<!-- longrun-state: {...} -->`，
        这里把它解析回来。没有这一行就跳过 —— 宁可报「找不到」，
        也不要从自然语言里猜状态。
        """
        rows = self.memory.search(keyword or "longrun", limit=limit)
        candidates = [r for r in rows if r.get("type") == "session"]

        for row in candidates:
            record_id = str(row.get("id", ""))
            if not record_id:
                continue
            try:
                payload = self.memory.read_raw(record_id)
            except MemoryBackendError:
                continue
            match = _STATE_MARKER_RE.search(payload.get("content", ""))
            if not match:
                continue
            try:
                data = json.loads(match.group(1))
                state = TaskState.from_dict(data)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

            state.checkpoint_id = record_id
            state.checkpoint_memory_ids = sorted(
                set(state.checkpoint_memory_ids) | {record_id}
            )
            state.history.append(
                {
                    "from": "memory",
                    "to": state.status.value,
                    "reason": f"recover_from_memory:{record_id}",
                    "at": now_iso(),
                }
            )
            self.store.save(state)
            self._state = state

            related = [r for r in rows if r.get("id") != record_id]
            summary = (
                f"已从记忆 {record_id} 重建任务 {state.task_id}。"
                f"状态：{state.status.value}；下一步：{state.next_step or '未指定'}。"
            )
            return RecoveryBundle(
                state=state, checkpoint=payload, related=related, summary=summary
            )

        raise NoActiveTask(
            f"在记忆库里找不到带状态标记的断点（检索词：{keyword!r}）。"
            "请确认任务开始时用的是同一个记忆库。"
        )

    # -- 读写门禁 -----------------------------------------------------------

    def enable_writes(self, reason: str = "") -> TaskState:
        """切换到可写模式。必须在调研完成、方案确认之后调用。"""
        return self._set_access_mode(AccessMode.WRITABLE, reason)

    def disable_writes(self, reason: str = "") -> TaskState:
        return self._set_access_mode(AccessMode.READ_ONLY, reason)

    def require_writable(self, action: str = "写操作") -> None:
        """执行任何修改代码/文件的操作前调用。"""
        state = self._require_state()
        if state.access_mode is not AccessMode.WRITABLE:
            raise WriteGateViolation(
                f"当前是只读模式，拒绝执行「{action}」。"
                "先完成调研并调用 enable_writes(原因) 显式解锁。"
            )

    def _set_access_mode(self, mode: AccessMode, reason: str) -> TaskState:
        state = self._require_state()
        if state.status in TERMINAL_STATUSES:
            raise LongRunError("任务已结束，不能再切换读写门禁。")
        previous = state.access_mode
        state.access_mode = mode
        state.note(
            f"access_mode: {previous.value} → {mode.value}"
            + (f"（{reason}）" if reason else "")
        )
        self.store.save(state)
        self._state = state
        return state

    # -- 决策记录 -----------------------------------------------------------

    def save_decision(
        self,
        title: str,
        content: str,
        *,
        keywords: str = "",
    ) -> dict[str, Any]:
        """单独沉淀一条重大决策（type=decision）。

        断点记录的是「进度」，决策记录的是「为什么」。两者分开存，
        以后检索「当时为什么选 A」时才能精准命中。
        """
        state = self._state or self.store.load()
        task_id = state.task_id if state else ""
        keyword_text = ",".join(
            p for p in ("longrun", task_id, keywords) if p
        )
        relates_to = (
            f"{state.checkpoint_id}:related_to"
            if state and state.checkpoint_id
            else ""
        )
        return self.memory.save(
            type="decision",
            title=title,
            content=content,
            keywords=keyword_text,
            relates_to=relates_to,
        )

    # -- 查询 ---------------------------------------------------------------

    def status(self) -> dict[str, Any] | None:
        state = self._state or self.store.load()
        return state.to_dict() if state else None

    # -- 内部 ---------------------------------------------------------------

    def _require_state(self) -> TaskState:
        if self._state is None:
            raise NoActiveTask("当前没有活动任务，先调用 start_task()。")
        return self._state

    def _new_task_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"T_{stamp}_{secrets.token_hex(2)}"

    def _search_for_task(self, terms: list[str]) -> list[dict[str, Any]]:
        if not terms:
            try:
                return self.memory.search("", limit=self.max_memories)
            except MemoryBackendError:
                if self.strict_memory:
                    raise
                return []

        found: dict[str, dict[str, Any]] = {}
        for term in terms[: self.max_search_terms]:
            try:
                rows = self.memory.search(term, limit=self.max_memories)
            except MemoryBackendError:
                if self.strict_memory:
                    raise
                continue
            for row in rows:
                rid = str(row.get("id", ""))
                if rid:
                    found.setdefault(rid, row)
            if len(found) >= self.max_memories:
                break
        return list(found.values())[: self.max_memories]

    def _hydrate(
        self, memories: list[dict[str, Any]], count: int
    ) -> list[dict[str, Any]]:
        hydrated: list[dict[str, Any]] = []
        for row in memories[:count]:
            rid = str(row.get("id", ""))
            if not rid:
                continue
            try:
                hydrated.append(self.memory.read_raw(rid))
            except MemoryBackendError:
                if self.strict_memory:
                    raise
        return hydrated

    def _safe_search(self, keyword: str) -> list[dict[str, Any]]:
        try:
            return self.memory.search(keyword, limit=self.max_memories)
        except MemoryBackendError:
            return []

    def _target_status(
        self, kind: str, status: TaskStatus | str | None
    ) -> TaskStatus | None:
        if status is not None:
            return status if isinstance(status, TaskStatus) else TaskStatus(status)
        return {
            "interrupt": TaskStatus.PAUSED,
            "blocked": TaskStatus.BLOCKED,
            "complete": TaskStatus.COMPLETED,
            "phase_complete": None,  # 阶段完成不改变总状态
        }[kind]

    def _start_summary(
        self,
        state: TaskState,
        memories: list[dict[str, Any]],
        context: list[dict[str, Any]],
    ) -> str:
        parts = [
            f"已先检索记忆：命中 {len(memories)} 条，加载 {len(context)} 条原文。",
            f"当前读写门禁：{state.access_mode.value}。",
        ]
        if context:
            parts.append("请先阅读 context 中的原文，再决定是否进入实施。")
        elif memories:
            parts.append("只拿到了摘要；需要细节时按 ID 调用 read_raw_data。")
        else:
            parts.append("没有命中相关历史，本次按新任务处理。")
        if state.access_mode is AccessMode.READ_ONLY:
            parts.append("修改代码前必须调用 enable_writes(原因)。")
        return " ".join(parts)

    def _checkpoint_title(self, state: TaskState, kind: str) -> str:
        short = _short(state.description, 28)
        if kind == "phase_complete":
            return f"[阶段完成] {state.phase} · {short}"
        if kind == "blocked":
            return f"[阻塞] {short} @ {state.phase}"
        if kind == "complete":
            return f"[完成] {short}"
        return f"[断点] {short} @ {state.phase}"

    def _render_checkpoint(
        self,
        state: TaskState,
        *,
        kind: str,
        reason: str,
        summary: str,
    ) -> str:
        lines = [
            "[LongRun Checkpoint]",
            f"任务: {state.description}",
            f"任务ID: {state.task_id}",
            f"类型: {kind}",
            f"状态: {state.status.value}",
            f"读写门禁: {state.access_mode.value}",
            f"阶段: {state.phase}",
        ]
        if reason:
            lines.append(f"触发原因: {reason}")
        if summary:
            lines.append(f"本阶段完成: {summary}")
        if state.completed_steps:
            lines.append("已完成:")
            lines.extend(f"  - {item}" for item in state.completed_steps[-10:])
        if state.decisions:
            lines.append("关键决策:")
            lines.extend(f"  - {item}" for item in state.decisions[-10:])
        lines.append(f"下一步: {state.next_step or '未指定'}")
        lines.append(f"时间: {now_iso()}")

        # 机器可读的状态标记。recover_from_memory() 靠它重建，不靠自然语言猜测。
        marker = state.to_dict()
        marker.pop("history", None)
        lines.append("")
        lines.append(
            "<!-- longrun-state: "
            + json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
            + " -->"
        )
        return "\n".join(lines)

    def _write_fallback(
        self,
        state: TaskState,
        kind: str,
        reason: str,
        summary: str,
        content: str,
    ) -> Path:
        """记忆库写不进去时的本地兜底。"""
        self.store.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state.to_dict(),
            "kind": kind,
            "reason": reason,
            "summary": summary,
            "content": content,
            "at": now_iso(),
        }
        tmp = self.store.fallback_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.store.fallback_path)
        return self.store.fallback_path

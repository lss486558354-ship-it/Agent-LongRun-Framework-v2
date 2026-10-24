"""长任务状态机 —— 从 engineering-workflow 的模块状态语义移植而来。

原项目用 `project-profile.json` 管理模块状态：

    pending → in_progress → waiting_verification → completed
                  │                │
                  └── blocked ←────┘

本模块保留这四个状态，并新增：

    paused    token 预警 / 主动断点 / 进程被杀 → 可由 resume() 回到 in_progress
    failed    不可恢复的终态

**所有状态变更必须经过 `TaskState.transition()`**，非法流转直接抛异常。
状态机存在的意义，就是让「跳跃式前进」在代码层面不可能发生 ——
只写在 Rule 里的约束，模型随时可以忘掉；写进状态机的约束，忘不掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class TaskStatus(str, Enum):
    """任务状态。值刻意用字符串，方便直接写进 JSON 与记忆库。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    BLOCKED = "blocked"
    WAITING_VERIFICATION = "waiting_verification"
    COMPLETED = "completed"
    FAILED = "failed"


class AccessMode(str, Enum):
    """读写门禁。

    READ_ONLY    调研 / 规划 / 读历史。可以查记忆，但不能改代码。
    WRITABLE     实施阶段。只有显式调用 enable_writes() 才会进入。
    """

    READ_ONLY = "read_only"
    WRITABLE = "writable"


# 状态流转白名单。任何不在这里的跳转都会被拒绝。
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.IN_PROGRESS},
    TaskStatus.IN_PROGRESS: {
        TaskStatus.PAUSED,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_VERIFICATION,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    },
    TaskStatus.PAUSED: {TaskStatus.IN_PROGRESS, TaskStatus.FAILED},
    TaskStatus.BLOCKED: {TaskStatus.IN_PROGRESS, TaskStatus.FAILED},
    TaskStatus.WAITING_VERIFICATION: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
}

TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED})

# 历史只保留最近 N 条，避免 state.json 无限增长。
MAX_HISTORY = 20


class StateMachineError(RuntimeError):
    """状态机相关错误的基类。"""


class TransitionError(StateMachineError):
    """尝试了白名单之外的流转。"""


class StateCorruptError(StateMachineError):
    """state.json 无法解析。宁可报错，也不要静默丢失进度。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TaskState:
    """一个长任务的完整状态。

    字段分三类：
    1. 身份：task_id / description / created_at
    2. 执行：status / access_mode / phase / completed_steps / next_step
    3. 记忆：decisions / checkpoint_id / checkpoint_memory_ids

    `checkpoint_memory_ids` 保存所有断点记忆的 ID。恢复时先看最新的那条，
    但保留全部 ID 是为了让 Agent 能按需回溯更早的断点。
    """

    task_id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    access_mode: AccessMode = AccessMode.READ_ONLY
    phase: str = "研究"
    completed_steps: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    next_step: str = ""
    checkpoint_id: str | None = None
    checkpoint_memory_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    history: list[dict[str, Any]] = field(default_factory=list)

    # -- 流转 ---------------------------------------------------------------

    def can_transition_to(self, target: TaskStatus) -> bool:
        return target in TRANSITIONS.get(self.status, set())

    def transition(self, target: TaskStatus, reason: str = "") -> None:
        if not isinstance(target, TaskStatus):
            target = TaskStatus(target)
        if target not in TRANSITIONS.get(self.status, set()):
            allowed = ", ".join(sorted(s.value for s in TRANSITIONS.get(self.status, set())))
            raise TransitionError(
                f"非法状态流转：{self.status.value} → {target.value}。"
                f"当前状态允许的目标：{allowed or '无（终态）'}。"
            )
        self.history.append(
            {
                "from": self.status.value,
                "to": target.value,
                "reason": reason,
                "at": now_iso(),
            }
        )
        self.history = self.history[-MAX_HISTORY:]
        self.status = target
        self.updated_at = now_iso()

    def note(self, message: str) -> None:
        """记录一条不改状态的事件（如切换读写门禁）。"""
        self.history.append(
            {
                "from": self.status.value,
                "to": self.status.value,
                "reason": message,
                "at": now_iso(),
            }
        )
        self.history = self.history[-MAX_HISTORY:]
        self.updated_at = now_iso()

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """显式转换枚举。直接 asdict() 会留下 Enum 对象，json.dumps 会炸。"""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "status": self.status.value,
            "access_mode": self.access_mode.value,
            "phase": self.phase,
            "completed_steps": list(self.completed_steps),
            "decisions": list(self.decisions),
            "next_step": self.next_step,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_memory_ids": list(self.checkpoint_memory_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        return cls(
            task_id=str(data["task_id"]),
            description=str(data.get("description", "")),
            status=TaskStatus(data.get("status", "pending")),
            access_mode=AccessMode(data.get("access_mode", "read_only")),
            phase=str(data.get("phase", "研究")),
            completed_steps=list(data.get("completed_steps") or []),
            decisions=list(data.get("decisions") or []),
            next_step=str(data.get("next_step", "")),
            checkpoint_id=data.get("checkpoint_id"),
            checkpoint_memory_ids=list(data.get("checkpoint_memory_ids") or []),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            history=list(data.get("history") or [])[-MAX_HISTORY:],
        )


class StateStore:
    """把任务状态落到本地 JSON。

    为什么还要本地落盘，既然记忆库里也有？

    - 记忆库是**冷数据**：适合历史回溯，不适合每次状态判断都去 MCP 绕一圈
    - state.json 是**热状态**：resume 时毫秒级读取，且不依赖 MCP 进程活着

    两者互为备份：本地丢了从记忆里重建，记忆写失败则先落本地待补。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.dir = self.root / ".longrun"
        self.path = self.dir / "state.json"
        self.fallback_path = self.dir / "pending_checkpoint.json"

    def load(self) -> TaskState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorruptError(
                f"状态文件损坏：{self.path}。"
                "不要猜，先修 JSON 或从记忆库恢复（recover_from_memory）。"
            ) from exc
        return TaskState.from_dict(data)

    def save(self, state: TaskState) -> None:
        # 先写临时文件再改名：进程被杀时不会留下半截 JSON。
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

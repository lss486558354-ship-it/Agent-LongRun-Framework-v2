"""状态机的行为测试。

核心断言只有一句：**非法流转必须失败**。
如果状态机允许跳跃，那它就只是装饰品。
"""

from __future__ import annotations

import json

import pytest

from state_machine import (
    AccessMode,
    StateCorruptError,
    StateStore,
    TaskState,
    TaskStatus,
    TransitionError,
)


def make_state(**overrides: object) -> TaskState:
    payload = {"task_id": "T_TEST", "description": "测试任务"}
    payload.update(overrides)
    return TaskState(**payload)  # type: ignore[arg-type]


def test_pending_to_in_progress() -> None:
    state = make_state()

    state.transition(TaskStatus.IN_PROGRESS, "开始")

    assert state.status is TaskStatus.IN_PROGRESS
    assert state.history[-1]["from"] == "pending"
    assert state.history[-1]["to"] == "in_progress"


def test_invalid_transition_is_rejected() -> None:
    state = make_state()

    # pending 不能直接跳到 completed
    with pytest.raises(TransitionError, match="pending → completed"):
        state.transition(TaskStatus.COMPLETED)


def test_paused_can_resume() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.transition(TaskStatus.PAUSED, "token 预警")

    state.transition(TaskStatus.IN_PROGRESS, "resume")

    assert state.status is TaskStatus.IN_PROGRESS


def test_blocked_can_resume() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.transition(TaskStatus.BLOCKED, "等待账号")

    state.transition(TaskStatus.IN_PROGRESS, "阻塞解除")

    assert state.status is TaskStatus.IN_PROGRESS


@pytest.mark.parametrize("terminal", [TaskStatus.COMPLETED, TaskStatus.FAILED])
def test_terminal_states_have_no_exits(terminal: TaskStatus) -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.transition(terminal)

    with pytest.raises(TransitionError):
        state.transition(TaskStatus.IN_PROGRESS)


def test_history_is_capped() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    for i in range(30):
        state.note(f"事件 {i}")

    assert len(state.history) <= 20


def test_round_trip_preserves_values() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.access_mode = AccessMode.WRITABLE
    state.completed_steps.append("第一步")
    state.checkpoint_memory_ids.append("S_0001")

    restored = TaskState.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored.to_dict() == state.to_dict()
    assert restored.access_mode is AccessMode.WRITABLE


def test_store_save_and_load(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)

    store.save(state)
    loaded = store.load()

    assert loaded is not None
    assert loaded.task_id == "T_TEST"
    assert loaded.status is TaskStatus.IN_PROGRESS


def test_store_missing_returns_none(tmp_path) -> None:
    assert StateStore(tmp_path).load() is None


def test_corrupt_state_raises(tmp_path) -> None:
    store = StateStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(StateCorruptError, match="状态文件损坏"):
        store.load()


def test_clear_removes_state(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = make_state()
    store.save(state)

    store.clear()

    assert store.load() is None

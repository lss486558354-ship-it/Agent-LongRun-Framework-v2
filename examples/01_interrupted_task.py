"""示例 1：长任务被中断 → 断点写回记忆 → 新进程恢复。

运行：

    python examples/01_interrupted_task.py          # 本地直连记忆核心（快）
    python examples/01_interrupted_task.py --mcp    # 真正走 MCP stdio（慢，但真实）

数据写到系统临时目录，不会污染你的正式记忆库。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workflow_manager import (  # noqa: E402
    LocalMemoryBackend,
    LongRunAgent,
    MCPMemoryBackend,
    TaskStatus,
)


def enable_utf8_console() -> None:
    """中文 Windows 控制台默认 GBK，打印 ✅ / 中文标点会直接崩。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def build_memory(project_root: Path):
    if "--mcp" in sys.argv:
        print("记忆后端：MCPMemoryBackend（stdio 子进程）")
        return MCPMemoryBackend(project_path=project_root)
    print("记忆后端：LocalMemoryBackend（进程内直连）")
    return LocalMemoryBackend(project_path=project_root)


def main() -> int:
    enable_utf8_console()
    home = Path(tempfile.mkdtemp(prefix="longrun-demo-"))
    memory = build_memory(home)

    # 先放一条"以前做过"的记忆，证明 start_task 真的会去检索。
    memory.save(
        type="decision",
        title="报价适配器优先做只读探测",
        content="历史结论：先不登录、不点按钮，只读首页能不能拿到价格；"
        "需要登录的平台单独标记，不要浪费轮次。",
        keywords="报价,适配器,探测",
    )

    print("\n=== 1. 启动任务（先查记忆，默认只读）===")
    agent = LongRunAgent(memory, root=home)
    start = agent.start_task("实现报价平台适配器", keywords="报价,适配器")

    print(start.summary)
    for row in start.memories:
        print(f"  命中 {row['id']}  {row['title']}")
    for row in start.context:
        print(f"  已加载原文 {row['id']}：{row['content'][:40]}…")

    # 试图在只读模式下写代码 —— 必须被拦
    try:
        agent.require_writable("修改 adapters/xometry.py")
    except Exception as exc:
        print(f"\n  门禁生效：{type(exc).__name__}: {exc}")

    print("\n=== 2. 确认方案，解锁写入 ===")
    agent.enable_writes("已读完历史方案，进入实施")
    agent.require_writable("修改 adapters/xometry.py")
    print("  已进入可写模式")

    print("\n=== 3. token 预警，写断点 ===")
    checkpoint = agent.checkpoint(
        reason="token 预警",
        summary="完成 Xometry 只读探测器",
        next_step="接入 Protolabs，复用同一接口",
        decisions=["先只读探测，不做登录自动化"],
    )
    print(checkpoint.text)

    print("\n=== 4. 模拟新进程：从本地状态恢复 ===")
    resumed_agent = LongRunAgent(memory, root=home)
    bundle = resumed_agent.resume()
    print(bundle.summary)
    print(f"  状态：{bundle.state.status.value}")
    print(f"  门禁：{bundle.state.access_mode.value}")
    print(f"  断点记忆：{bundle.state.checkpoint_id}")
    if bundle.checkpoint:
        print(f"  断点原文前 60 字：{bundle.checkpoint['content'][:60]}…")

    print("\n=== 5. 完成 ===")
    done = resumed_agent.finish("两个平台适配器均已接入并通过只读验证")
    print(f"  终态：{done.state.status.value}")
    print(f"  归档记忆：{done.memory_id}")

    print(f"\n数据目录：{home}")
    assert done.state.status is TaskStatus.COMPLETED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

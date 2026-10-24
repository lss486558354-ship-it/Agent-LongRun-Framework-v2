"""示例 2：阶段完成归档 + 决策沉淀 + 状态文件丢失后的记忆重建。

运行：

    python examples/02_phase_archive.py
    python examples/02_phase_archive.py --mcp

这里演示的是「换机器」场景：
`.longrun/state.json` 不存在了，但记忆库还在 ——
`recover_from_memory()` 会解析断点里的机器可读标记，重建状态机。
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
)


def enable_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> int:
    enable_utf8_console()
    home = Path(tempfile.mkdtemp(prefix="longrun-demo2-"))
    memory = (
        MCPMemoryBackend(project_path=home)
        if "--mcp" in sys.argv
        else LocalMemoryBackend(project_path=home)
    )

    agent = LongRunAgent(memory, root=home)
    started = agent.start_task("重构报价流水线", keywords="报价,流水线")
    agent.enable_writes("方案已确认")

    print("=== 阶段 1 完成 ===")
    first = agent.complete_phase(
        "阶段1：抽象接口",
        "抽出 PlatformAdapter 协议，三个平台共用",
        decisions=["统一返回 QuoteResult，不用裸 dict"],
        next_step="阶段2：接入第一个平台",
    )
    print(f"  记忆：{first.memory_id}  {first.text.splitlines()[0]}")
    print(f"  已完成步骤：{first.state.completed_steps}")

    print("\n=== 单独沉淀决策 ===")
    decision = agent.save_decision(
        "适配器统一异步接口",
        "同步接口在批量并发时会被单个平台阻塞；统一 async 后可以用 asyncio.gather 并发探测。",
        keywords="async,接口,并发",
    )
    print(f"  决策记忆：{decision['id']}  {decision['title']}")

    print("\n=== 阶段 2 完成并断点 ===")
    second = agent.complete_phase(
        "阶段2：接入 Xometry",
        "只读探测跑通，首页无法拿到匿名报价",
        decisions=["Xometry 标记为需登录，不做自动化绕过"],
        next_step="阶段3：接入 Protolabs",
    )
    print(second.text)

    print("\n=== 模拟换机器：状态文件不存在 ===")
    lost_state_dir = home / "another-machine"
    fresh = LongRunAgent(memory, root=lost_state_dir)
    bundle = fresh.recover_from_memory(started.state.task_id)

    print(bundle.summary)
    print(f"  重建出的阶段：{bundle.state.phase}")
    print(f"  已完成步骤：{bundle.state.completed_steps}")
    print(f"  关键决策：{bundle.state.decisions}")
    print(f"  关联记忆：{[r['id'] for r in bundle.related]}")

    assert bundle.state.task_id == started.state.task_id
    assert bundle.state.next_step == "阶段3：接入 Protolabs"
    print("\n✅ 从记忆重建成功")
    print(f"数据目录：{home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

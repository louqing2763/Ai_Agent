"""
tools/system_monitor.py — 系統狀態監控

使用 psutil 讀取 CPU、記憶體、磁碟、網路、程式資訊。
Railway 上讀到的是容器狀態，本機跑則是真實電腦狀態。
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def get_system_status(detail: str = "all") -> Optional[str]:
    return await asyncio.to_thread(_get_status_sync, detail)


def _get_status_sync(detail: str) -> str:
    try:
        import psutil
    except ImportError:
        return "psutil 未安裝，無法取得系統狀態。請在 requirements.txt 加入 psutil。"

    parts = []

    try:
        if detail in ("all", "cpu"):
            cpu   = psutil.cpu_percent(interval=1)
            cores = psutil.cpu_count()
            freq  = psutil.cpu_freq()
            freq_str = f"，{freq.current:.0f} MHz" if freq else ""
            parts.append(f"CPU：{cpu}%（{cores} 核{freq_str}）")

        if detail in ("all", "memory"):
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()
            parts.append(
                f"記憶體：{_mb(mem.used)} / {_mb(mem.total)} MB"
                f"（使用 {mem.percent}%）"
                f"，Swap：{_mb(swap.used)} / {_mb(swap.total)} MB"
            )

        if detail in ("all", "disk"):
            disk_lines = []
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disk_lines.append(
                        f"  {part.mountpoint}：{_gb(usage.used)} / {_gb(usage.total)} GB"
                        f"（{usage.percent}%）"
                    )
                except Exception:
                    continue
            if disk_lines:
                parts.append("磁碟：\n" + "\n".join(disk_lines))

        if detail in ("all", "network"):
            net = psutil.net_io_counters()
            parts.append(
                f"網路：↑ {_mb(net.bytes_sent)} MB 已送出"
                f"，↓ {_mb(net.bytes_recv)} MB 已接收"
            )

        if detail in ("all", "processes"):
            procs = []
            for p in sorted(
                psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
                key=lambda x: x.info.get("cpu_percent") or 0,
                reverse=True,
            )[:8]:
                try:
                    procs.append(
                        f"  [{p.info['pid']}] {p.info['name']}"
                        f" CPU:{p.info['cpu_percent']:.1f}%"
                        f" MEM:{p.info['memory_percent']:.1f}%"
                    )
                except Exception:
                    continue
            if procs:
                parts.append("CPU 佔用前 8 程式：\n" + "\n".join(procs))

    except Exception as e:
        logger.error(f"[system_monitor] 讀取失敗: {e}")
        return f"系統狀態讀取失敗：{e}"

    return "\n".join(parts) if parts else "無法取得系統資訊。"


def _mb(b: int) -> str:
    return f"{b / 1024 / 1024:.1f}"

def _gb(b: int) -> str:
    return f"{b / 1024 / 1024 / 1024:.1f}"

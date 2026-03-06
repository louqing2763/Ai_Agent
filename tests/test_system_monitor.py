"""
tests/test_system_monitor.py — Unit tests for tools/system_monitor.py

Coverage areas:
- _mb(): bytes → MB string formatting
- _gb(): bytes → GB string formatting
- _get_status_sync(): with mocked psutil, for each detail level
- get_system_status(): async wrapper delegates to sync function
- psutil ImportError handling
- Exception handling inside status collection
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.system_monitor import _mb, _gb, _get_status_sync, get_system_status


# ------------------------------------------------------------------
# _mb and _gb helpers
# ------------------------------------------------------------------
class TestMbGbFormatters:
    def test_mb_exact_megabyte(self):
        assert _mb(1024 * 1024) == "1.0"

    def test_mb_zero(self):
        assert _mb(0) == "0.0"

    def test_mb_large_value(self):
        result = _mb(8 * 1024 * 1024 * 1024)  # 8 GB in bytes
        assert result == "8192.0"

    def test_gb_exact_gigabyte(self):
        assert _gb(1024 * 1024 * 1024) == "1.0"

    def test_gb_zero(self):
        assert _gb(0) == "0.0"

    def test_gb_fractional(self):
        half_gb = 512 * 1024 * 1024
        assert _gb(half_gb) == "0.5"

    def test_mb_one_decimal_place(self):
        # 1.5 MB
        result = _mb(int(1.5 * 1024 * 1024))
        assert result == "1.5"


# ------------------------------------------------------------------
# Helpers for building mock psutil objects
# ------------------------------------------------------------------
def make_cpu_freq(current=2400.0):
    freq = MagicMock()
    freq.current = current
    return freq


def make_memory(used, total, percent):
    mem = MagicMock()
    mem.used = used
    mem.total = total
    mem.percent = percent
    return mem


def make_disk_partition(mountpoint="/"):
    part = MagicMock()
    part.mountpoint = mountpoint
    return part


def make_disk_usage(used, total, percent):
    usage = MagicMock()
    usage.used = used
    usage.total = total
    usage.percent = percent
    return usage


def make_net_io(sent, recv):
    net = MagicMock()
    net.bytes_sent = sent
    net.bytes_recv = recv
    return net


def make_process(pid, name, cpu_pct, mem_pct):
    p = MagicMock()
    p.info = {"pid": pid, "name": name, "cpu_percent": cpu_pct, "memory_percent": mem_pct}
    return p


# ------------------------------------------------------------------
# _get_status_sync — CPU detail
# ------------------------------------------------------------------
class TestGetStatusSyncCPU:
    def _mock_psutil(self, mock_module):
        mock_module.cpu_percent.return_value = 42.5
        mock_module.cpu_count.return_value = 8
        mock_module.cpu_freq.return_value = make_cpu_freq(3200.0)

    def test_cpu_detail_contains_cpu_info(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            self._mock_psutil(psutil)
            result = _get_status_sync("cpu")
        assert "CPU" in result
        assert "42.5" in result
        assert "8" in result

    def test_cpu_detail_excludes_memory(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            self._mock_psutil(psutil)
            result = _get_status_sync("cpu")
        assert "記憶體" not in result

    def test_cpu_freq_none_handled_gracefully(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            psutil.cpu_percent.return_value = 10.0
            psutil.cpu_count.return_value = 4
            psutil.cpu_freq.return_value = None
            result = _get_status_sync("cpu")
        assert "CPU" in result
        assert "MHz" not in result


# ------------------------------------------------------------------
# _get_status_sync — memory detail
# ------------------------------------------------------------------
class TestGetStatusSyncMemory:
    def test_memory_detail_contains_mem_info(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            psutil.virtual_memory.return_value = make_memory(
                used=4 * 1024**3, total=16 * 1024**3, percent=25.0
            )
            psutil.swap_memory.return_value = make_memory(
                used=0, total=2 * 1024**3, percent=0.0
            )
            result = _get_status_sync("memory")
        assert "記憶體" in result
        assert "25.0%" in result

    def test_memory_detail_excludes_cpu(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            psutil.virtual_memory.return_value = make_memory(1024**3, 8 * 1024**3, 12.5)
            psutil.swap_memory.return_value = make_memory(0, 1024**3, 0.0)
            result = _get_status_sync("memory")
        assert "CPU" not in result


# ------------------------------------------------------------------
# _get_status_sync — disk detail
# ------------------------------------------------------------------
class TestGetStatusSyncDisk:
    def test_disk_detail_contains_disk_info(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            psutil.disk_partitions.return_value = [make_disk_partition("/")]
            psutil.disk_usage.return_value = make_disk_usage(
                used=50 * 1024**3, total=200 * 1024**3, percent=25.0
            )
            result = _get_status_sync("disk")
        assert "磁碟" in result
        assert "/" in result
        assert "25.0%" in result

    def test_disk_detail_skips_unreadable_partitions(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            psutil.disk_partitions.return_value = [make_disk_partition("/mnt/bad")]
            psutil.disk_usage.side_effect = PermissionError("no access")
            result = _get_status_sync("disk")
        # Should not crash; result might be empty disk section or fallback
        assert isinstance(result, str)


# ------------------------------------------------------------------
# _get_status_sync — network detail
# ------------------------------------------------------------------
class TestGetStatusSyncNetwork:
    def test_network_detail_contains_net_info(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            psutil.net_io_counters.return_value = make_net_io(
                sent=100 * 1024**2, recv=500 * 1024**2
            )
            result = _get_status_sync("network")
        assert "網路" in result
        assert "↑" in result
        assert "↓" in result


# ------------------------------------------------------------------
# _get_status_sync — processes detail
# ------------------------------------------------------------------
class TestGetStatusSyncProcesses:
    def test_processes_detail_contains_process_list(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            procs = [
                make_process(1, "python", 35.0, 5.0),
                make_process(2, "nginx", 10.0, 1.0),
            ]
            psutil.process_iter.return_value = procs
            result = _get_status_sync("processes")
        assert "程式" in result or "CPU" in result

    def test_processes_limited_to_top_8(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            procs = [make_process(i, f"proc{i}", float(100 - i), 1.0) for i in range(20)]
            psutil.process_iter.return_value = procs
            result = _get_status_sync("processes")
        # Count occurrences of "proc" prefix — should have at most 8
        count = result.count("proc")
        assert count <= 8


# ------------------------------------------------------------------
# _get_status_sync — all detail
# ------------------------------------------------------------------
class TestGetStatusSyncAll:
    def test_all_detail_includes_all_sections(self):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import psutil
            psutil.cpu_percent.return_value = 50.0
            psutil.cpu_count.return_value = 4
            psutil.cpu_freq.return_value = make_cpu_freq()
            psutil.virtual_memory.return_value = make_memory(2 * 1024**3, 8 * 1024**3, 25.0)
            psutil.swap_memory.return_value = make_memory(0, 1024**3, 0.0)
            psutil.disk_partitions.return_value = [make_disk_partition("/")]
            psutil.disk_usage.return_value = make_disk_usage(50 * 1024**3, 200 * 1024**3, 25.0)
            psutil.net_io_counters.return_value = make_net_io(10 * 1024**2, 50 * 1024**2)
            psutil.process_iter.return_value = [make_process(1, "main", 20.0, 3.0)]
            result = _get_status_sync("all")

        assert "CPU" in result
        assert "記憶體" in result
        assert "磁碟" in result
        assert "網路" in result


# ------------------------------------------------------------------
# _get_status_sync — psutil not installed
# ------------------------------------------------------------------
class TestGetStatusSyncImportError:
    def test_missing_psutil_returns_error_message(self):
        with patch.dict("sys.modules", {"psutil": None}):
            result = _get_status_sync("all")
        assert "psutil" in result or "未安裝" in result


# ------------------------------------------------------------------
# get_system_status — async wrapper
# ------------------------------------------------------------------
class TestGetSystemStatus:
    @pytest.mark.asyncio
    async def test_async_wrapper_returns_string(self):
        with patch("tools.system_monitor._get_status_sync", return_value="CPU: 10%"):
            result = await get_system_status("cpu")
        assert result == "CPU: 10%"

    @pytest.mark.asyncio
    async def test_default_detail_is_all(self):
        with patch("tools.system_monitor._get_status_sync", return_value="all info") as mock_sync:
            await get_system_status()
        mock_sync.assert_called_once_with("all")

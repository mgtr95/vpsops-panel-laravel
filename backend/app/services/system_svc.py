from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

_cpu_lock = threading.Lock()
_prev_cpu: dict[str, int] | None = None
_prev_cpu_at: float = 0.0

_CPU_FIELDS = (
    "user",
    "nice",
    "system",
    "idle",
    "iowait",
    "irq",
    "softirq",
    "steal",
    "guest",
    "guest_nice",
)


def _read_text(name: str) -> str:
    errors: list[OSError] = []
    for root in (Path("/host/proc"), Path("/proc")):
        try:
            return (root / name).read_text()
        except OSError as e:
            errors.append(e)
    raise errors[-1]


def _cpu_times() -> dict[str, int]:
    line = _read_text("stat").splitlines()[0]
    nums = [int(x) for x in line.split()[1:]]
    data = {name: nums[i] if i < len(nums) else 0 for i, name in enumerate(_CPU_FIELDS)}
    data["total"] = sum(nums[:8])
    return data


def _cpu_percent(sample: float = 0.12) -> dict[str, float | None]:
    global _prev_cpu, _prev_cpu_at
    empty = {k: None for k in ("percent", "user", "nice", "system", "iowait", "steal", "idle")}
    try:
        now = _cpu_times()
        now_at = time.monotonic()
    except OSError:
        return empty

    with _cpu_lock:
        prev, prev_at = _prev_cpu, _prev_cpu_at

    if prev is None or now_at - prev_at > 120:
        prev, prev_at = now, now_at
        time.sleep(sample)
        try:
            now = _cpu_times()
            now_at = time.monotonic()
        except OSError:
            return empty

    total_delta = now["total"] - prev["total"]
    with _cpu_lock:
        _prev_cpu, _prev_cpu_at = now, now_at
    if total_delta <= 0:
        return empty

    def pct(key: str) -> float:
        return round((now[key] - prev[key]) / total_delta * 100, 1)

    idle = now["idle"] - prev["idle"]
    used = total_delta - idle
    return {
        "percent": round(used / total_delta * 100, 1),
        "user": pct("user"),
        "nice": pct("nice"),
        "system": pct("system"),
        "iowait": pct("iowait"),
        "steal": pct("steal"),
        "idle": round(idle / total_delta * 100, 1),
    }


def _cpu_count() -> int:
    try:
        count = 0
        for line in _read_text("cpuinfo").splitlines():
            if line.lower().startswith("processor"):
                count += 1
        if count:
            return count
    except OSError:
        pass
    return os.cpu_count() or 1


def _cpu_model() -> str | None:
    try:
        hardware = None
        for line in _read_text("cpuinfo").splitlines():
            lower = line.lower()
            if lower.startswith("model name"):
                return line.split(":", 1)[1].strip()
            if lower.startswith("hardware") and hardware is None:
                hardware = line.split(":", 1)[1].strip()
        return hardware
    except OSError:
        return None


def _loadavg() -> list[float]:
    try:
        parts = _read_text("loadavg").split()
        return [round(float(parts[i]), 2) for i in range(3)]
    except (OSError, ValueError, IndexError):
        try:
            return [round(x, 2) for x in os.getloadavg()]
        except OSError:
            return []


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in _read_text("meminfo").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            parts = rest.split()
            if not parts:
                continue
            kb = int(parts[0])
            out[key] = kb * 1024
    except OSError:
        pass
    return out


def _uptime_seconds() -> float | None:
    try:
        return float(_read_text("uptime").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _docker_host() -> dict:
    try:
        from app.services import docker_svc

        info = docker_svc.get_client().info()
        return {
            "hostname": info.get("Name"),
            "os": info.get("OperatingSystem"),
            "kernel": info.get("KernelVersion"),
            "arch": info.get("Architecture"),
            "cpus": info.get("NCPU"),
            "mem_total": info.get("MemTotal"),
        }
    except Exception:
        return {}


def snapshot() -> dict:
    cpu_pct = _cpu_percent()
    cores = _cpu_count()
    load = _loadavg()
    mem = _meminfo()
    docker_host = _docker_host()

    total = mem.get("MemTotal") or docker_host.get("mem_total") or 0
    available = mem.get("MemAvailable")
    if available is None:
        available = (
            mem.get("MemFree", 0)
            + mem.get("Buffers", 0)
            + mem.get("Cached", 0)
            + mem.get("SReclaimable", 0)
        )
    used = max(total - available, 0) if total else 0
    swap_total = mem.get("SwapTotal") or 0
    swap_free = mem.get("SwapFree") or 0
    swap_used = max(swap_total - swap_free, 0)

    cores = int(docker_host.get("cpus") or cores or 1)
    load1 = load[0] if load else None
    load_percent = round((load1 / cores) * 100, 1) if load1 is not None and cores else None

    return {
        "cpu": {
            **cpu_pct,
            "count": cores,
            "load": load,
            "load_percent": load_percent,
            "model": _cpu_model(),
        },
        "memory": {
            "total": total or None,
            "used": used if total else None,
            "available": available if total else None,
            "free": mem.get("MemFree"),
            "cached": mem.get("Cached"),
            "buffers": mem.get("Buffers"),
            "percent": round(used / total * 100, 1) if total else None,
            "swap_total": swap_total or None,
            "swap_used": swap_used if swap_total else None,
            "swap_percent": round(swap_used / swap_total * 100, 1) if swap_total else None,
        },
        "host": {
            "hostname": docker_host.get("hostname") or socket.gethostname(),
            "os": docker_host.get("os"),
            "kernel": docker_host.get("kernel"),
            "arch": docker_host.get("arch"),
            "uptime_seconds": _uptime_seconds(),
        },
    }

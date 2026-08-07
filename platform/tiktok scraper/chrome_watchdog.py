"""Watchdog Chrome orphelins — filet de securite (harden-mvp 1.1).

Thread daemon leger: tue les process chrome/chromium dont le parent est mort
(ou PPID=1) et dont l'age depasse TIKTOK_CHROME_ORPHAN_MAX_MINUTES (defaut 10).
Aucune dependance externe (pas de psutil).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="chrome_watchdog")

_STARTED = False
_LOCK = threading.Lock()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def enabled() -> bool:
    return _env_bool("TIKTOK_CHROME_WATCHDOG_ENABLED", True)


def orphan_max_minutes() -> float:
    return max(2.0, _env_float("TIKTOK_CHROME_ORPHAN_MAX_MINUTES", 10.0))


def scan_interval_s() -> float:
    return max(30.0, _env_float("TIKTOK_CHROME_WATCHDOG_INTERVAL_S", 60.0))


def _is_chrome_name(name: str) -> bool:
    n = (name or "").lower()
    return any(
        tok in n
        for tok in (
            "chrome",
            "chromium",
            "msedge",
            "playwright",
        )
    ) and "chrome_watchdog" not in n


def _linux_candidates() -> list[tuple[int, int, float, str]]:
    """Retourne (pid, ppid, age_s, name) via /proc."""
    out: list[tuple[int, int, float, str]] = []
    proc = "/proc"
    if not os.path.isdir(proc):
        return out
    now = time.time()
    try:
        entries = os.listdir(proc)
    except OSError:
        return out
    for ent in entries:
        if not ent.isdigit():
            continue
        pid = int(ent)
        base = f"{proc}/{pid}"
        try:
            with open(f"{base}/stat", encoding="utf-8", errors="ignore") as fh:
                stat = fh.read().split()
            # pid (comm) state ppid ...
            comm = stat[1].strip("()") if len(stat) > 1 else ""
            ppid = int(stat[3]) if len(stat) > 3 else 0
            with open(f"{base}/comm", encoding="utf-8", errors="ignore") as fh:
                name = (fh.read() or comm).strip()
            start_ticks = float(stat[21]) if len(stat) > 21 else 0.0
            # Approximation age via etime de /proc/uptime - starttime/CLK_TCK
            clk = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            with open("/proc/uptime", encoding="utf-8") as fh:
                uptime = float(fh.read().split()[0])
            age_s = max(0.0, uptime - (start_ticks / float(clk or 100)))
        except Exception:
            continue
        if _is_chrome_name(name) or _is_chrome_name(comm):
            out.append((pid, ppid, age_s, name or comm))
    return out


def _win_candidates() -> list[tuple[int, int, float, str]]:
    """Retourne (pid, ppid, age_s≈0 si inconnu, name) via WMIC/PowerShell."""
    out: list[tuple[int, int, float, str]] = []
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -match 'chrome|chromium|msedge|playwright' } | "
                    "Select-Object ProcessId,ParentProcessId,Name,CreationDate | "
                    "ConvertTo-Csv -NoTypeInformation"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return out
    lines = [ln.strip() for ln in (completed.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return out
    now = time.time()
    for line in lines[1:]:
        # CSV: "ProcessId","ParentProcessId","Name","CreationDate"
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            name = parts[2]
        except ValueError:
            continue
        age_s = 0.0
        if len(parts) >= 4 and parts[3]:
            # Fallback: si age inconnu, watchdog ne tue que PPID mort / 0
            age_s = 0.0
        if _is_chrome_name(name):
            out.append((pid, ppid, age_s, name))
    _ = now
    return out


def _parent_alive(ppid: int) -> bool:
    if ppid <= 1:
        return False
    if sys.platform == "win32":
        try:
            # OpenProcess fails / tasklist
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {ppid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            text = (completed.stdout or "").lower()
            return str(ppid) in text and "no tasks" not in text
        except Exception:
            return True
    return os.path.exists(f"/proc/{ppid}")


def _kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        LOGGER.debug("watchdog kill pid=%s failed", pid, exc_info=True)


def reap_orphans() -> int:
    """Tue les Chrome orphelins eligibles. Retourne le nombre tues."""
    if not enabled():
        return 0
    max_age = orphan_max_minutes() * 60.0
    try:
        rows = _win_candidates() if sys.platform == "win32" else _linux_candidates()
    except Exception:
        LOGGER.debug("watchdog scan failed", exc_info=True)
        return 0

    killed = 0
    my_pid = os.getpid()
    for pid, ppid, age_s, name in rows:
        if pid == my_pid:
            continue
        orphan = not _parent_alive(ppid)
        # Linux: age connu. Windows: si orphan et chrome → kill (age souvent 0).
        old_enough = age_s >= max_age if age_s > 0 else orphan
        if orphan and old_enough:
            LOGGER.warning(
                "[CHROME_WATCHDOG] killing orphan pid=%s ppid=%s age=%.0fs name=%s",
                pid,
                ppid,
                age_s,
                name,
            )
            _kill_pid(pid)
            killed += 1
    return killed


def _loop() -> None:
    while True:
        try:
            reap_orphans()
        except Exception:
            LOGGER.debug("watchdog loop error", exc_info=True)
        time.sleep(scan_interval_s())


def start_chrome_watchdog() -> None:
    """Demarre le thread daemon une seule fois par process worker."""
    global _STARTED
    if not enabled():
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    t = threading.Thread(target=_loop, name="chrome-watchdog", daemon=True)
    t.start()
    LOGGER.info(
        "[CHROME_WATCHDOG] started (max_orphan_minutes=%.1f interval=%.0fs)",
        orphan_max_minutes(),
        scan_interval_s(),
    )


def kill_process_tree(pid: int) -> None:
    """API partagee: kill PID + enfants (Windows taskkill / Linux killpg)."""
    if pid <= 0:
        return
    LOGGER.warning("[PERF] Killing scrape process tree pid=%s", pid)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except Exception:
        LOGGER.debug("kill_process_tree failed pid=%s", pid, exc_info=True)

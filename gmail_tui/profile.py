"""Optional CPU and memory log.

``gmail-tui --profile`` appends to ``<data>/profile.log``. Nothing is recorded
unless that flag is set. The log has three kinds of lines:

* ``mem`` / ``cpu`` — RSS and, after the first sample, each thread's share of
  one core. ``proc`` is their sum, which is the number ``top`` shows.
* ``msg`` — wall time for one cached message: socket read, JSON, parse, and
  the index write. ``read`` is mostly waiting on Gmail.
* ``--- thread ---`` — the functions that used time on that thread. One
  profiler covers every account thread; Python only allows a single one.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_recorder: Recorder | None = None
_Call = tuple[str, int, str]


class _Frames:
    """Call times for one thread. Exclusive time is the time spent in that function."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: dict[_Call, list[float]] = {}
        self.stack: list[list] = []
        self.active = True
        self.dumped_at = 0.0

    def enter(self, code: object, *, count: bool) -> None:
        now = time.perf_counter()
        if self.stack:
            top_key, started = self.stack[-1]
            elapsed = now - started
            if elapsed > 0:
                self.calls[top_key][1] += elapsed
            self.stack[-1][1] = now
        key = _call_key(code)
        row = self.calls.get(key)
        if row is None:
            self.calls[key] = [1.0 if count else 0.0, 0.0]
        elif count:
            row[0] += 1
        self.stack.append([key, now])

    def leave(self, code: object) -> None:
        now = time.perf_counter()
        key = _call_key(code)
        while self.stack:
            top_key, started = self.stack.pop()
            elapsed = now - started
            if elapsed > 0:
                row = self.calls.get(top_key)
                if row is not None:
                    row[1] += elapsed
            if top_key == key:
                break
            now = time.perf_counter()
        if self.stack:
            self.stack[-1][1] = time.perf_counter()


class Recorder:
    def __init__(self, path: Path, interval: float | None) -> None:
        self.path = path
        self.interval = interval
        self.messages = 0
        self.raw_bytes = 0
        self.totals = {"read": 0.0, "json": 0.0, "parse": 0.0, "save": 0.0}
        self._file = None
        self._lock = threading.Lock()
        self._local = threading.local()
        self._frames: list[_Frames] = []
        self._tool: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticks: dict[int, int] = {}
        self._sampled_at = 0.0
        self._clk = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self.line(f"profile start pid={os.getpid()} log={self.path}")
        self.sample()
        if self.interval is not None:
            self._thread = threading.Thread(target=self._loop, name="gmail-tui-profile", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self.detach()
        self._disable_tool()
        self._dump()
        self.line("profile stop")
        if self._file is not None:
            self._file.close()
            self._file = None

    def line(self, text: str) -> None:
        with self._lock:
            self._write(text)

    def attach(self) -> None:
        """Start measuring this thread. Safe to call from every account thread."""
        try:
            self._enable_tool()
        except Exception as exc:
            self.line(f"profile attach failed: {exc}")
            return
        current = getattr(self._local, "frames", None)
        if current is not None and current.active:
            return
        frames = _Frames(threading.current_thread().name)
        self._local.frames = frames
        with self._lock:
            self._frames.append(frames)

    def detach(self) -> None:
        frames = getattr(self._local, "frames", None)
        if frames is None:
            return
        frames.active = False
        frames.stack.clear()
        self._local.frames = None

    def begin_message(self) -> None:
        self._local.bucket = {"read": 0.0, "json": 0.0, "parse": 0.0, "save": 0.0, "wire": 0}

    def discard_message(self) -> None:
        self._local.bucket = None

    def add(self, part: str, seconds: float, wire: int = 0) -> None:
        bucket = getattr(self._local, "bucket", None)
        if bucket is None:
            return
        bucket[part] += seconds
        bucket["wire"] += wire

    def finish_message(self, gmail_id: str, raw_bytes: int) -> None:
        bucket = getattr(self._local, "bucket", None)
        self._local.bucket = None
        if bucket is None:
            return
        with self._lock:
            self.messages += 1
            self.raw_bytes += raw_bytes
            for key in self.totals:
                self.totals[key] += bucket[key]
            self._write(
                f"msg {gmail_id} raw={raw_bytes} wire={bucket['wire']}"
                f" read={_ms(bucket['read'])} json={_ms(bucket['json'])}"
                f" parse={_ms(bucket['parse'])} save={_ms(bucket['save'])}"
            )

    def checkpoint(self) -> None:
        """Write this thread's functions. Called on the thread that is being measured."""
        frames = getattr(self._local, "frames", None)
        if frames is None or not frames.active:
            return
        now = time.monotonic()
        if not frames.dumped_at:
            frames.dumped_at = now
            return
        if now - frames.dumped_at < 60:
            return
        frames.dumped_at = now
        self._write_frames(frames)

    def sample(self) -> None:
        rss_kb, peak_kb = _memory()
        now = time.monotonic()
        elapsed = now - self._sampled_at if self._sampled_at else 0.0
        proc = 0.0
        parts: list[str] = []
        ticks: dict[int, int] = {}
        for tid, name, total in _thread_cpu():
            previous = self._ticks.get(tid)
            ticks[tid] = total
            if previous is None or elapsed <= 0 or self._clk <= 0:
                continue
            share = (total - previous) / self._clk / elapsed * 100
            proc += share
            parts.append(f"{name}={share:.1f}%")
        self._ticks = ticks
        self._sampled_at = now
        mem = f"mem rss={rss_kb / 1024:.0f}MB peak={peak_kb / 1024:.0f}MB"
        if parts:
            mem = f"cpu proc={proc:.1f}% {mem} " + " ".join(parts)
        with self._lock:
            self._write(mem + self._totals_locked())

    def _loop(self) -> None:
        while not self._stop.wait(self.interval or 5.0):
            try:
                self.sample()
            except Exception as exc:
                self.line(f"profile error {exc}")

    def _enable_tool(self) -> None:
        """Register the one process-wide profiler. Further threads only join it."""
        if self._tool is not None:
            return
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is None:
            raise RuntimeError("this Python cannot profile more than one thread")
        tool = None
        for candidate in (3, 4):
            try:
                monitoring.use_tool_id(candidate, "gmail-tui-profile")
            except ValueError:
                continue
            tool = candidate
            break
        if tool is None:
            raise RuntimeError("profiling is already in use")
        self._tool = tool
        events = monitoring.events
        try:
            monitoring.register_callback(tool, events.PY_START, _on_start)
            monitoring.register_callback(tool, events.PY_RETURN, _on_return)
            monitoring.register_callback(tool, events.PY_UNWIND, _on_return)
            monitoring.register_callback(tool, events.PY_YIELD, _on_return)
            monitoring.register_callback(tool, events.PY_RESUME, _on_resume)
            monitoring.set_events(
                tool,
                events.PY_START
                | events.PY_RETURN
                | events.PY_UNWIND
                | events.PY_YIELD
                | events.PY_RESUME,
            )
        except Exception:
            self._disable_tool()
            raise

    def _disable_tool(self) -> None:
        tool = self._tool
        if tool is None:
            return
        self._tool = None
        monitoring = sys.monitoring
        try:
            monitoring.set_events(tool, monitoring.events.NO_EVENTS)
            monitoring.free_tool_id(tool)
        except ValueError:
            return

    def _dump(self) -> None:
        with self._lock:
            frames = list(self._frames)
        for item in frames:
            self._write_frames(item)

    def _write_frames(self, frames: _Frames) -> None:
        ranked = sorted(frames.calls.items(), key=lambda item: item[1][1], reverse=True)[:12]
        lines = [f"--- {frames.name} ---", f"{'ncalls':>8} {'tottime':>10} function"]
        for (filename, lineno, name), (ncalls, tottime) in ranked:
            short = filename.rsplit("/", 1)[-1]
            lines.append(f"{int(ncalls):8d} {tottime:10.4f} {name} ({short}:{lineno})")
        self.line("\n".join(lines))

    def _totals_locked(self) -> str:
        if self.messages == 0:
            return ""
        return (
            f" n={self.messages} read={_ms(self.totals['read'])}"
            f" json={_ms(self.totals['json'])} parse={_ms(self.totals['parse'])}"
            f" save={_ms(self.totals['save'])} raw={self.raw_bytes}"
        )

    def _write(self, text: str) -> None:
        if self._file is None:
            return
        self._file.write(text.rstrip() + "\n")
        self._file.flush()


def recording() -> bool:
    return _recorder is not None


def start(path: Path | str, *, interval: float | None = 5.0) -> None:
    global _recorder
    if _recorder is not None:
        return
    recorder = Recorder(Path(path), interval)
    recorder.open()
    _recorder = recorder


def stop() -> None:
    global _recorder
    recorder = _recorder
    _recorder = None
    if recorder is not None:
        recorder.close()


def attach() -> None:
    recorder = _recorder
    if recorder is not None:
        recorder.attach()


def detach() -> None:
    recorder = _recorder
    if recorder is not None:
        recorder.detach()


def begin_message() -> None:
    recorder = _recorder
    if recorder is not None:
        recorder.begin_message()


def discard_message() -> None:
    recorder = _recorder
    if recorder is not None:
        recorder.discard_message()


def add(part: str, seconds: float, wire: int = 0) -> None:
    recorder = _recorder
    if recorder is not None:
        recorder.add(part, seconds, wire)


def finish_message(gmail_id: str, raw_bytes: int) -> None:
    recorder = _recorder
    if recorder is not None:
        recorder.finish_message(gmail_id, raw_bytes)


def checkpoint() -> None:
    recorder = _recorder
    if recorder is not None:
        recorder.checkpoint()


def sample() -> None:
    recorder = _recorder
    if recorder is not None:
        recorder.sample()


def _call_key(code: object) -> _Call:
    filename = getattr(code, "co_filename", "")
    lineno = getattr(code, "co_firstlineno", 0)
    name = getattr(code, "co_name", "?")
    return filename, lineno, name


def _frames() -> _Frames | None:
    recorder = _recorder
    if recorder is None:
        return None
    frames = getattr(recorder._local, "frames", None)
    if frames is None or not frames.active:
        return None
    return frames


def _on_start(code: object, _offset: int) -> None:
    frames = _frames()
    if frames is not None:
        frames.enter(code, count=True)


def _on_resume(code: object, _offset: int) -> None:
    frames = _frames()
    if frames is not None:
        frames.enter(code, count=False)


def _on_return(code: object, _offset: int, _value: object = None) -> None:
    frames = _frames()
    if frames is not None:
        frames.leave(code)


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f}ms"


def _memory() -> tuple[int, int]:
    """Current and peak RSS in kilobytes."""
    rss = peak = 0
    try:
        text = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1])
        elif line.startswith("VmHWM:"):
            peak = int(line.split()[1])
    return rss, peak


def _thread_cpu() -> list[tuple[int, str, int]]:
    """Each thread's id, name, and cumulative user+system ticks."""
    root = Path("/proc/self/task")
    if not root.is_dir():
        return []
    rows: list[tuple[int, str, int]] = []
    for task in root.iterdir():
        try:
            stat = (task / "stat").read_text(encoding="utf-8")
            name = (task / "comm").read_text(encoding="utf-8").strip()
            tid = int(task.name)
        except (OSError, ValueError):
            continue
        end = stat.rfind(")")
        fields = stat[end + 2 :].split()
        if len(fields) < 13:
            continue
        rows.append((tid, name or str(tid), int(fields[11]) + int(fields[12])))
    return rows

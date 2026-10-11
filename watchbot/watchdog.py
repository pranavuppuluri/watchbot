"""Keep the bot running: starts the listener, the saver and (optionally) the uploader in one window,
restarts any that stop, and restarts the listener if it stops writing its heartbeat.

  python -m watchbot run --out <folder> [--seller "Ravi"] [--upload]

Everything is also logged to <folder>/logs/watchdog-<date>.log.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGGED_OUT = 3            # the listener's exit code when the phone unlinked it
HEARTBEAT_STALE = 5 * 60  # seconds without a listener heartbeat before it's restarted
STABLE = 10 * 60          # a child that ran this long gets its restart delay reset


class Child:
    def __init__(self, name: str, cmd: list[str], cwd: Path, env: dict):
        self.name, self.cmd, self.cwd, self.env = name, cmd, cwd, env
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.delay = 5
        self.next_start = 0.0
        self.disabled = False


class Watchdog:
    def __init__(self, out: Path, sellers: list[str], upload: bool):
        self.out = out.resolve()
        self.inbox = self.out / "_inbox"
        self.heartbeat = self.inbox / "heartbeat.txt"
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "WATCHBOT_INBOX": str(self.inbox), "WATCHBOT_OUTBOX": str(self.out / "_outbox")}
        node = "node.exe" if os.name == "nt" else "node"
        watch = [sys.executable, "-m", "watchbot", "watch", "--out", str(self.out)]
        for s in sellers:
            watch += ["--seller", s]
        self.children = [
            Child("listener", [node, "index.mjs"], ROOT / "listener", env),
            Child("saver", watch, ROOT, env),
        ]
        if upload:
            self.children.append(Child("uploader", [sys.executable, "-m", "watchbot", "upload", "--out", str(self.out)], ROOT, env))

    def log(self, name: str, line: str) -> None:
        stamp = datetime.now()
        text = f"{stamp:%H:%M:%S} [{name}] {line}"
        with self.lock:
            print(text, flush=True)
            with open(self.out / "logs" / f"watchdog-{stamp:%Y%m%d}.log", "a", encoding="utf-8") as f:
                f.write(text + "\n")

    def _pipe(self, c: Child) -> None:
        dump = False  # libsignal prints whole sessions (with keys) on "Closing session: SessionEntry {"
        for raw in c.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if dump or (c.name == "listener" and line.endswith("SessionEntry {")):
                dump = line != "}"
                continue
            if c.name == "listener" and set(line) <= set("▄▀█ "):
                with self.lock:  # QR code rows: print as-is so it stays scannable
                    print(line, flush=True)
            elif line:
                self.log(c.name, line)

    def start(self, c: Child) -> None:
        c.proc = subprocess.Popen(c.cmd, cwd=c.cwd, env=c.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        c.started = time.time()
        threading.Thread(target=self._pipe, args=(c,), daemon=True).start()
        self.log("watchdog", f"started {c.name} (pid {c.proc.pid})")

    def stop(self, c: Child) -> None:
        if c.proc and c.proc.poll() is None:
            c.proc.terminate()
            try:
                c.proc.wait(10)
            except subprocess.TimeoutExpired:
                c.proc.kill()

    def check(self, c: Child) -> None:
        if c.disabled:
            return
        if c.proc is None:
            if time.time() >= c.next_start:
                self.start(c)
            return
        code = c.proc.poll()
        if code is None and c.name == "listener" and self._stale(c):
            self.log("watchdog", "listener heartbeat is stale; restarting it")
            self.stop(c)
            code = c.proc.poll()
        if code is None:
            return
        if c.name == "listener" and code == LOGGED_OUT:
            self.log("watchdog", "listener was logged out from the phone. Delete listener/auth and restart to link again.")
            c.disabled = True
            return
        if time.time() - c.started >= STABLE:
            c.delay = 5
        self.log("watchdog", f"{c.name} stopped (exit {code}); restarting in {c.delay}s")
        c.proc, c.next_start = None, time.time() + c.delay
        c.delay = min(c.delay * 2, 300)

    def _stale(self, c: Child) -> bool:
        """Only once the listener has connected since this start (no heartbeat while showing a QR)."""
        try:
            beat = int(self.heartbeat.read_text()) / 1000
        except (OSError, ValueError):
            return False
        return beat >= c.started and time.time() - beat > HEARTBEAT_STALE

    def run(self) -> None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")  # a ₹ in a child's output must never crash the watchdog
        self.log("watchdog", f"saving to {self.out}; running {', '.join(c.name for c in self.children)}. Ctrl+C to stop.")
        try:
            while True:
                for c in self.children:
                    self.check(c)
                time.sleep(5)
        except KeyboardInterrupt:
            self.log("watchdog", "stopping...")
            for c in self.children:
                self.stop(c)

"""
launcher.py — Unified launcher for all Antigravity Assistant services.

CHANGES vs previous version:
- Replaced find_and_kill_extra_antigravity() with kill_all_antigravity().
  Old version only killed "extra" instances (kept first). New version kills
  ALL antigravity processes before starting — prevents accumulation across
  systemd restarts.
- Added kill_orphan_phone_connect() — kills stale Node.js processes bound
  to port 3000 before Phone Connect starts. Prevents "port already in use"
  after crash without proper cleanup.
- Added _cleanup_all() — called both at startup AND on shutdown (KeyboardInterrupt
  and SIGTERM). Ensures clean state on every lifecycle event.
- Registered SIGTERM handler so systemd stop/restart triggers full cleanup,
  not just KeyboardInterrupt path.
- _wait_for_port() replaces time.sleep(3) — active polling until port is ready.
- All other logic (os.killpg, start_new_session, MAX_RESTARTS) unchanged.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
import urllib.request
import urllib.error

from dotenv import load_dotenv
from app.logger import setup_logger

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

HOME = Path.home()
PROJECT_DIR = BASE_DIR
VENV_PYTHON = PROJECT_DIR / "venv" / "bin" / "python"

if not VENV_PYTHON.exists():
    VENV_PYTHON = Path(sys.executable)

logger = setup_logger("launcher", "launcher.log")

PROJECTS_FILE = BASE_DIR / "projects.json"
PROJECTS_BASE_DIR = Path(
    os.getenv("PROJECTS_BASE_DIR", str(HOME / "antigravity" / "projects"))
)

_DEFAULT_PROJECT_DIR = os.getenv(
    "ANTIGRAVITY_PROJECT_DIR",
    str(HOME / "antigravity" / "projects" / "crewtask-v2"),
)


def resolve_active_project() -> Path:
    if PROJECTS_FILE.exists():
        try:
            with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
                projects = json.load(f)
            for p in projects:
                if p.get("active"):
                    return Path(p["path"])
        except (json.JSONDecodeError, KeyError):
            pass
    return Path(_DEFAULT_PROJECT_DIR)


PHONE_CONNECT_DIR = Path(
    os.getenv("PHONE_CONNECT_DIR", str(HOME / "antigravity_phone_chat"))
)

PHONE_WORKER_HOST = "127.0.0.1"
PHONE_WORKER_PORT = 8788
PHONE_WORKER_URL = f"http://{PHONE_WORKER_HOST}:{PHONE_WORKER_PORT}"

FILE_MONITOR_HOST = "127.0.0.1"
FILE_MONITOR_PORT = 8787

LAUNCH_ANTIGRAVITY = os.getenv("LAUNCH_ANTIGRAVITY", "true").lower() == "true"
AUTO_INIT_SESSION = os.getenv("AUTO_INIT_SESSION", "true").lower() == "true"
ANTIGRAVITY_DEBUG_PORT = int(os.getenv("ANTIGRAVITY_DEBUG_PORT", "9000"))

HEALTH_CHECK_INTERVAL = 30
MAX_RESTARTS = 5

# How long to wait for a port after service start.
# Node.js (Phone Connect) cold start can take 10-15s on ThinkPad E580.
PORT_WAIT_TIMEOUT = 45


# ---------------------------------------------------------------------------
# Process cleanup — the core fix for the "20 antigravity processes" problem
# ---------------------------------------------------------------------------

def _pkill(pattern: str, signal_num: int = signal.SIGTERM, wait: float = 2.0) -> int:
    """Kill all processes matching pattern. Returns number of PIDs killed."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        if not pids:
            return 0
        for pid in pids:
            try:
                os.kill(int(pid), signal_num)
            except ProcessLookupError:
                pass
        if wait > 0:
            time.sleep(wait)
        return len(pids)
    except Exception as e:
        logger.error(f"_pkill({pattern}) failed: {e}")
        return 0


def kill_all_antigravity() -> None:
    """Kill ALL antigravity processes unconditionally.

    Why unconditional (not "keep first")?
    After a systemd restart the "first" process may be a zombie or may have
    lost its CDP connection. Starting fresh is always safer than trying to
    reuse a stale process. Electron (antigravity) starts in ~3s on this hw.

    Two-pass strategy:
    1. SIGTERM — graceful shutdown, lets Electron flush its state.
    2. Wait 3s, then SIGKILL anything that survived.
    """
    count = _pkill("antigravity", signal.SIGTERM, wait=3.0)
    if count > 0:
        logger.info(f"Sent SIGTERM to {count} antigravity process(es).")
        # Second pass: kill survivors that ignored SIGTERM
        survivors = _pkill("antigravity", signal.SIGKILL, wait=1.0)
        if survivors:
            logger.warning(f"SIGKILL sent to {survivors} surviving antigravity process(es).")
    else:
        logger.info("No antigravity processes found — clean state.")


def kill_orphan_phone_connect() -> None:
    """Kill any stale Node.js process holding port 3000.

    After a crash without proper cleanup, the old Phone Connect process may
    still be alive (or in zombie state) holding the port. New instance can't
    bind and silently fails.
    """
    try:
        # fuser -k sends SIGKILL to whatever owns the port
        result = subprocess.run(
            ["fuser", "-k", "3000/tcp"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            logger.info("Killed stale process on port 3000 (Phone Connect).")
            time.sleep(1.0)
    except FileNotFoundError:
        # fuser not available — fallback to pkill
        _pkill("antigravity_phone_chat", signal.SIGTERM, wait=1.0)
    except Exception as e:
        logger.error(f"kill_orphan_phone_connect failed: {e}")


def _cleanup_all(services: list["Service"]) -> None:
    """Stop all managed services and clean up stale processes.

    Called both at shutdown (KeyboardInterrupt / SIGTERM) and at startup
    (before launching fresh instances). Ensures no accumulation of stale
    processes across restarts.
    """
    logger.info("Running full cleanup...")
    for svc in reversed(services):
        svc.stop()
    # Belt-and-suspenders: kill anything that escaped our process groups.
    if LAUNCH_ANTIGRAVITY:
        kill_all_antigravity()
    kill_orphan_phone_connect()
    logger.info("Cleanup complete.")


# ---------------------------------------------------------------------------
# Service management
# ---------------------------------------------------------------------------

class Service:
    def __init__(
        self,
        name: str,
        cmd: list,
        cwd: Path = None,
        check_port: int = None,
        depends_on_port: int = None,
    ):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.check_port = check_port
        self.depends_on_port = depends_on_port
        self.proc: subprocess.Popen | None = None
        self.log_file = None
        self.restart_count = 0
        self.started = False

    def start(self) -> None:
        if self.depends_on_port and not is_port_in_use("127.0.0.1", self.depends_on_port):
            logger.warning(
                f"{self.name}: dependency port {self.depends_on_port} not ready, waiting..."
            )
            _wait_for_port("127.0.0.1", self.depends_on_port, timeout=PORT_WAIT_TIMEOUT)

        if self.check_port and is_port_in_use("127.0.0.1", self.check_port):
            logger.info(
                f"{self.name}: port {self.check_port} already in use — skipping start."
            )
            self.started = True
            self.proc = None
            return

        log_path = PROJECT_DIR / "logs" / f"{self.name}_stdout.log"
        logger.info(f"Starting {self.name}: {' '.join(str(c) for c in self.cmd)}")
        self.log_file = open(log_path, "w", encoding="utf-8")

        # start_new_session=True: child gets its own process group.
        # os.killpg can then kill the entire subtree with one signal.
        self.proc = subprocess.Popen(
            [str(c) for c in self.cmd],
            cwd=self.cwd,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.started = True

        if self.check_port:
            _wait_for_port("127.0.0.1", self.check_port, timeout=PORT_WAIT_TIMEOUT)

    def is_alive(self) -> bool:
        if self.check_port and self.started:
            return is_port_in_use("127.0.0.1", self.check_port)
        if self.proc is None:
            return self.started
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            logger.info(f"Stopping {self.name} (pid={self.proc.pid})...")
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning(f"{self.name}: SIGTERM timeout — sending SIGKILL.")
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            except ProcessLookupError:
                pass  # already gone
            except Exception as e:
                logger.error(f"Error stopping {self.name}: {e}")
        if self.log_file:
            self.log_file.close()
            self.log_file = None

    def restart(self) -> bool:
        if self.restart_count >= MAX_RESTARTS:
            logger.error(
                f"{self.name}: restart limit ({MAX_RESTARTS}) reached. "
                "Manual intervention required."
            )
            return False
        self.restart_count += 1
        logger.info(
            f"Restarting {self.name} (attempt {self.restart_count}/{MAX_RESTARTS})..."
        )
        self.stop()
        # For Antigravity specifically — kill all before restart to prevent
        # accumulation. Other services just stop their own process.
        if self.name == "Antigravity" and LAUNCH_ANTIGRAVITY:
            kill_all_antigravity()
        time.sleep(2)
        self.start()
        return True


def is_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except (OSError, ConnectionRefusedError):
            return False


def _wait_for_port(host: str, port: int, timeout: int = PORT_WAIT_TIMEOUT) -> None:
    """Active polling until port accepts connections or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_port_in_use(host, port):
            logger.info(f"Port {port} is ready.")
            return
        time.sleep(0.5)
    logger.warning(
        f"Port {port} on {host} not ready after {timeout}s — continuing anyway."
    )


def http_get_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    antigravity_project_dir = resolve_active_project()
    logger.info("=== Antigravity Assistant launcher starting ===")
    logger.info(f"Active project: {antigravity_project_dir}")

    # --- Pre-launch cleanup ---
    # Kill ALL stale antigravity processes before starting fresh.
    # This is the primary fix for the "20 antigravity processes" accumulation.
    if LAUNCH_ANTIGRAVITY:
        kill_all_antigravity()
    kill_orphan_phone_connect()

    services: list[Service] = []

    if LAUNCH_ANTIGRAVITY:
        if antigravity_project_dir.exists():
            services.append(
                Service(
                    name="Antigravity",
                    cmd=[
                        "antigravity",
                        ".",
                        f"--remote-debugging-port={ANTIGRAVITY_DEBUG_PORT}",
                    ],
                    cwd=antigravity_project_dir,
                    check_port=ANTIGRAVITY_DEBUG_PORT,
                )
            )
        else:
            logger.error(f"Project directory not found: {antigravity_project_dir}")

    if PHONE_CONNECT_DIR.exists():
        services.append(
            Service(
                name="Phone_Connect",
                cmd=["python3", "launcher.py"],
                cwd=PHONE_CONNECT_DIR,
                check_port=3000,
                depends_on_port=ANTIGRAVITY_DEBUG_PORT,
            )
        )
    else:
        logger.error(f"Phone Connect directory not found: {PHONE_CONNECT_DIR}")

    services.append(
        Service(
            name="phone_worker",
            cmd=[
                str(VENV_PYTHON), "-m", "uvicorn", "app.phone_worker:app",
                "--host", PHONE_WORKER_HOST, "--port", str(PHONE_WORKER_PORT),
            ],
            cwd=PROJECT_DIR,
            check_port=PHONE_WORKER_PORT,
            depends_on_port=3000,
        )
    )

    services.append(
        Service(
            name="file_monitor",
            cmd=[
                str(VENV_PYTHON), "-m", "uvicorn", "app.file_monitor:app",
                "--host", FILE_MONITOR_HOST, "--port", str(FILE_MONITOR_PORT),
            ],
            cwd=PROJECT_DIR,
            check_port=FILE_MONITOR_PORT,
        )
    )

    services.append(
        Service(
            name="tg_bot",
            cmd=[str(VENV_PYTHON), "-m", "app.tg_bot"],
            cwd=PROJECT_DIR,
        )
    )

    # Register SIGTERM handler so systemd stop/restart triggers proper cleanup.
    # Without this, systemd sends SIGTERM to the launcher but child processes
    # (especially Antigravity/Electron) keep running until the OS kills them.
    def _sigterm_handler(signum, frame):
        logger.info("SIGTERM received — shutting down cleanly.")
        _cleanup_all(services)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Start services sequentially — each waits for its port before next starts.
    for svc in services:
        svc.start()

    logger.info("=== All services started ===")

    if AUTO_INIT_SESSION:
        time.sleep(2)
        if http_get_ok(f"{PHONE_WORKER_URL}/health"):
            try:
                req = urllib.request.Request(
                    f"{PHONE_WORKER_URL}/init",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10)
                logger.info("Session auto-initialized via /init.")
            except Exception as e:
                logger.error(f"Auto-init failed: {e}")
        else:
            logger.warning("phone_worker /health failed — skipping auto-init.")

    # Health-check loop.
    try:
        while True:
            time.sleep(HEALTH_CHECK_INTERVAL)
            for svc in services:
                if not svc.is_alive() and svc.proc is not None:
                    logger.warning(f"{svc.name} is not alive — restarting...")
                    svc.restart()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down...")
        _cleanup_all(services)
        logger.info("=== Shutdown complete ===")


if __name__ == "__main__":
    main()
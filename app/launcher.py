"""
launcher.py — Unified launcher for all Antigravity Assistant services.

CHANGES vs original:
- Replaced blind time.sleep(3) between service starts with _wait_for_port(),
  which actively polls until the port is accepting connections (or times out).
  This prevents phone_worker from starting before Phone Connect is truly ready.
- Added StandardOutput/StandardError passthrough note in docstring (configured
  in the systemd unit, not here).
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
    """Read projects.json and return the path of the active project.

    Falls back to ANTIGRAVITY_PROJECT_DIR env var if no projects.json exists
    or if no project is marked active.
    """
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

# How long (seconds) to wait for a port to become available after service start.
# Node.js (Phone Connect) cold start can take 8-12s on ThinkPad E580.
PORT_WAIT_TIMEOUT = 45


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
        # Wait for dependency port before starting this service.
        if self.depends_on_port and not is_port_in_use("127.0.0.1", self.depends_on_port):
            logger.warning(
                f"{self.name}: dependency port {self.depends_on_port} not ready, waiting..."
            )
            _wait_for_port("127.0.0.1", self.depends_on_port, timeout=PORT_WAIT_TIMEOUT)

        # If the service port is already occupied — assume it's already running.
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

        # start_new_session=True puts the child in its own process group.
        # This is required for os.killpg to work — it lets us kill the entire
        # subtree (including Node.js children spawned by Phone Connect) with
        # a single signal, preventing zombie/orphan processes.
        self.proc = subprocess.Popen(
            [str(c) for c in self.cmd],
            cwd=self.cwd,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.started = True

        # Wait for the service's own port to come up before proceeding.
        # This replaces the old blanket time.sleep(3).
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
            logger.info(f"Stopping {self.name}...")
            try:
                # Kill the entire process group — catches all child processes
                # (e.g. Node.js workers spawned by Phone Connect launcher).
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning(f"{self.name}: SIGTERM timed out — sending SIGKILL.")
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # Process already gone — that's fine.
            except Exception as e:
                logger.error(f"Error stopping {self.name}: {e}")
        if self.log_file:
            self.log_file.close()
            self.log_file = None

    def restart(self) -> bool:
        if self.restart_count >= MAX_RESTARTS:
            logger.error(
                f"{self.name}: restart limit reached ({MAX_RESTARTS}). Manual intervention required."
            )
            return False
        self.restart_count += 1
        logger.info(f"Restarting {self.name} (attempt {self.restart_count}/{MAX_RESTARTS})...")
        self.stop()
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
    """Block until the port starts accepting connections or timeout is reached.

    Replaces the old time.sleep(3) between service starts with active polling.
    On ThinkPad E580 with cold Node.js start, Phone Connect can take 10-15s.
    """
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
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def find_and_kill_extra_antigravity() -> None:
    """Kill duplicate Antigravity IDE instances.

    Multiple instances on the same debug port cause silent failures where
    Phone Connect attaches to a stale process.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "antigravity.*--remote-debugging-port"],
            capture_output=True,
            text=True,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        if len(pids) > 1:
            logger.warning(
                f"{len(pids)} antigravity processes found. Keeping first, killing rest."
            )
            for pid in pids[1:]:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
    except Exception as e:
        logger.error(f"Failed to clean up extra Antigravity instances: {e}")


def main() -> None:
    antigravity_project_dir = resolve_active_project()
    logger.info("=== Antigravity Assistant launcher starting ===")
    logger.info(f"Active project: {antigravity_project_dir}")

    services: list[Service] = []

    if LAUNCH_ANTIGRAVITY:
        if antigravity_project_dir.exists():
            find_and_kill_extra_antigravity()
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
                str(VENV_PYTHON),
                "-m",
                "uvicorn",
                "app.phone_worker:app",
                "--host",
                PHONE_WORKER_HOST,
                "--port",
                str(PHONE_WORKER_PORT),
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
                str(VENV_PYTHON),
                "-m",
                "uvicorn",
                "app.file_monitor:app",
                "--host",
                FILE_MONITOR_HOST,
                "--port",
                str(FILE_MONITOR_PORT),
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

    # Start each service and wait for its port before proceeding to the next.
    # _wait_for_port is called inside Service.start() — no sleep needed here.
    for svc in services:
        svc.start()

    logger.info("=== All services started ===")

    if AUTO_INIT_SESSION:
        # Give phone_worker a moment to register routes after port is up.
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
            logger.warning("phone_worker /health check failed — skipping auto-init.")

    # Health-check loop: restart any dead service automatically.
    try:
        while True:
            time.sleep(HEALTH_CHECK_INTERVAL)
            for svc in services:
                if not svc.is_alive() and svc.proc is not None:
                    logger.warning(f"{svc.name} is not alive — attempting restart...")
                    svc.restart()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down all services...")
        for svc in reversed(services):
            svc.stop()
        logger.info("=== Shutdown complete ===")


if __name__ == "__main__":
    main()

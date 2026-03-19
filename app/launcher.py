"""
launcher.py — Unified launcher for all Antigravity Assistant services.

Reads the active project from projects.json (if it exists) or falls back
to ANTIGRAVITY_PROJECT_DIR from .env.  The TG bot can switch projects by
updating projects.json and then restarting this service via
    systemctl --user restart antigravity-assistant
"""

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

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

HOME = Path.home()
PROJECT_DIR = BASE_DIR
VENV_PYTHON = PROJECT_DIR / "venv" / "bin" / "python"

if not VENV_PYTHON.exists():
    VENV_PYTHON = Path(sys.executable)

# ---------- Project resolution ----------

PROJECTS_FILE = BASE_DIR / "projects.json"

# Base directory that contains all Antigravity projects
# e.g. /home/sagitt/antigravity/projects
PROJECTS_BASE_DIR = Path(
    os.getenv("PROJECTS_BASE_DIR", str(HOME / "antigravity" / "projects"))
)

# Default project from .env (used as fallback)
_DEFAULT_PROJECT_DIR = os.getenv(
    "ANTIGRAVITY_PROJECT_DIR",
    str(HOME / "antigravity" / "projects" / "crewtask-v2"),
)


def resolve_active_project() -> Path:
    """Determine which project directory to open in Antigravity.

    Priority:
    1. Active project in projects.json
    2. ANTIGRAVITY_PROJECT_DIR from .env
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

LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

HEALTH_CHECK_INTERVAL = 30
MAX_RESTARTS = 5


class Service:
    def __init__(self, name, cmd, cwd=None, check_port=None, depends_on_port=None):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.check_port = check_port
        self.depends_on_port = depends_on_port
        self.proc = None
        self.log_file = None
        self.restart_count = 0
        self.started = False

    def start(self):
        if self.depends_on_port and not is_port_in_use("127.0.0.1", self.depends_on_port):
            print(f"⚠️  {self.name}: waiting port {self.depends_on_port}...")
            for _ in range(30):
                time.sleep(1)
                if is_port_in_use("127.0.0.1", self.depends_on_port):
                    break
            else:
                print(f"⚠️  {self.name}: port {self.depends_on_port} is not ready during 30 sec")

        if self.check_port and is_port_in_use("127.0.0.1", self.check_port):
            print(f"ℹ️  {self.name}: port {self.check_port} busy - may be already working.")
            self.started = True
            self.proc = None  # Clear stale proc reference on port-busy reattach
            return

        log_path = LOG_DIR / f"{self.name}.log"
        print(f"→ Starting {self.name}: {' '.join(str(c) for c in self.cmd)}")
        self.log_file = open(log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(c) for c in self.cmd],
            cwd=self.cwd,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.started = True

    def is_alive(self):
        # If we have a check_port, use it as the primary health signal.
        # This handles GUI apps (like Antigravity) that fork/detach:
        # the wrapper process exits immediately, but the app keeps the port open.
        if self.check_port and self.started:
            return is_port_in_use("127.0.0.1", self.check_port)
        if self.proc is None:
            return self.started
        return self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            print(f"→ Stopping {self.name}...")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log_file:
            self.log_file.close()

    def restart(self):
        if self.restart_count >= MAX_RESTARTS:
            print(f"❌ {self.name}: restart limit exceeded ({MAX_RESTARTS})")
            return False
        self.restart_count += 1
        print(f"🔄 Restart {self.name} (attempt {self.restart_count}/{MAX_RESTARTS})...")
        self.stop()
        time.sleep(2)
        self.start()
        return True


def is_port_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except (OSError, ConnectionRefusedError):
            return False


def http_get_ok(url, timeout=5.0):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def find_and_kill_extra_antigravity():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "antigravity.*--remote-debugging-port"],
            capture_output=True, text=True
        )
        pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
        if len(pids) > 1:
            print(f"⚠️   {len(pids)} antigravity processes found. Keeping the first one, killing the rest.")
            for pid in pids[1:]:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
    except Exception:
        pass


def main():
    # Resolve which project to open
    antigravity_project_dir = resolve_active_project()

    print("🚀 Launcher: starting Antigravity Assistant")
    print(f"   Project DIRECTORY: {PROJECT_DIR}")
    print(f"   Antigravity project: {antigravity_project_dir}")
    print(f"   Phone Connect: {PHONE_CONNECT_DIR}")
    print()

    services = []

    # 1. Antigravity IDE
    if LAUNCH_ANTIGRAVITY:
        if antigravity_project_dir.exists():
            find_and_kill_extra_antigravity()
            services.append(Service(
                name="Antigravity",
                cmd=["antigravity", ".", f"--remote-debugging-port={ANTIGRAVITY_DEBUG_PORT}"],
                cwd=antigravity_project_dir,
                check_port=ANTIGRAVITY_DEBUG_PORT,
            ))
        else:
            print(f"⚠️  The directory named Antigravity was not found: {antigravity_project_dir}")
    else:
        print("ℹ️  LAUNCH_ANTIGRAVITY=false - skipping.")

    # 2. Phone Connect
    if PHONE_CONNECT_DIR.exists():
        services.append(Service(
            name="Phone_Connect",
            cmd=["python3", "launcher.py"],
            cwd=PHONE_CONNECT_DIR,
            check_port=3000,
            depends_on_port=ANTIGRAVITY_DEBUG_PORT,
        ))
    else:
        print(f"⚠️  The directory named  Phone Connect was not found: {PHONE_CONNECT_DIR}")

    # 3. Phone Worker
    services.append(Service(
        name="phone_worker",
        cmd=[str(VENV_PYTHON), "-m", "uvicorn", "app.phone_worker:app",
             "--host", PHONE_WORKER_HOST, "--port", str(PHONE_WORKER_PORT)],
        cwd=PROJECT_DIR,
        check_port=PHONE_WORKER_PORT,
        depends_on_port=3000,
    ))

    # 4. File Monitor
    services.append(Service(
        name="file_monitor",
        cmd=[str(VENV_PYTHON), "-m", "uvicorn", "app.file_monitor:app",
             "--host", FILE_MONITOR_HOST, "--port", str(FILE_MONITOR_PORT)],
        cwd=PROJECT_DIR,
        check_port=FILE_MONITOR_PORT,
    ))

    # 5. Telegram Bot
    services.append(Service(
        name="tg_bot",
        cmd=[str(VENV_PYTHON), "-m", "app.tg_bot"],
        cwd=PROJECT_DIR,
    ))

    for svc in services:
        svc.start()
        time.sleep(3)

    print("\n✅ All services are running:")
    for svc in services:
        status = "✅" if svc.is_alive() else "❌"
        port_info = f" (port {svc.check_port})" if svc.check_port else ""
        print(f"   {svc.name}: {status}{port_info}")

    if AUTO_INIT_SESSION:
        time.sleep(5)
        if http_get_ok(f"{PHONE_WORKER_URL}/health"):
            try:
                req = urllib.request.Request(f"{PHONE_WORKER_URL}/init", method="POST",
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
                print("✅ Session initialized.")
            except Exception as e:
                print(f"⚠️  Init failed: {e}")

    print("\n" + "="*50)
    print("🟢 Antigravity Assistant is running.")
    print("   Open Telegram and message the bot.")
    print("   Ctrl+C - stop everything.")
    print("="*50 + "\n")

    try:
        while True:
            time.sleep(HEALTH_CHECK_INTERVAL)
            for svc in services:
                if not svc.is_alive() and svc.proc is not None:
                    print(f"⚠️  {svc.name} fell!")
                    svc.restart()
    except KeyboardInterrupt:
        print("\n⏹ Stopping all services...")
        for svc in reversed(services):
            svc.stop()
        print("👋 Done.")


if __name__ == "__main__":
    main()

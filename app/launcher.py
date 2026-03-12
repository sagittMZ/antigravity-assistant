import os
import socket
import subprocess
import sys
import time
from pathlib import Path
import urllib.request
import urllib.error

from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# Paths and env
# ─────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent  # project root: antigravity-assistant
load_dotenv(BASE_DIR / ".env")

HOME = Path.home()
PROJECT_DIR = BASE_DIR
VENV_PYTHON = PROJECT_DIR / "venv" / "bin" / "python"

# Paths can be overridden from .env
ANTIGRAVITY_PROJECT_DIR = Path(
    os.getenv("ANTIGRAVITY_PROJECT_DIR", str(HOME / "antigravity" / "projects" / "crewtask-v2"))
)
PHONE_CONNECT_DIR = Path(
    os.getenv("PHONE_CONNECT_DIR", str(HOME / "antigravity_phone_chat"))
)

PHONE_WORKER_HOST = "127.0.0.1"
PHONE_WORKER_PORT = 8788
PHONE_WORKER_URL = f"http://{PHONE_WORKER_HOST}:{PHONE_WORKER_PORT}"

LAUNCH_ANTIGRAVITY = os.getenv("LAUNCH_ANTIGRAVITY", "true").lower() == "true"
AUTO_INIT_SESSION = os.getenv("AUTO_INIT_SESSION", "true").lower() == "true"

LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def run_process(cmd, cwd=None, name=None):
    """Start subprocess and write its stdout/stderr into a log file."""
    proc_name = (name or cmd[0]).replace(" ", "_")
    log_path = LOG_DIR / f"{proc_name}.log"
    print(f"→ Starting {name or cmd[0]}: {' '.join(str(c) for c in cmd)} (cwd={cwd}), log: {log_path}")
    log_file = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [str(c) for c in cmd],
        cwd=cwd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def http_post(url: str, timeout: float = 10.0) -> bool:
    """Best-effort HTTP POST helper. Returns False on any error."""
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _ = resp.read()
        return True
    except urllib.error.URLError as e:
        print(f"⚠️ HTTP POST {url} failed: {e}")
        return False


def is_port_in_use(host: str, port: int) -> bool:
    """Check if TCP port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except (OSError, ConnectionRefusedError):
            return False


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    if not VENV_PYTHON.exists():
        print(f"Python from venv not found: {VENV_PYTHON}")
        sys.exit(1)

    print("🚀 Launcher: starting Antigravity + Phone Connect + assistant stack")
    print(f"Assistant project dir: {PROJECT_DIR}")
    print(f"AUTO_INIT_SESSION={AUTO_INIT_SESSION}, LAUNCH_ANTIGRAVITY={LAUNCH_ANTIGRAVITY}")

    processes = []

    # 1. Antigravity IDE (optional)
    ag_proc = None
    if LAUNCH_ANTIGRAVITY:
        if ANTIGRAVITY_PROJECT_DIR.exists():
            if is_port_in_use("127.0.0.1", 9000):
                print("ℹ️ Port 9000 is already in use. Assuming Antigravity is already running.")
            else:
                print("→ Starting Antigravity IDE with --remote-debugging-port=9000 ...")
                ag_cmd = ["antigravity", ".", "--remote-debugging-port=9000"]
                ag_proc = run_process(ag_cmd, cwd=ANTIGRAVITY_PROJECT_DIR, name="Antigravity")
                processes.append(("Antigravity", ag_proc))
                time.sleep(5)
        else:
            print(f"⚠️ Antigravity project folder not found: {ANTIGRAVITY_PROJECT_DIR}")
    else:
        print(
            "ℹ️ LAUNCH_ANTIGRAVITY=false, skipping Antigravity startup. "
            "Make sure IDE is running with --remote-debugging-port=9000."
        )

    # 2. Phone Connect
    pc_proc = None
    if PHONE_CONNECT_DIR.exists():
        print("→ Starting Phone Connect (python3 launcher.py) ...")
        pc_cmd = ["python3", "launcher.py"]
        pc_proc = run_process(pc_cmd, cwd=PHONE_CONNECT_DIR, name="Phone_Connect")
        processes.append(("Phone_Connect", pc_proc))
        time.sleep(8)
    else:
        print(f"⚠️ Phone Connect folder not found: {PHONE_CONNECT_DIR}")

    # 3. phone_worker (Playwright + FastAPI)
    if is_port_in_use(PHONE_WORKER_HOST, PHONE_WORKER_PORT):
        print(f"ℹ️ Port {PHONE_WORKER_PORT} already in use. Assuming phone_worker is already running.")
        pw_proc = None
    else:
        print("→ Starting phone_worker (Playwright + FastAPI) ...")
        pw_cmd = [
            VENV_PYTHON,
            "-m",
            "uvicorn",
            "app.phone_worker:app",
            "--host",
            PHONE_WORKER_HOST,
            "--port",
            str(PHONE_WORKER_PORT),
        ]
        pw_proc = run_process(pw_cmd, cwd=PROJECT_DIR, name="phone_worker")
        processes.append(("phone_worker", pw_proc))
        time.sleep(3)

    # 4. Initialize first session via Playwright (optional, never fatal)
    if AUTO_INIT_SESSION:
        print("→ Sending initial message via phone_worker /init ...")
        init_ok = http_post(f"{PHONE_WORKER_URL}/init", timeout=5.0)
        if init_ok:
            print("✅ Initial message sent successfully.")
        else:
            print(
                "⚠️ Failed to send initial message. "
                "Check PHONE_CONNECT_URL and Phone Connect state. "
                "Continuing without init."
            )
    else:
        print("ℹ️ AUTO_INIT_SESSION=false, skipping initial /init call.")

    # 5. file_monitor (uvicorn)
    print("→ Starting file_monitor (uvicorn) ...")
    fm_cmd = [
        VENV_PYTHON,
        "-m",
        "uvicorn",
        "app.file_monitor:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8787",
    ]
    fm_proc = run_process(fm_cmd, cwd=PROJECT_DIR, name="file_monitor")
    processes.append(("file_monitor", fm_proc))
    time.sleep(1)

    # 6. Telegram bot
    print("→ Starting Telegram bot (app.tg_bot) ...")
    bot_cmd = [VENV_PYTHON, "-m", "app.tg_bot"]
    bot_proc = run_process(bot_cmd, cwd=PROJECT_DIR, name="tg_bot")
    processes.append(("tg_bot", bot_proc))
    time.sleep(1)

    print("\n✅ All available services started (if paths and URLs are correct):")
    if ag_proc or is_port_in_use("127.0.0.1", 9000):
        print(f"- Antigravity: project {ANTIGRAVITY_PROJECT_DIR} (or already running on port 9000)")
    if pc_proc:
        print(f"- Phone Connect: {PHONE_CONNECT_DIR}")
    if pw_proc or is_port_in_use(PHONE_WORKER_HOST, PHONE_WORKER_PORT):
        print(f"- phone_worker: {PHONE_WORKER_URL}")
    print("- file_monitor: http://127.0.0.1:8787")
    print("- tg_bot: listening for Telegram updates")

    print("\nPress Ctrl+C to stop all processes.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⏹ Stopping processes...")
        for name, proc in processes:
            if proc and proc.poll() is None:
                print(f"→ Terminating {name} ...")
                proc.terminate()
        print("👋 Launcher finished.")


if __name__ == "__main__":
    main()

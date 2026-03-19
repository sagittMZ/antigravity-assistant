"""
tg_bot.py — Telegram bot for full bidirectional communication
with Antigravity AI agent via Phone Connect.

Features:
- Send prompts to Antigravity from Telegram
- Receive AI responses back in Telegram
- Dynamic model list fetched from Antigravity UI (no hardcoded models)
- Choose mode (Fast/Planning)
- View implementation plans, tasks, walkthroughs
- Start new chats, stop generation
- View brain session files
- Manage and switch between multiple projects
- Launch/restart Antigravity with selected project from TG
- Session logging
- Background polling: detects new messages even when user works on desktop
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")
ARTIFACTS_DIR = BASE_DIR / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")
if not ALLOWED_USER_ID:
    raise RuntimeError("ALLOWED_USER_ID is not set in .env")

SESSION_FILE = BASE_DIR / "session.json"
PROJECTS_FILE = BASE_DIR / "projects.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "agent.log"

FILE_SERVICE_URL = os.getenv("FILE_SERVICE_URL", "http://127.0.0.1:8787").strip()
PHONE_WORKER_URL = os.getenv("PHONE_WORKER_URL", "http://127.0.0.1:8788").strip()

# Base directory containing all Antigravity projects
PROJECTS_BASE_DIR = Path(
    os.getenv("PROJECTS_BASE_DIR", str(Path.home() / "antigravity" / "projects"))
)

# systemd user service name for restart
SYSTEMD_SERVICE_NAME = os.getenv("SYSTEMD_SERVICE_NAME", "antigravity-assistant")

# Polling interval for checking AI responses (seconds)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3.0"))
# How long to poll for a response before giving up (seconds)
POLL_TIMEOUT = float(os.getenv("POLL_TIMEOUT", "600"))
# Background watcher interval — how often to check for changes when idle (seconds)
BG_WATCH_INTERVAL = float(os.getenv("BG_WATCH_INTERVAL", "10.0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- State ----------

_known_message_hashes: set[str] = set()
_polling_active = False
_last_snapshot_hash: Optional[str] = None
_waiting_for_response = False
_pending_status_msg: Optional[types.Message] = None

# Background watcher state
_bg_watcher_running = False
_bg_last_hash: Optional[str] = None
_bg_baseline_hashes: set[str] = set()
_active_chat_id: Optional[int] = None


# ---------- FSM for adding new project ----------

class AddProjectStates(StatesGroup):
    waiting_for_name = State()


# ---------- Session / Projects ----------

def load_session() -> dict:
    if SESSION_FILE.exists():
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"thread_id": str(int(time.time()))}


def save_session(session_data: dict):
    session_data["last_updated"] = datetime.now().isoformat()
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)


def load_projects() -> list[dict]:
    """Load project list from projects.json.

    Each project: {"name": "...", "path": "/full/path/...", "active": bool}
    If file doesn't exist, seed it from ANTIGRAVITY_PROJECT_DIR in .env.
    """
    if PROJECTS_FILE.exists():
        try:
            with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
                projects = json.load(f)
            if isinstance(projects, list) and projects:
                return projects
        except (json.JSONDecodeError, ValueError):
            pass

    # Seed from .env default
    default_path = os.getenv("ANTIGRAVITY_PROJECT_DIR", "")
    if default_path:
        projects = [{"name": Path(default_path).name, "path": default_path, "active": True}]
    else:
        projects = []

    # Auto-discover existing project directories
    if PROJECTS_BASE_DIR.exists():
        for d in sorted(PROJECTS_BASE_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                already = any(p["path"] == str(d) for p in projects)
                if not already:
                    projects.append({
                        "name": d.name,
                        "path": str(d),
                        "active": len(projects) == 0,
                    })

    save_projects(projects)
    return projects


def save_projects(projects: list[dict]):
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def get_active_project() -> Optional[dict]:
    projects = load_projects()
    for p in projects:
        if p.get("active"):
            return p
    return projects[0] if projects else None


def set_active_project(project_name: str) -> Optional[dict]:
    """Set a project as active by name. Returns the activated project or None."""
    projects = load_projects()
    target = None
    for p in projects:
        if p["name"] == project_name:
            p["active"] = True
            target = p
        else:
            p["active"] = False
    if target:
        save_projects(projects)
    return target


def add_project(name: str) -> dict:
    """Add a new project by name. Path = PROJECTS_BASE_DIR / name.
    Returns the new project dict.
    """
    projects = load_projects()

    # Check if already exists
    for p in projects:
        if p["name"] == name:
            return p

    project_path = str(PROJECTS_BASE_DIR / name)
    new_project = {"name": name, "path": project_path, "active": False}
    projects.append(new_project)
    save_projects(projects)
    return new_project


def remove_project(name: str) -> bool:
    """Remove a project from the list (does not delete files)."""
    projects = load_projects()
    original_len = len(projects)
    projects = [p for p in projects if p["name"] != name]
    if len(projects) < original_len:
        # If we removed the active project, activate the first one
        if projects and not any(p.get("active") for p in projects):
            projects[0]["active"] = True
        save_projects(projects)
        return True
    return False


current_session = load_session()


# ---------- Logging ----------

def write_log(sender: str, message: str):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%H:%M:%S")
        f.write(f"[{timestamp}] {sender}: {message}\n")


# ---------- HTTP helpers ----------

async def pw_get(path: str) -> dict:
    """GET request to phone_worker."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{PHONE_WORKER_URL}{path}",
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.json()


async def pw_post(path: str, data: dict = None) -> dict:
    """POST request to phone_worker."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{PHONE_WORKER_URL}{path}",
            json=data or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.json()


async def fm_get(path: str) -> str:
    """GET text content from file_monitor."""
    url = f"{FILE_SERVICE_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            if resp.status == 200:
                return text
            raise RuntimeError(f"HTTP {resp.status}: {text}")


# ---------- Service restart ----------

async def restart_service():
    """Restart the systemd user service (the whole launcher).

    This is how we apply a project switch: bot updates projects.json,
    then restarts the launcher which reads the new active project.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", SYSTEMD_SERVICE_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, "Service restarted."
        else:
            err = stderr.decode().strip() or stdout.decode().strip()
            return False, f"Restart failed: {err}"
    except Exception as e:
        return False, f"Restart error: {e}"


# ---------- Content dedup ----------

# Texts we have already sent to Telegram (prefix → full text).
# Using first 120 chars as key: if a new message starts with the same
# 120-char prefix, it is either a duplicate or a longer streaming version
# of something we already sent.
_sent_text_prefixes: dict[str, str] = {}  # prefix → last sent text


def _text_prefix(text: str, length: int = 120) -> str:
    """Return a stable prefix for dedup comparison."""
    return text[:length].strip()


def _is_duplicate(text: str) -> bool:
    """Check if this text has already been sent (or is a substring of one).

    Also catches the reverse: if we already sent a shorter version and now
    the message grew (streaming), we skip it because the meaningful content
    was already delivered.
    """
    prefix = _text_prefix(text)
    if not prefix:
        return True

    # Exact prefix match — same message (possibly grew during streaming)
    if prefix in _sent_text_prefixes:
        return True

    # Check if any previously sent text contains this text or vice versa
    for sent_prefix, sent_full in _sent_text_prefixes.items():
        # New text is a subset of something we already sent
        if text in sent_full:
            return True
        # Something we sent is a subset of the new text (streaming grew it)
        if sent_full in text:
            # Update the stored version but still skip sending
            _sent_text_prefixes[sent_prefix] = text
            return True

    return False


def _mark_sent(text: str):
    """Record that we sent this text to Telegram."""
    prefix = _text_prefix(text)
    if prefix:
        _sent_text_prefixes[prefix] = text


def _reset_sent_texts():
    """Clear sent text tracker (e.g. on new chat)."""
    _sent_text_prefixes.clear()


# ---------- Message polling ----------

async def get_current_messages() -> list[dict]:
    """Fetch current messages from Antigravity via snapshot."""
    try:
        data = await pw_get("/snapshot/text")
        return data.get("messages", [])
    except Exception as e:
        write_log("ERROR", f"Failed to get snapshot: {e}")
        return []


async def get_snapshot_hash() -> Optional[str]:
    """Get hash of current snapshot to detect changes."""
    try:
        data = await pw_get("/snapshot")
        html = data.get("html", "")
        if html:
            return hashlib.md5(html.encode()).hexdigest()
    except Exception:
        pass
    return None


async def poll_for_response(chat_id: int, timeout: float = None):
    """Poll for new messages from Antigravity after a prompt was sent.

    Keeps polling until:
    - We delivered at least one assistant message AND the snapshot
      has been stable for a while (AI finished responding), OR
    - The timeout expires.

    Key insight: while the AI is *thinking*, the snapshot HTML keeps
    changing (thinking bubbles, progress bars) but the parser filters
    all of that out, so we get 0 new meaningful messages.  We must NOT
    treat those thinking-only changes as "the AI is still responding" —
    instead we track stability separately.
    """
    global _polling_active, _last_snapshot_hash, _waiting_for_response
    global _bg_last_hash, _bg_baseline_hashes

    if _polling_active:
        return

    _polling_active = True
    _waiting_for_response = True
    effective_timeout = timeout or POLL_TIMEOUT
    start_time = time.time()

    # Get initial snapshot hash
    _last_snapshot_hash = await get_snapshot_hash()

    # Take a baseline of existing messages AND register their texts
    # so we never re-send content that was already visible.
    baseline = await get_current_messages()
    baseline_hashes = {m["hash"] for m in baseline}
    for m in baseline:
        _mark_sent(m["text"])

    prev_hash = _last_snapshot_hash
    ever_sent = False          # Did we deliver at least one real message?
    stable_polls = 0           # How many polls with NO snapshot change
    # How many polls to wait for "stable" after we sent something:
    #   10 polls * 3s = 30s of no change → done
    STABLE_AFTER_SEND = 10
    # How many polls to wait for "idle" when we never got a response:
    #   50 polls * 3s = 150s (2.5 min) of total silence → give up
    STABLE_NO_RESPONSE = 50

    try:
        while time.time() - start_time < effective_timeout:
            await asyncio.sleep(POLL_INTERVAL)

            current_hash = await get_snapshot_hash()

            if current_hash == prev_hash:
                stable_polls += 1

                # Exit conditions based on stability
                if ever_sent and stable_polls >= STABLE_AFTER_SEND:
                    # We already sent content and snapshot is stable → done
                    break
                if not ever_sent and stable_polls >= STABLE_NO_RESPONSE:
                    # Never got content and snapshot stopped changing → AI
                    # probably finished but response was all filtered out,
                    # or something is stuck.  Do a final check and exit.
                    break
                continue

            # Snapshot changed
            stable_polls = 0
            prev_hash = current_hash

            # Check for new meaningful messages
            current_msgs = await get_current_messages()
            new_msgs = [m for m in current_msgs if m["hash"] not in baseline_hashes]

            for msg in new_msgs:
                baseline_hashes.add(msg["hash"])

                # Skip user messages — the user already knows what
                # they sent; we only forward assistant responses.
                if msg["role"] == "user":
                    _mark_sent(msg["text"])
                    continue

                text = msg["text"]

                # Skip duplicates
                if _is_duplicate(text):
                    continue

                await send_long_message(chat_id, f"\U0001f916 {text}")
                _mark_sent(text)
                write_log("AI_RESPONSE", text[:200])
                ever_sent = True

        # --- Final sweep ---
        # When we exit the loop (timeout or stability), do one last fetch
        # to catch any messages that appeared in the last moments.
        try:
            final_msgs = await get_current_messages()
            for msg in final_msgs:
                if msg["hash"] in baseline_hashes:
                    continue
                baseline_hashes.add(msg["hash"])
                if msg["role"] == "user":
                    _mark_sent(msg["text"])
                    continue
                text = msg["text"]
                if _is_duplicate(text):
                    continue
                await send_long_message(chat_id, f"\U0001f916 {text}")
                _mark_sent(text)
                write_log("AI_RESPONSE_FINAL", text[:200])
                ever_sent = True
        except Exception:
            pass

        if not ever_sent:
            write_log("POLL_TIMEOUT", "No AI response received during polling")

    except Exception as e:
        write_log("POLL_ERROR", str(e))
        await bot.send_message(chat_id, f"Polling error: {e}")
    finally:
        _polling_active = False
        _waiting_for_response = False
        # Update background watcher baseline so it doesn't re-send these messages
        _bg_last_hash = prev_hash
        _bg_baseline_hashes = baseline_hashes


async def send_long_message(chat_id: int, text: str, **kwargs):
    """Send a message, splitting if too long for Telegram."""
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await bot.send_message(chat_id, text, **kwargs)
        return

    # Split by paragraphs first
    parts = []
    current = ""
    for line in text.split('\n'):
        if len(current) + len(line) + 1 > MAX_LEN:
            if current:
                parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line

    if current:
        parts.append(current)

    for i, part in enumerate(parts):
        if i > 0:
            await asyncio.sleep(0.3)
        await bot.send_message(chat_id, part, **kwargs)


# ---------- Background watcher ----------

async def background_watcher():
    """Periodically check for new Antigravity messages even when user
    works on the desktop.  Runs as a long-lived asyncio task.

    Logic:
    - Every BG_WATCH_INTERVAL seconds, fetch the snapshot hash.
    - If hash changed AND active polling is not running, fetch messages
      and forward any truly new ones to Telegram.
    - Skips cycles while poll_for_response is active (to avoid duplicates).
    """
    global _bg_watcher_running, _bg_last_hash, _bg_baseline_hashes

    if _bg_watcher_running:
        return
    _bg_watcher_running = True

    write_log("BG_WATCHER", "Background watcher started")

    # Wait a bit on startup for phone_worker to be reachable
    await asyncio.sleep(5)

    try:
        while True:
            await asyncio.sleep(BG_WATCH_INTERVAL)

            # Skip if active polling is running (prompt-triggered)
            if _polling_active:
                continue

            # Skip if we don't know which chat to send to
            if _active_chat_id is None:
                continue

            try:
                current_hash = await get_snapshot_hash()
            except Exception:
                continue

            # No change — nothing to do
            if current_hash is None or current_hash == _bg_last_hash:
                continue

            _bg_last_hash = current_hash

            # Snapshot changed — check for new messages
            try:
                current_msgs = await get_current_messages()
            except Exception:
                continue

            if not current_msgs:
                continue

            new_msgs = [
                m for m in current_msgs
                if m["hash"] not in _bg_baseline_hashes
            ]

            if not new_msgs:
                # Hash changed but no new parseable messages (e.g. typing indicator)
                continue

            for msg in new_msgs:
                _bg_baseline_hashes.add(msg["hash"])

                # Skip user messages (we only forward assistant responses)
                if msg["role"] == "user":
                    _mark_sent(msg["text"])
                    continue

                text = msg["text"]

                # Skip if we already sent this content (dedup against polling)
                if _is_duplicate(text):
                    continue

                try:
                    await send_long_message(
                        _active_chat_id,
                        f"\U0001f4e1 \U0001f916 {text}",  # satellite emoji = from desktop
                    )
                    _mark_sent(text)
                    write_log("BG_MSG", text[:200])
                except Exception as e:
                    write_log("BG_SEND_ERROR", str(e))

    except asyncio.CancelledError:
        write_log("BG_WATCHER", "Background watcher stopped")
    except Exception as e:
        write_log("BG_WATCHER_ERROR", f"Unexpected error: {e}")
    finally:
        _bg_watcher_running = False


# ---------- Keyboards ----------

def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="Prompt")
    builder.button(text="Plan")
    builder.button(text="Task")
    builder.button(text="Walkthrough")
    builder.button(text="Mode/Model")
    builder.button(text="New Chat")
    builder.button(text="Stop")
    builder.button(text="Projects")
    builder.button(text="Brain Files")
    builder.button(text="Status")
    builder.button(text="Logs")
    builder.adjust(3, 3, 3, 2)
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def get_mode_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Fast", callback_data="mode_Fast")
    builder.button(text="Planning", callback_data="mode_Planning")
    builder.adjust(2)
    return builder.as_markup()


# ---------- Dynamic model list ----------

# Cache: {"models": [...], "fetched_at": float}
_model_cache: dict = {"models": [], "fetched_at": 0}
MODEL_CACHE_TTL = 300  # seconds (5 min)

# Fallback models used when dynamic fetch fails and cache is empty
FALLBACK_MODELS = [
    "Gemini 2.5 Flash",
    "Gemini 2.5 Pro",
    "Claude Sonnet 4",
    "Claude Opus 4",
]


async def fetch_available_models() -> list[str]:
    """Fetch available models from Phone Connect via phone_worker.
    Uses a cache to avoid opening the UI dropdown on every request."""
    now = time.time()
    if _model_cache["models"] and (now - _model_cache["fetched_at"]) < MODEL_CACHE_TTL:
        return _model_cache["models"]

    try:
        data = await pw_get("/models")
        models = data.get("models", [])
        if models:
            _model_cache["models"] = models
            _model_cache["fetched_at"] = now
            return models
    except Exception as e:
        write_log("WARN", f"Failed to fetch models: {e}")

    # Return cached models if available (even if stale), otherwise fallback
    if _model_cache["models"]:
        return _model_cache["models"]
    return FALLBACK_MODELS


async def get_model_keyboard():
    """Build model keyboard dynamically from Antigravity's available models."""
    models = await fetch_available_models()
    builder = InlineKeyboardBuilder()
    for model_name in models:
        # Callback data max 64 bytes; truncate if needed
        cb_data = f"model_{model_name}"[:64]
        builder.button(text=model_name, callback_data=cb_data)
    # Arrange: 2 per row
    builder.adjust(2)
    # Add a refresh button at the bottom
    builder.row(InlineKeyboardButton(text="Refresh list", callback_data="models_refresh"))
    return builder.as_markup()


def get_settings_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Change Mode", callback_data="settings_mode")
    builder.button(text="Change Model", callback_data="settings_model")
    builder.button(text="Current State", callback_data="settings_state")
    builder.adjust(2, 1)
    return builder.as_markup()


def get_projects_keyboard():
    """Build inline keyboard with project list and management actions."""
    projects = load_projects()
    builder = InlineKeyboardBuilder()

    for p in projects:
        marker = "\u2705 " if p.get("active") else ""
        label = f"{marker}{p['name']}"
        cb_data = f"proj_select_{p['name']}"[:64]
        builder.button(text=label, callback_data=cb_data)

    builder.adjust(1)  # One project per row

    # Action buttons at the bottom
    builder.row(
        InlineKeyboardButton(text="\u2795 Add project", callback_data="proj_add"),
        InlineKeyboardButton(text="\U0001f680 Launch", callback_data="proj_launch"),
    )
    builder.row(
        InlineKeyboardButton(text="\U0001f5d1 Remove", callback_data="proj_remove_menu"),
        InlineKeyboardButton(text="\U0001f504 Scan folder", callback_data="proj_scan"),
    )

    return builder.as_markup()


# ---------- Auth ----------

def is_allowed(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID


# ---------- Handlers ----------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    global _active_chat_id
    _active_chat_id = message.chat.id

    project = get_active_project()
    project_info = f"\nActive project: *{project['name']}*" if project else ""

    await message.answer(
        f"*Antigravity Assistant*\n{project_info}\n\n"
        "Send any text as a prompt to Antigravity AI.\n"
        "Use buttons for quick actions:\n\n"
        "*Prompt* — type and send prompt\n"
        "*Plan* — implementation plan\n"
        "*Task* — current task\n"
        "*Walkthrough* — walkthrough\n"
        "*Mode/Model* — switch mode or model\n"
        "*New Chat* — start new conversation\n"
        "*Stop* — stop current generation\n"
        "*Projects* — switch or launch a project\n"
        "*Brain Files* — latest session files\n"
        "*Status* — connectivity status",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await show_status(message)


@dp.message(F.text == "Status")
async def btn_status(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await show_status(message)


async def show_status(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    status_parts = []

    # Active project
    project = get_active_project()
    if project:
        status_parts.append(f"Project: {project['name']}")
        status_parts.append(f"   Path: {project['path']}")

    # Phone Worker health
    try:
        pw_health = await pw_get("/health")
        pc = pw_health.get("phone_connect", {})
        cdp_ok = pc.get("cdpConnected", False)
        connected_str = "Connected" if cdp_ok else "Disconnected"
        status_parts.append(f"Phone Connect: {connected_str}")
        if pc.get("uptime"):
            mins = int(pc["uptime"] / 60)
            status_parts.append(f"   Uptime: {mins} min")
    except Exception as e:
        status_parts.append(f"Phone Worker: error — {e}")

    # App State
    try:
        state = await pw_get("/app-state")
        mode = state.get("mode", "Unknown")
        model = state.get("model", "Unknown")
        status_parts.append(f"Mode: {mode}")
        status_parts.append(f"Model: {model}")
    except Exception:
        status_parts.append("Mode/Model: unavailable")

    # File Monitor health
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FILE_SERVICE_URL}/health") as resp:
                fm = await resp.json()
                brain_ok = "OK" if fm.get("brain_accessible") else "N/A"
                project_ok = "OK" if fm.get("project_accessible") else "N/A"
                status_parts.append(f"Brain dir: {brain_ok}")
                status_parts.append(f"Project dir: {project_ok}")
    except Exception:
        status_parts.append("File Monitor: unreachable")

    # Background watcher status
    watcher_status = "active" if _bg_watcher_running else "stopped"
    status_parts.append(f"Background watcher: {watcher_status}")

    await message.answer("\n".join(status_parts), reply_markup=get_main_menu())


# ----- PROMPT: any text that isn't a button -----

@dp.message(F.text == "Prompt")
async def btn_prompt(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "Send your next message — it will go directly to the Antigravity agent.",
        reply_markup=get_main_menu(),
    )


@dp.message(F.text == "New Chat")
async def btn_new_chat(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        result = await pw_post("/new-chat")
        if result.get("success") or result.get("method"):
            global _known_message_hashes, _last_snapshot_hash
            global _bg_last_hash, _bg_baseline_hashes
            _known_message_hashes.clear()
            _last_snapshot_hash = None
            _bg_last_hash = None
            _bg_baseline_hashes.clear()
            _reset_sent_texts()
            current_session["thread_id"] = str(int(time.time()))
            save_session(current_session)
            write_log("SYSTEM", "New chat started via Telegram.")
            await message.answer("New chat started in Antigravity.", reply_markup=get_main_menu())
        else:
            await message.answer(f"Could not start new chat: {result}", reply_markup=get_main_menu())
    except Exception as e:
        await message.answer(f"Error: {e}", reply_markup=get_main_menu())


@dp.message(F.text == "Stop")
async def btn_stop(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    try:
        result = await pw_post("/stop")
        await message.answer("Stop signal sent to Antigravity.", reply_markup=get_main_menu())
    except Exception as e:
        await message.answer(f"Error: {e}", reply_markup=get_main_menu())


@dp.message(F.text == "Mode/Model")
async def btn_settings(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer("Settings:", reply_markup=get_settings_keyboard())


@dp.callback_query(F.data == "settings_mode")
async def cb_settings_mode(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id):
        return
    await callback.message.answer("Choose mode:", reply_markup=get_mode_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "settings_model")
async def cb_settings_model(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id):
        return
    await callback.message.answer("Loading models...", reply_markup=get_main_menu())
    keyboard = await get_model_keyboard()
    await callback.message.answer("Choose model:", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "models_refresh")
async def cb_models_refresh(callback: types.CallbackQuery):
    """Force-refresh the model list (invalidate cache)."""
    if not is_allowed(callback.from_user.id):
        return
    _model_cache["fetched_at"] = 0  # Invalidate cache
    await callback.message.answer("Refreshing models...", reply_markup=get_main_menu())
    keyboard = await get_model_keyboard()
    await callback.message.answer("Choose model:", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "settings_state")
async def cb_settings_state(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id):
        return
    try:
        state = await pw_get("/app-state")
        mode = state.get("mode", "Unknown")
        model = state.get("model", "Unknown")
        await callback.message.answer(f"Mode: {mode}\nModel: {model}")
    except Exception as e:
        await callback.message.answer(f"Error: {e}")
    await callback.answer()


@dp.callback_query(F.data.startswith("mode_"))
async def cb_set_mode(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id):
        return
    mode = callback.data.replace("mode_", "")
    try:
        result = await pw_post("/set-mode", {"mode": mode})
        if result.get("success") or result.get("alreadySet"):
            await callback.message.answer(f"Mode set to: {mode}", reply_markup=get_main_menu())
        else:
            await callback.message.answer(f"Error: {result.get('error', 'Unknown error')}", reply_markup=get_main_menu())
    except Exception as e:
        await callback.message.answer(f"Error: {e}", reply_markup=get_main_menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("model_"))
async def cb_set_model(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id):
        return
    model = callback.data.replace("model_", "")
    try:
        result = await pw_post("/set-model", {"model": model})
        if result.get("success"):
            await callback.message.answer(f"Model set to: {model}", reply_markup=get_main_menu())
        else:
            await callback.message.answer(f"Error: {result.get('error', 'Unknown error')}", reply_markup=get_main_menu())
    except Exception as e:
        await callback.message.answer(f"Error: {e}", reply_markup=get_main_menu())
    await callback.answer()


# ----- Projects management -----

@dp.message(F.text == "Projects")
async def btn_projects(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    project = get_active_project()
    active_name = project["name"] if project else "none"
    projects = load_projects()

    text = (
        f"*Projects* ({len(projects)} total)\n"
        f"Active: *{active_name}*\n\n"
        "Tap a project to select it.\n"
        "Then tap Launch to restart Antigravity with it."
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=get_projects_keyboard())


@dp.callback_query(F.data.startswith("proj_select_"))
async def cb_project_select(callback: types.CallbackQuery):
    """Select a project as active."""
    if not is_allowed(callback.from_user.id):
        return

    name = callback.data.replace("proj_select_", "")
    project = set_active_project(name)

    if project:
        await callback.message.edit_text(
            f"Active project set to: *{name}*\n"
            f"Path: `{project['path']}`\n\n"
            "Press Launch to restart Antigravity with this project.",
            parse_mode="Markdown",
            reply_markup=get_projects_keyboard(),
        )
    else:
        await callback.message.answer(f"Project not found: {name}")
    await callback.answer()


@dp.callback_query(F.data == "proj_launch")
async def cb_project_launch(callback: types.CallbackQuery):
    """Restart the whole service to launch Antigravity with the active project."""
    if not is_allowed(callback.from_user.id):
        return

    project = get_active_project()
    if not project:
        await callback.message.answer("No active project selected.")
        await callback.answer()
        return

    project_path = Path(project["path"])
    if not project_path.exists():
        await callback.message.answer(
            f"Project directory does not exist:\n`{project['path']}`\n\n"
            "Create the directory first or choose another project.",
            parse_mode="Markdown",
        )
        await callback.answer()
        return

    await callback.message.answer(
        f"Restarting Antigravity with project: *{project['name']}*\n"
        "The bot will be back in ~15 seconds...",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )
    await callback.answer()

    write_log("PROJECT", f"Launching project: {project['name']} at {project['path']}")

    # Restart the systemd service — this will kill this bot process too
    ok, msg = await restart_service()
    if not ok:
        # If restart failed, we're still alive — tell the user
        await callback.message.answer(
            f"Could not restart service: {msg}\n\n"
            "You can restart manually:\n"
            f"`systemctl --user restart {SYSTEMD_SERVICE_NAME}`",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )


@dp.callback_query(F.data == "proj_add")
async def cb_project_add(callback: types.CallbackQuery, state: FSMContext):
    """Ask user to type a project name."""
    if not is_allowed(callback.from_user.id):
        return

    await callback.message.answer(
        f"Enter project name (directory name inside `{PROJECTS_BASE_DIR}`):\n\n"
        "For example: `my-new-project`\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )
    await state.set_state(AddProjectStates.waiting_for_name)
    await callback.answer()


@dp.message(AddProjectStates.waiting_for_name, Command("cancel"))
async def cancel_add_project(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.", reply_markup=get_main_menu())


@dp.message(AddProjectStates.waiting_for_name)
async def process_new_project_name(message: types.Message, state: FSMContext):
    """User typed a project name — add it to the list."""
    if not is_allowed(message.from_user.id):
        return

    name = message.text.strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        await message.answer(
            "Invalid project name. Use a simple directory name like `my-project`.",
            parse_mode="Markdown",
        )
        return

    project = add_project(name)
    project_path = Path(project["path"])
    exists = project_path.exists()

    status = "exists on disk" if exists else "will be created by Antigravity on first run"

    await state.clear()
    await message.answer(
        f"Project added: *{name}*\n"
        f"Path: `{project['path']}`\n"
        f"Status: {status}\n\n"
        "Open Projects to select and launch it.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )
    write_log("PROJECT", f"Added project: {name}")


@dp.callback_query(F.data == "proj_remove_menu")
async def cb_project_remove_menu(callback: types.CallbackQuery):
    """Show a list of projects to remove."""
    if not is_allowed(callback.from_user.id):
        return

    projects = load_projects()
    if len(projects) <= 1:
        await callback.message.answer("Cannot remove the last project.")
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for p in projects:
        cb_data = f"proj_rm_{p['name']}"[:64]
        builder.button(text=f"\u274c {p['name']}", callback_data=cb_data)
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="Cancel", callback_data="proj_rm_cancel"))

    await callback.message.answer(
        "Select project to remove from the list\n(files will NOT be deleted):",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("proj_rm_"))
async def cb_project_remove(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id):
        return

    if callback.data == "proj_rm_cancel":
        await callback.message.delete()
        await callback.answer()
        return

    name = callback.data.replace("proj_rm_", "")
    removed = remove_project(name)

    if removed:
        await callback.message.edit_text(
            f"Project removed from list: {name}\n(files not deleted)",
        )
        write_log("PROJECT", f"Removed project from list: {name}")
    else:
        await callback.message.edit_text(f"Project not found: {name}")
    await callback.answer()


@dp.callback_query(F.data == "proj_scan")
async def cb_project_scan(callback: types.CallbackQuery):
    """Scan PROJECTS_BASE_DIR for project folders and add any missing ones."""
    if not is_allowed(callback.from_user.id):
        return

    if not PROJECTS_BASE_DIR.exists():
        await callback.message.answer(
            f"Projects base directory not found:\n`{PROJECTS_BASE_DIR}`",
            parse_mode="Markdown",
        )
        await callback.answer()
        return

    projects = load_projects()
    existing_paths = {p["path"] for p in projects}
    added = []

    for d in sorted(PROJECTS_BASE_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and str(d) not in existing_paths:
            projects.append({
                "name": d.name,
                "path": str(d),
                "active": False,
            })
            added.append(d.name)

    if added:
        save_projects(projects)
        await callback.message.answer(
            f"Found {len(added)} new project(s):\n" + "\n".join(f"  {n}" for n in added),
            reply_markup=get_projects_keyboard(),
        )
        write_log("PROJECT", f"Scanned and added: {', '.join(added)}")
    else:
        await callback.message.answer("No new project directories found.")

    await callback.answer()


# ----- Brain artifact buttons -----

@dp.message(F.text == "Plan")
async def btn_plan(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await fetch_and_send_artifact(message, "/latest/plan", "implementation_plan.md", "Plan")


@dp.message(F.text == "Task")
async def btn_task(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await fetch_and_send_artifact(message, "/latest/task", "task.md", "Task")


@dp.message(F.text == "Walkthrough")
async def btn_walkthrough(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await fetch_and_send_artifact(message, "/latest/walkthrough", "walkthrough.md", "Walkthrough")


async def fetch_and_send_artifact(message: types.Message, endpoint: str, filename: str, label: str):
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        content = await fm_get(endpoint)
    except Exception as e:
        await message.answer(f"Failed to get {filename}: {e}", reply_markup=get_main_menu())
        return

    write_log("ARTIFACT_VIEW", f"User requested {filename}.")

    if len(content) < 3500:
        await message.answer(
            f"*{label} — {filename}*:\n\n```\n{content}\n```",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )
    else:
        tmp_path = ARTIFACTS_DIR / filename
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        await message.answer_document(
            FSInputFile(tmp_path),
            caption=f"{label} — {filename}",
            reply_markup=get_main_menu(),
        )


@dp.message(F.text == "Brain Files")
async def btn_brain_files(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FILE_SERVICE_URL}/latest/files") as resp:
                data = await resp.json()
                files = data.get("files", [])

        if not files:
            await message.answer("No brain files found.", reply_markup=get_main_menu())
            return

        lines = ["*Latest brain session files:*\n"]
        builder = InlineKeyboardBuilder()
        for f in files[:20]:
            size_kb = f["size"] / 1024
            lines.append(f"  `{f['name']}` ({size_kb:.1f} KB)")
            builder.button(
                text=f["name"],
                callback_data=f"brainfile_{f['name'][:40]}",
            )
        builder.adjust(2)

        await message.answer(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=builder.as_markup(),
        )
    except Exception as e:
        await message.answer(f"Error: {e}", reply_markup=get_main_menu())


@dp.callback_query(F.data.startswith("brainfile_"))
async def cb_brain_file(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id):
        return
    filename = callback.data.replace("brainfile_", "")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FILE_SERVICE_URL}/latest/file?name={filename}") as resp:
                if resp.status == 200:
                    content = await resp.text()
                    if len(content) < 3500:
                        await callback.message.answer(
                            f"*{filename}*:\n\n```\n{content}\n```",
                            parse_mode="Markdown",
                        )
                    else:
                        tmp = ARTIFACTS_DIR / filename
                        with open(tmp, "w", encoding="utf-8") as f:
                            f.write(content)
                        await callback.message.answer_document(
                            FSInputFile(tmp),
                            caption=filename,
                        )
                else:
                    await callback.message.answer(f"File not found: {filename}")
    except Exception as e:
        await callback.message.answer(f"Error: {e}")
    await callback.answer()


@dp.message(F.text == "Logs")
async def btn_logs(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    if not LOG_FILE.exists():
        await message.answer("Log file is empty.", reply_markup=get_main_menu())
        return
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()[-30:]
    log_text = "".join(lines)
    await message.answer(
        f"*Last logs:*\n```\n{log_text}\n```",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


# ----- Default: treat any other text as a prompt -----

@dp.message(F.text)
async def handle_prompt(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    global _active_chat_id
    _active_chat_id = message.chat.id

    text = message.text.strip()
    if not text:
        return

    # Skip button texts
    button_texts = {
        "Prompt", "Plan", "Task", "Walkthrough",
        "Mode/Model", "New Chat", "Stop", "Logs",
        "Brain Files", "Status", "Projects",
    }
    if text in button_texts:
        return

    write_log("USER", text[:500])

    # Send typing indicator
    await bot.send_chat_action(message.chat.id, "typing")

    # Send the message to Antigravity via Phone Worker
    try:
        result = await pw_post("/send_message", {"text": text})
        if result.get("status") == "ok":
            status_msg = await message.answer(
                "Prompt sent. Waiting for AI response...",
                reply_markup=get_main_menu(),
            )
            # Start polling for response in the background
            asyncio.create_task(poll_for_response(message.chat.id))
        else:
            await message.answer(
                f"Failed to send: {result}",
                reply_markup=get_main_menu(),
            )
    except Exception as e:
        write_log("SEND_ERROR", str(e))
        await message.answer(
            f"Could not send message: {e}",
            reply_markup=get_main_menu(),
        )


# ---------- Main ----------

async def main():
    print("Telegram bot started. Waiting for updates...")

    # Start the background watcher alongside the bot polling
    asyncio.create_task(background_watcher())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

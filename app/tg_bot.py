"""
tg_bot.py — Telegram bot for full bidirectional communication
with Antigravity AI agent via Phone Connect.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

ARTIFACTS_DIR = BASE_DIR / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
except ValueError:
    ALLOWED_USER_ID = 0

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")
if not ALLOWED_USER_ID:
    raise RuntimeError("ALLOWED_USER_ID is not set or invalid in .env")

SESSION_FILE = BASE_DIR / "session.json"
PROJECTS_FILE = BASE_DIR / "projects.json"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "agent.log"

FILE_SERVICE_URL = os.getenv("FILE_SERVICE_URL", "http://127.0.0.1:8787").strip()
PHONE_WORKER_URL = os.getenv("PHONE_WORKER_URL", "http://127.0.0.1:8788").strip()

PROJECTS_BASE_DIR = Path(
    os.getenv("PROJECTS_BASE_DIR", str(Path.home() / "antigravity" / "projects"))
)

SYSTEMD_SERVICE_NAME = os.getenv("SYSTEMD_SERVICE_NAME", "antigravity-assistant")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3.0"))
POLL_TIMEOUT = float(os.getenv("POLL_TIMEOUT", "600"))
BG_WATCH_INTERVAL = float(os.getenv("BG_WATCH_INTERVAL", "10.0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

_known_message_hashes: set[str] = set()
_polling_active = False
_last_snapshot_hash: Optional[str] = None
_waiting_for_response = False

_bg_watcher_running = False
_bg_last_hash: Optional[str] = None
_bg_baseline_hashes: set[str] = set()
_active_chat_id: Optional[int] = None

class AddProjectStates(StatesGroup):
    waiting_for_name = State()

def load_session() -> dict:
    if SESSION_FILE.exists():
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"thread_id": str(int(time.time()))}

def save_session(session_data: dict):
    session_data["last_updated"] = datetime.now().isoformat()
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)

def load_projects() -> list[dict]:
    if PROJECTS_FILE.exists():
        try:
            with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
                projects = json.load(f)
            if isinstance(projects, list) and projects:
                return projects
        except (json.JSONDecodeError, ValueError):
            pass

    default_path = os.getenv("ANTIGRAVITY_PROJECT_DIR", "")
    if default_path:
        projects = [{"name": Path(default_path).name, "path": default_path, "active": True}]
    else:
        projects = []

    if PROJECTS_BASE_DIR.exists():
        for d in sorted(PROJECTS_BASE_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                already = any(p["path"] == str(d) for p in projects)
                if not already:
                    projects.append({"name": d.name, "path": str(d), "active": len(projects) == 0})
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
    projects = load_projects()
    for p in projects:
        if p["name"] == name:
            return p
    project_path = str(PROJECTS_BASE_DIR / name)
    new_project = {"name": name, "path": project_path, "active": False}
    projects.append(new_project)
    save_projects(projects)
    return new_project

def remove_project(name: str) -> bool:
    projects = load_projects()
    original_len = len(projects)
    projects = [p for p in projects if p["name"] != name]
    if len(projects) < original_len:
        if projects and not any(p.get("active") for p in projects):
            projects[0]["active"] = True
        save_projects(projects)
        return True
    return False

current_session = load_session()

def write_log(sender: str, message: str):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%H:%M:%S")
        f.write(f"[{timestamp}] {sender}: {message}\n")

async def pw_get(path: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PHONE_WORKER_URL}{path}", timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.json()

async def pw_post(path: str, data: dict = None) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{PHONE_WORKER_URL}{path}", json=data or {}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.json()

async def fm_get(path: str) -> str:
    url = f"{FILE_SERVICE_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            if resp.status == 200:
                return text
            raise RuntimeError(f"HTTP {resp.status}: {text}")

async def restart_service():
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", SYSTEMD_SERVICE_NAME,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, "Service restarted successfully."
        else:
            err = stderr.decode().strip() or stdout.decode().strip()
            return False, f"Restart failed: {err}"
    except Exception as e:
        return False, f"Restart error: {e}"

_sent_text_prefixes: dict[str, str] = {}

def _text_prefix(text: str, length: int = 120) -> str:
    return text[:length].strip()

def _is_duplicate(text: str) -> bool:
    prefix = _text_prefix(text)
    if not prefix: return True
    if prefix in _sent_text_prefixes: return True
    for sent_prefix, sent_full in _sent_text_prefixes.items():
        if text in sent_full: return True
        if sent_full in text:
            _sent_text_prefixes[sent_prefix] = text
            return True
    return False

def _mark_sent(text: str):
    prefix = _text_prefix(text)
    if prefix: _sent_text_prefixes[prefix] = text

def _reset_sent_texts():
    _sent_text_prefixes.clear()

async def get_current_messages() -> list[dict]:
    try:
        data = await pw_get("/snapshot/text")
        return data.get("messages", [])
    except Exception as e:
        write_log("ERROR", f"Failed to get snapshot: {e}")
        return []

async def get_snapshot_hash() -> Optional[str]:
    try:
        data = await pw_get("/snapshot")
        html = data.get("html", "")
        if html: return hashlib.md5(html.encode()).hexdigest()
    except Exception:
        pass
    return None

async def poll_for_response(chat_id: int, timeout: float = None):
    global _polling_active, _last_snapshot_hash, _waiting_for_response
    global _bg_last_hash, _bg_baseline_hashes

    if _polling_active: return

    _polling_active = True
    _waiting_for_response = True
    effective_timeout = timeout or POLL_TIMEOUT
    start_time = time.time()

    _last_snapshot_hash = await get_snapshot_hash()
    baseline = await get_current_messages()
    baseline_hashes = {m["hash"] for m in baseline}
    for m in baseline: _mark_sent(m["text"])

    prev_hash = _last_snapshot_hash
    ever_sent = False
    stable_polls = 0

    try:
        while time.time() - start_time < effective_timeout:
            await asyncio.sleep(POLL_INTERVAL)
            current_hash = await get_snapshot_hash()

            if current_hash == prev_hash:
                stable_polls += 1
                if ever_sent and stable_polls >= 10: break
                if not ever_sent and stable_polls >= 50: break
                continue

            stable_polls = 0
            prev_hash = current_hash
            current_msgs = await get_current_messages()
            new_msgs = [m for m in current_msgs if m["hash"] not in baseline_hashes]

            for msg in new_msgs:
                baseline_hashes.add(msg["hash"])
                if msg["role"] == "user":
                    _mark_sent(msg["text"])
                    continue
                text = msg["text"]
                if _is_duplicate(text): continue
                await send_long_message(chat_id, f"🤖 {text}")
                _mark_sent(text)
                write_log("AI_RESPONSE", text[:200])
                ever_sent = True

        try:
            final_msgs = await get_current_messages()
            for msg in final_msgs:
                if msg["hash"] in baseline_hashes: continue
                baseline_hashes.add(msg["hash"])
                if msg["role"] == "user":
                    _mark_sent(msg["text"])
                    continue
                text = msg["text"]
                if _is_duplicate(text): continue
                await send_long_message(chat_id, f"🤖 {text}")
                _mark_sent(text)
                write_log("AI_RESPONSE_FINAL", text[:200])
                ever_sent = True
        except Exception:
            pass

        if not ever_sent:
            write_log("POLL_TIMEOUT", "No AI response received.")
    except Exception as e:
        write_log("POLL_ERROR", str(e))
        await bot.send_message(chat_id, f"Polling error: {e}")
    finally:
        _polling_active = False
        _waiting_for_response = False
        _bg_last_hash = prev_hash
        _bg_baseline_hashes = baseline_hashes

async def send_long_message(chat_id: int, text: str, **kwargs):
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await bot.send_message(chat_id, text, **kwargs)
        return
    parts = []
    current = ""
    for line in text.split('\n'):
        if len(current) + len(line) + 1 > MAX_LEN:
            if current: parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current: parts.append(current)

    for i, part in enumerate(parts):
        if i > 0: await asyncio.sleep(0.3)
        await bot.send_message(chat_id, part, **kwargs)

async def background_watcher():
    global _bg_watcher_running, _bg_last_hash, _bg_baseline_hashes
    if _bg_watcher_running: return
    _bg_watcher_running = True
    write_log("BG_WATCHER", "Background watcher started.")
    await asyncio.sleep(5)

    try:
        while True:
            await asyncio.sleep(BG_WATCH_INTERVAL)
            if _polling_active or _active_chat_id is None: continue
            try:
                current_hash = await get_snapshot_hash()
            except Exception: continue

            if current_hash is None or current_hash == _bg_last_hash: continue
            _bg_last_hash = current_hash

            try:
                current_msgs = await get_current_messages()
            except Exception: continue

            if not current_msgs: continue
            new_msgs = [m for m in current_msgs if m["hash"] not in _bg_baseline_hashes]
            if not new_msgs: continue

            for msg in new_msgs:
                _bg_baseline_hashes.add(msg["hash"])
                if msg["role"] == "user":
                    _mark_sent(msg["text"])
                    continue
                text = msg["text"]
                if _is_duplicate(text): continue

                try:
                    await send_long_message(_active_chat_id, f"📡 🤖 {text}")
                    _mark_sent(text)
                    write_log("BG_MSG", text[:200])
                except Exception as e:
                    write_log("BG_SEND_ERROR", str(e))
    except asyncio.CancelledError:
        write_log("BG_WATCHER", "Background watcher stopped.")
    except Exception as e:
        write_log("BG_WATCHER_ERROR", f"Unexpected error: {e}")
    finally:
        _bg_watcher_running = False

def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="Prompt")
    builder.button(text="Refresh")
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
    builder.adjust(3, 3, 3, 3)
    return builder.as_markup(resize_keyboard=True, is_persistent=True)

def get_mode_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Fast", callback_data="mode_Fast")
    builder.button(text="Planning", callback_data="mode_Planning")
    builder.adjust(2)
    return builder.as_markup()

_model_cache: dict = {"models": [], "fetched_at": 0}
MODEL_CACHE_TTL = 300
FALLBACK_MODELS = ["Gemini 2.5 Flash", "Gemini 2.5 Pro", "Claude Sonnet 4", "Claude Opus 4"]

async def fetch_available_models() -> list[str]:
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
    if _model_cache["models"]: return _model_cache["models"]
    return FALLBACK_MODELS

async def get_model_keyboard():
    models = await fetch_available_models()
    builder = InlineKeyboardBuilder()
    for model_name in models:
        builder.button(text=model_name, callback_data=f"model_{model_name}"[:64])
    builder.adjust(2)
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
    projects = load_projects()
    builder = InlineKeyboardBuilder()
    for p in projects:
        marker = "✅ " if p.get("active") else ""
        builder.button(text=f"{marker}{p['name']}", callback_data=f"proj_select_{p['name']}"[:64])
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="➕ Add project", callback_data="proj_add"),
        InlineKeyboardButton(text="🚀 Launch", callback_data="proj_launch"),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Remove", callback_data="proj_remove_menu"),
        InlineKeyboardButton(text="🔄 Scan folder", callback_data="proj_scan"),
    )
    return builder.as_markup()

def is_allowed(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_allowed(message.from_user.id): return
    global _active_chat_id
    _active_chat_id = message.chat.id
    project = get_active_project()
    project_info = f"\nActive project: *{project['name']}*" if project else ""
    await message.answer(
        f"*Antigravity Assistant*\n{project_info}\n\n"
        "Send any text as a prompt to the Antigravity AI agent.\n"
        "Use the keyboard buttons for quick actions:\n\n"
        "*Prompt* — Prepare to send a prompt\n"
        "*Refresh* — Instantly fetch the latest agent responses\n"
        "*Plan* — View the implementation plan\n"
        "*Task* — View the current task\n"
        "*Walkthrough* — View the walkthrough artifact\n"
        "*Mode/Model* — Switch the agent's mode or model\n"
        "*New Chat* — Start a new conversation context\n"
        "*Stop* — Stop the current generation\n"
        "*Projects* — Switch or launch a workspace\n"
        "*Brain Files* — Access the latest session files\n"
        "*Status* — View connectivity and system status",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await show_status(message)

@dp.message(F.text == "Status")
async def btn_status(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await show_status(message)

async def show_status(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    status_parts = []
    project = get_active_project()
    if project:
        status_parts.append(f"Project: {project['name']}")
        status_parts.append(f"   Path: {project['path']}")

    try:
        pw_health = await pw_get("/health")
        pc = pw_health.get("phone_connect", {})
        cdp_ok = pc.get("cdpConnected", False)
        status_parts.append(f"Phone Connect: {'Connected' if cdp_ok else 'Disconnected'}")
        if pc.get("uptime"): status_parts.append(f"   Uptime: {int(pc['uptime'] / 60)} min")
    except Exception as e:
        status_parts.append(f"Phone Worker: error — {e}")

    try:
        state = await pw_get("/app-state")
        status_parts.append(f"Mode: {state.get('mode', 'Unknown')}")
        status_parts.append(f"Model: {state.get('model', 'Unknown')}")
    except Exception:
        status_parts.append("Mode/Model: unavailable")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FILE_SERVICE_URL}/health") as resp:
                fm = await resp.json()
                status_parts.append(f"Brain dir: {'OK' if fm.get('brain_accessible') else 'N/A'}")
                status_parts.append(f"Project dir: {'OK' if fm.get('project_accessible') else 'N/A'}")
    except Exception:
        status_parts.append("File Monitor: unreachable")

    status_parts.append(f"Background watcher: {'active' if _bg_watcher_running else 'stopped'}")
    await message.answer("\n".join(status_parts), reply_markup=get_main_menu())

@dp.message(F.text == "Refresh")
async def btn_refresh(message: types.Message):
    if not is_allowed(message.from_user.id): return
    global _bg_baseline_hashes, _bg_last_hash, _active_chat_id
    _active_chat_id = message.chat.id
    await bot.send_chat_action(message.chat.id, "typing")
    
    try:
        current_hash = await get_snapshot_hash()
        current_msgs = await get_current_messages()
        
        if not current_msgs:
            await message.answer("The chat is empty or currently inaccessible.", reply_markup=get_main_menu())
            return

        new_msgs = [m for m in current_msgs if m["hash"] not in _bg_baseline_hashes]
        if not new_msgs:
            await message.answer("🔄 No new messages to pull.", reply_markup=get_main_menu())
            return

        _bg_last_hash = current_hash
        sent_count = 0
        for msg in new_msgs:
            _bg_baseline_hashes.add(msg["hash"])
            if msg["role"] == "user":
                _mark_sent(msg["text"])
                continue
            text = msg["text"]
            if _is_duplicate(text): continue
            await send_long_message(message.chat.id, f"📥 {text}")
            _mark_sent(text)
            sent_count += 1
            
        if sent_count == 0:
            await message.answer("🔄 Only internal agent reasoning was found. No significant output yet.", reply_markup=get_main_menu())
    except Exception as e:
        write_log("REFRESH_ERROR", str(e))
        await message.answer(f"Error during refresh: {e}", reply_markup=get_main_menu())

@dp.message(F.text == "Prompt")
async def btn_prompt(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await message.answer("Send your next message — it will be routed directly to the Antigravity agent.", reply_markup=get_main_menu())

@dp.message(F.text == "New Chat")
async def btn_new_chat(message: types.Message):
    if not is_allowed(message.from_user.id): return
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
            await message.answer("A new chat context has been initiated in Antigravity.", reply_markup=get_main_menu())
        else:
            await message.answer(f"Failed to start a new chat: {result}", reply_markup=get_main_menu())
    except Exception as e:
        await message.answer(f"Error: {e}", reply_markup=get_main_menu())

@dp.message(F.text == "Stop")
async def btn_stop(message: types.Message):
    if not is_allowed(message.from_user.id): return
    try:
        await pw_post("/stop")
        await message.answer("Stop signal dispatched to Antigravity.", reply_markup=get_main_menu())
    except Exception as e:
        await message.answer(f"Error: {e}", reply_markup=get_main_menu())

@dp.message(F.text == "Mode/Model")
async def btn_settings(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await message.answer("Settings Configuration:", reply_markup=get_settings_keyboard())

@dp.callback_query(F.data == "settings_mode")
async def cb_settings_mode(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    await callback.message.answer("Select the operation mode:", reply_markup=get_mode_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "settings_model")
async def cb_settings_model(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    await callback.message.answer("Loading available models...", reply_markup=get_main_menu())
    keyboard = await get_model_keyboard()
    await callback.message.answer("Select the target model:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "models_refresh")
async def cb_models_refresh(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    _model_cache["fetched_at"] = 0
    await callback.message.answer("Refreshing model list...", reply_markup=get_main_menu())
    keyboard = await get_model_keyboard()
    await callback.message.answer("Select the target model:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "settings_state")
async def cb_settings_state(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    try:
        state = await pw_get("/app-state")
        await callback.message.answer(f"Current Mode: {state.get('mode', 'Unknown')}\nCurrent Model: {state.get('model', 'Unknown')}")
    except Exception as e:
        await callback.message.answer(f"Error retrieving state: {e}")
    await callback.answer()

@dp.callback_query(F.data.startswith("mode_"))
async def cb_set_mode(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    mode = callback.data.replace("mode_", "")
    try:
        result = await pw_post("/set-mode", {"mode": mode})
        if result.get("success") or result.get("alreadySet"):
            await callback.message.answer(f"Operation mode updated to: {mode}", reply_markup=get_main_menu())
        else:
            await callback.message.answer(f"Error: {result.get('error', 'Unknown error')}", reply_markup=get_main_menu())
    except Exception as e:
        await callback.message.answer(f"Error executing request: {e}", reply_markup=get_main_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("model_"))
async def cb_set_model(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    model = callback.data.replace("model_", "")
    try:
        result = await pw_post("/set-model", {"model": model})
        if result.get("success"):
            await callback.message.answer(f"Active model updated to: {model}", reply_markup=get_main_menu())
        else:
            await callback.message.answer(f"Error updating model: {result.get('error', 'Unknown error')}", reply_markup=get_main_menu())
    except Exception as e:
        await callback.message.answer(f"Error executing request: {e}", reply_markup=get_main_menu())
    await callback.answer()

@dp.message(F.text == "Projects")
async def btn_projects(message: types.Message):
    if not is_allowed(message.from_user.id): return
    project = get_active_project()
    active_name = project["name"] if project else "none"
    projects = load_projects()
    text = (
        f"*Workspace Projects* ({len(projects)} total)\n"
        f"Active Workspace: *{active_name}*\n\n"
        "Tap a project to set it as active.\n"
        "Then tap Launch to restart Antigravity with the selected workspace."
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=get_projects_keyboard())

@dp.callback_query(F.data.startswith("proj_select_"))
async def cb_project_select(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    name = callback.data.replace("proj_select_", "")
    project = set_active_project(name)
    if project:
        await callback.message.edit_text(
            f"Active project updated to: *{name}*\nPath: `{project['path']}`\n\nPress Launch to restart.",
            parse_mode="Markdown", reply_markup=get_projects_keyboard()
        )
    else:
        await callback.message.answer(f"Project identifier not found: {name}")
    await callback.answer()

@dp.callback_query(F.data == "proj_launch")
async def cb_project_launch(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    project = get_active_project()
    if not project:
        await callback.message.answer("No active project is currently selected.")
        await callback.answer()
        return
    if not Path(project["path"]).exists():
        await callback.message.answer(f"The specified project directory does not exist:\n`{project['path']}`", parse_mode="Markdown")
        await callback.answer()
        return
    await callback.message.answer(f"Initiating Antigravity restart with workspace: *{project['name']}*...", parse_mode="Markdown", reply_markup=get_main_menu())
    await callback.answer()
    write_log("PROJECT", f"Launching workspace environment: {project['name']} at {project['path']}")
    ok, msg = await restart_service()
    if not ok:
        await callback.message.answer(f"Service restart sequence failed: {msg}\n\nManual restart required.", parse_mode="Markdown", reply_markup=get_main_menu())

@dp.callback_query(F.data == "proj_add")
async def cb_project_add(callback: types.CallbackQuery, state: FSMContext):
    if not is_allowed(callback.from_user.id): return
    await callback.message.answer(
        f"Enter the new project name (directory inside `{PROJECTS_BASE_DIR}`):\n\nType /cancel to abort.",
        parse_mode="Markdown", reply_markup=get_main_menu()
    )
    await state.set_state(AddProjectStates.waiting_for_name)
    await callback.answer()

@dp.message(AddProjectStates.waiting_for_name, Command("cancel"))
async def cancel_add_project(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Project creation aborted.", reply_markup=get_main_menu())

@dp.message(AddProjectStates.waiting_for_name)
async def process_new_project_name(message: types.Message, state: FSMContext):
    if not is_allowed(message.from_user.id): return
    name = message.text.strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        await message.answer("Invalid project identifier.", parse_mode="Markdown")
        return
    project = add_project(name)
    exists = Path(project["path"]).exists()
    status = "exists on disk" if exists else "will be created"
    await state.clear()
    await message.answer(f"Project registered: *{name}*\nStatus: {status}\nNavigate to Projects to launch it.", parse_mode="Markdown", reply_markup=get_main_menu())
    write_log("PROJECT", f"Registered new workspace project: {name}")

@dp.callback_query(F.data == "proj_remove_menu")
async def cb_project_remove_menu(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    projects = load_projects()
    if len(projects) <= 1:
        await callback.message.answer("Operation denied: Cannot remove the last remaining project.")
        await callback.answer()
        return
    builder = InlineKeyboardBuilder()
    for p in projects:
        builder.button(text=f"❌ {p['name']}", callback_data=f"proj_rm_{p['name']}"[:64])
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="Cancel", callback_data="proj_rm_cancel"))
    await callback.message.answer("Select the project to unregister:", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("proj_rm_"))
async def cb_project_remove(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    if callback.data == "proj_rm_cancel":
        await callback.message.delete()
        await callback.answer()
        return
    name = callback.data.replace("proj_rm_", "")
    if remove_project(name):
        await callback.message.edit_text(f"Project successfully unregistered: {name}")
        write_log("PROJECT", f"Unregistered workspace project: {name}")
    else:
        await callback.message.edit_text(f"Error: Project identifier not found: {name}")
    await callback.answer()

@dp.callback_query(F.data == "proj_scan")
async def cb_project_scan(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    if not PROJECTS_BASE_DIR.exists():
        await callback.message.answer(f"Base workspace directory not found:\n`{PROJECTS_BASE_DIR}`", parse_mode="Markdown")
        await callback.answer()
        return
    projects = load_projects()
    existing_paths = {p["path"] for p in projects}
    added = []
    for d in sorted(PROJECTS_BASE_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and str(d) not in existing_paths:
            projects.append({"name": d.name, "path": str(d), "active": False})
            added.append(d.name)
    if added:
        save_projects(projects)
        await callback.message.answer(f"Discovered {len(added)} new directory(s):\n" + "\n".join(f"  {n}" for n in added), reply_markup=get_projects_keyboard())
        write_log("PROJECT", f"Automated scan registered: {', '.join(added)}")
    else:
        await callback.message.answer("Scan complete. No unindexed project directories found.")
    await callback.answer()

@dp.message(F.text == "Plan")
async def btn_plan(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await fetch_and_send_artifact(message, "/latest/plan", "implementation_plan.md", "Implementation Plan")

@dp.message(F.text == "Task")
async def btn_task(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await fetch_and_send_artifact(message, "/latest/task", "task.md", "Current Task Definition")

@dp.message(F.text == "Walkthrough")
async def btn_walkthrough(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await fetch_and_send_artifact(message, "/latest/walkthrough", "walkthrough.md", "Task Walkthrough")

async def fetch_and_send_artifact(message: types.Message, endpoint: str, filename: str, label: str):
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        content = await fm_get(endpoint)
    except Exception as e:
        await message.answer(f"Failed to fetch artifact '{filename}': {e}", reply_markup=get_main_menu())
        return
    write_log("ARTIFACT_VIEW", f"User retrieved artifact: {filename}.")
    if len(content) < 3500:
        await message.answer(f"*{label} — {filename}*:\n\n```\n{content}\n```", parse_mode="Markdown", reply_markup=get_main_menu())
    else:
        tmp_path = ARTIFACTS_DIR / filename
        with open(tmp_path, "w", encoding="utf-8") as f: f.write(content)
        await message.answer_document(FSInputFile(tmp_path), caption=f"{label} — {filename}", reply_markup=get_main_menu())

@dp.message(F.text == "Brain Files")
async def btn_brain_files(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FILE_SERVICE_URL}/latest/files") as resp:
                data = await resp.json()
                files = data.get("files", [])
        if not files:
            await message.answer("No session files found in the brain directory.", reply_markup=get_main_menu())
            return
        lines = ["*Latest brain session artifacts:*\n"]
        builder = InlineKeyboardBuilder()
        for f in files[:20]:
            lines.append(f"  `{f['name']}` ({(f['size'] / 1024):.1f} KB)")
            builder.button(text=f["name"], callback_data=f"brainfile_{f['name'][:40]}")
        builder.adjust(2)
        await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception as e:
        await message.answer(f"Error accessing brain files: {e}", reply_markup=get_main_menu())

@dp.callback_query(F.data.startswith("brainfile_"))
async def cb_brain_file(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id): return
    filename = callback.data.replace("brainfile_", "")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FILE_SERVICE_URL}/latest/file?name={filename}") as resp:
                if resp.status == 200:
                    content = await resp.text()
                    if len(content) < 3500:
                        await callback.message.answer(f"*{filename}*:\n\n```\n{content}\n```", parse_mode="Markdown")
                    else:
                        tmp = ARTIFACTS_DIR / filename
                        with open(tmp, "w", encoding="utf-8") as f: f.write(content)
                        await callback.message.answer_document(FSInputFile(tmp), caption=filename)
                else:
                    await callback.message.answer(f"Artifact not found on server: {filename}")
    except Exception as e:
        await callback.message.answer(f"Retrieval error: {e}")
    await callback.answer()

@dp.message(F.text == "Logs")
async def btn_logs(message: types.Message):
    if not is_allowed(message.from_user.id): return
    if not LOG_FILE.exists():
        await message.answer("The system log file is currently empty.", reply_markup=get_main_menu())
        return
    with open(LOG_FILE, "r", encoding="utf-8") as f: lines = f.readlines()[-30:]
    await message.answer(f"*Recent system logs:*\n```\n{''.join(lines)}\n```", parse_mode="Markdown", reply_markup=get_main_menu())

@dp.message(F.text)
async def handle_prompt(message: types.Message):
    if not is_allowed(message.from_user.id): return
    global _active_chat_id
    _active_chat_id = message.chat.id
    text = message.text.strip()
    if not text: return

    if text in {"Prompt", "Refresh", "Plan", "Task", "Walkthrough", "Mode/Model", "New Chat", "Stop", "Logs", "Brain Files", "Status", "Projects"}:
        return

    write_log("USER", text[:500])
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        result = await pw_post("/send_message", {"text": text})
        if result.get("status") == "ok":
            await message.answer("Prompt successfully dispatched. Awaiting agent response...", reply_markup=get_main_menu())
            asyncio.create_task(poll_for_response(message.chat.id))
        else:
            await message.answer(f"Dispatch failed: {result}", reply_markup=get_main_menu())
    except Exception as e:
        write_log("SEND_ERROR", str(e))
        await message.answer(f"Failed to establish connection: {e}", reply_markup=get_main_menu())

async def main():
    print("Telegram bridge initialized. Listening for updates...")
    asyncio.create_task(background_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
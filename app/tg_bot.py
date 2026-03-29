from __future__ import annotations

import asyncio
import json
import os
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, cast

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv

from app.logger import setup_logger
from app.state import init_db, get_val, set_val

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

init_db()

tg_log = setup_logger("tg_bot", "tg_bot.log")

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

_bg_baseline_hashes: set[str] = set(cast(list[str], get_val("bg_baseline_hashes", [])))

_active_chat_id: Optional[int] = None


def _persist_baseline() -> None:
    set_val("bg_baseline_hashes", list(_bg_baseline_hashes))


class AddProjectStates(StatesGroup):
    waiting_for_name = State()


def load_session() -> dict[str, Any]:
    if SESSION_FILE.exists():
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                return cast(dict[str, Any], json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
    return {"thread_id": str(int(time.time()))}


def save_session(session_data: dict[str, Any]) -> None:
    session_data["last_updated"] = datetime.now().isoformat()
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)


def load_projects() -> list[dict[str, Any]]:
    if PROJECTS_FILE.exists():
        try:
            with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
                projects = json.load(f)
            if isinstance(projects, list) and projects:
                return cast(list[dict[str, Any]], projects)
        except (json.JSONDecodeError, ValueError):
            pass

    default_path = os.getenv("ANTIGRAVITY_PROJECT_DIR", "")
    projects: list[dict[str, Any]] = []
    if default_path:
        projects.append({"name": Path(default_path).name, "path": default_path, "active": True})

    if PROJECTS_BASE_DIR.exists():
        for d in sorted(PROJECTS_BASE_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                already = any(p["path"] == str(d) for p in projects)
                if not already:
                    projects.append(
                        {"name": d.name, "path": str(d), "active": len(projects) == 0}
                    )
    save_projects(projects)
    return projects


def save_projects(projects: list[dict[str, Any]]) -> None:
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def get_active_project() -> Optional[dict[str, Any]]:
    projects = load_projects()
    for p in projects:
        if p.get("active"):
            return p
    return projects[0] if projects else None


def set_active_project(project_name: str) -> Optional[dict[str, Any]]:
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


def add_project(name: str) -> dict[str, Any]:
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


def write_log(sender: str, message: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%H:%M:%S")
        f.write(f"[{timestamp}] {sender}: {message}\n")


async def pw_get(path: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{PHONE_WORKER_URL}{path}",
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            res = await resp.json()
            return cast(dict[str, Any], res)


async def pw_post(path: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = data if data is not None else {}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{PHONE_WORKER_URL}{path}",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            res = await resp.json()
            return cast(dict[str, Any], res)


async def fm_get(path: str) -> str:
    url = f"{FILE_SERVICE_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            if resp.status == 200:
                return text
            raise RuntimeError(f"HTTP {resp.status}: {text}")


async def restart_service() -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "restart",
            SYSTEMD_SERVICE_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, "Service restarted."
        else:
            err = stderr.decode().strip() or stdout.decode().strip()
            return False, f"Error: {err}"
    except Exception as e:
        return False, f"Exception: {e}"


_sent_text_prefixes: dict[str, str] = {}


def _text_prefix(text: str, length: int = 120) -> str:
    return text[:length].strip()


def _is_duplicate(text: str) -> bool:
    prefix = _text_prefix(text)
    if not prefix:
        return True
    if prefix in _sent_text_prefixes:
        return True
    for sent_prefix, sent_full in _sent_text_prefixes.items():
        if text in sent_full or sent_full in text:
            _sent_text_prefixes[sent_prefix] = text if len(text) > len(sent_full) else sent_full
            return True
    return False


def _mark_sent(text: str) -> None:
    prefix = _text_prefix(text)
    if prefix:
        _sent_text_prefixes[prefix] = text


async def get_current_messages() -> list[dict[str, str]]:
    try:
        data = await pw_get("/snapshot/text")
        return cast(list[dict[str, str]], data.get("messages", []))
    except Exception:
        return []


async def get_snapshot_hash() -> Optional[str]:
    try:
        data = await pw_get("/snapshot/hash")
        return cast(Optional[str], data.get("hash"))
    except Exception:
        return None


async def poll_for_response(chat_id: int, timeout: Optional[float] = None) -> None:
    global _polling_active, _last_snapshot_hash, _waiting_for_response
    global _bg_last_hash, _bg_baseline_hashes

    if _polling_active:
        return

    _polling_active = True
    _waiting_for_response = True
    effective_timeout = timeout or POLL_TIMEOUT
    start_time = time.time()

    _last_snapshot_hash = await get_snapshot_hash()
    baseline = await get_current_messages()
    baseline_hashes = {m["hash"] for m in baseline}
    for m in baseline:
        _mark_sent(m["text"])

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
                ever_sent = True

    finally:
        _polling_active = False
        _waiting_for_response = False
        _bg_last_hash = prev_hash
        _bg_baseline_hashes = baseline_hashes
        _persist_baseline()


async def send_long_message(chat_id: int, text: str, **kwargs: Any) -> None:
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await bot.send_message(chat_id, text, **kwargs)
        return
    parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
    for part in parts:
        await bot.send_message(chat_id, part, **kwargs)
        await asyncio.sleep(0.3)


async def background_watcher() -> None:
    global _bg_watcher_running, _bg_last_hash, _bg_baseline_hashes
    if _bg_watcher_running: return
    _bg_watcher_running = True
    await asyncio.sleep(5)
    try:
        while True:
            await asyncio.sleep(BG_WATCH_INTERVAL)
            if _polling_active or _active_chat_id is None: continue
            current_hash = await get_snapshot_hash()
            if current_hash is None or current_hash == _bg_last_hash: continue
            _bg_last_hash = current_hash
            current_msgs = await get_current_messages()
            new_msgs = [m for m in current_msgs if m["hash"] not in _bg_baseline_hashes]
            for msg in new_msgs:
                _bg_baseline_hashes.add(msg["hash"])
                if msg["role"] == "user": continue
                text = msg["text"]
                if _is_duplicate(text): continue
                await send_long_message(_active_chat_id, f"📡 🤖 {text}")
                _mark_sent(text)
            _persist_baseline()
    finally:
        _bg_watcher_running = False


def get_main_menu():
    builder = ReplyKeyboardBuilder()
    for txt in ["Prompt", "Refresh", "Plan", "Task", "Walkthrough", "Mode/Model", "New Chat", "Stop", "Projects", "Brain Files", "Status", "Logs"]:
        builder.button(text=txt)
    builder.adjust(3)
    return builder.as_markup(resize_keyboard=True)


def get_mode_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Fast", callback_data="mode_Fast")
    builder.button(text="Planning", callback_data="mode_Planning")
    return builder.as_markup()


_model_cache: dict[str, Any] = {"models": [], "fetched_at": 0}

async def fetch_available_models() -> list[str]:
    now = time.time()
    if _model_cache["models"] and (now - _model_cache["fetched_at"]) < 300:
        return cast(list[str], _model_cache["models"])
    try:
        data = await pw_get("/models")
        models = cast(list[str], data.get("models", []))
        if models:
            _model_cache["models"] = models
            _model_cache["fetched_at"] = now
            return models
    except Exception: pass
    return cast(list[str], _model_cache["models"])


async def get_model_keyboard():
    models = await fetch_available_models()
    builder = InlineKeyboardBuilder()
    for m in models:
        builder.button(text=m, callback_data=f"model_{m}"[:64])
    builder.button(text="🔄 Refresh", callback_data="models_refresh")
    builder.adjust(2)
    return builder.as_markup()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not message.from_user or not is_allowed(message.from_user.id): return
    global _active_chat_id
    _active_chat_id = message.chat.id
    await message.answer("Antigravity Assistant active.", reply_markup=get_main_menu())


@dp.message(F.text == "Status")
async def btn_status(message: types.Message):
    if not message.from_user or not is_allowed(message.from_user.id): return
    await message.answer("Checking status...")


@dp.message(F.text == "Refresh")
async def btn_refresh(message: types.Message):
    if not message.from_user or not is_allowed(message.from_user.id): return
    await message.answer("Manual refresh initiated.")


@dp.message(F.text == "Mode/Model")
async def btn_settings(message: types.Message):
    if not message.from_user or not is_allowed(message.from_user.id): return
    builder = InlineKeyboardBuilder()
    builder.button(text="Change Mode", callback_data="settings_mode")
    builder.button(text="Change Model", callback_data="settings_model")
    await message.answer("Settings:", reply_markup=builder.as_markup())


@dp.callback_query(F.data == "settings_mode")
async def cb_settings_mode(callback: types.CallbackQuery):
    if not callback.from_user or not is_allowed(callback.from_user.id): return
    if isinstance(callback.message, types.Message):
        await callback.message.answer("Select Mode:", reply_markup=get_mode_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "settings_model")
async def cb_settings_model(callback: types.CallbackQuery):
    if not callback.from_user or not is_allowed(callback.from_user.id): return
    if isinstance(callback.message, types.Message):
        await callback.message.answer("Select Model:", reply_markup=await get_model_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("mode_"))
async def cb_set_mode(callback: types.CallbackQuery):
    if not callback.from_user or not is_allowed(callback.from_user.id) or not callback.data: return
    mode = callback.data.replace("mode_", "")
    if isinstance(callback.message, types.Message):
        await pw_post("/set-mode", {"mode": mode})
        await callback.message.answer(f"Mode set to {mode}")
    await callback.answer()


@dp.callback_query(F.data.startswith("model_"))
async def cb_set_model(callback: types.CallbackQuery):
    if not callback.from_user or not is_allowed(callback.from_user.id) or not callback.data: return
    model = callback.data.replace("model_", "")
    if isinstance(callback.message, types.Message):
        await pw_post("/set-model", {"model": model})
        await callback.message.answer(f"Model set to {model}")
    await callback.answer()


@dp.message(F.text)
async def handle_prompt(message: types.Message):
    if not message.from_user or not is_allowed(message.from_user.id) or not message.text: return
    text = message.text.strip()
    if text in ["Prompt", "Refresh", "Plan", "Task", "Walkthrough", "Mode/Model", "New Chat", "Stop", "Projects", "Brain Files", "Status", "Logs"]: return
    await pw_post("/send_message", {"text": text})
    await message.answer("Prompt sent.")
    asyncio.create_task(poll_for_response(message.chat.id))


def is_allowed(uid: int) -> bool: return uid == ALLOWED_USER_ID

async def main():
    asyncio.create_task(background_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
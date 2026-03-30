"""
tg_bot.py — Telegram bot for full bidirectional communication
with Antigravity AI agent via Phone Connect.
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
from typing import Optional, Any, cast, Dict, List, Tuple, Set

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

_polling_active = False
_last_snapshot_hash: Optional[str] = None
_waiting_for_response = False

_bg_watcher_running = False
_bg_last_hash: Optional[str] = None

# Restored from SQLite on startup — survives systemd restarts.
# NOTE: using list() + set() instead of set[str] annotation at module level
# to stay compatible with Python 3.8 runtime.
_bg_baseline_hashes: Set[str] = set(get_val("bg_baseline_hashes", []))

_active_chat_id: Optional[int] = None


def _persist_baseline() -> None:
    """Write current baseline hashes to SQLite so they survive restarts."""
    set_val("bg_baseline_hashes", list(_bg_baseline_hashes))


class AddProjectStates(StatesGroup):
    waiting_for_name = State()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def load_session() -> Dict[str, Any]:
    if SESSION_FILE.exists():
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                return cast(Dict[str, Any], json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
    return {"thread_id": str(int(time.time()))}


def save_session(session_data: Dict[str, Any]) -> None:
    session_data["last_updated"] = datetime.now().isoformat()
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Project management
# ---------------------------------------------------------------------------

def load_projects() -> List[Dict[str, Any]]:
    if PROJECTS_FILE.exists():
        try:
            with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
                projects = json.load(f)
            if isinstance(projects, list) and projects:
                return cast(List[Dict[str, Any]], projects)
        except (json.JSONDecodeError, ValueError):
            pass

    default_path = os.getenv("ANTIGRAVITY_PROJECT_DIR", "")
    projects: List[Dict[str, Any]] = []
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


def save_projects(projects: List[Dict[str, Any]]) -> None:
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def get_active_project() -> Optional[Dict[str, Any]]:
    projects = load_projects()
    for p in projects:
        if p.get("active"):
            return p
    return projects[0] if projects else None


def set_active_project(project_name: str) -> Optional[Dict[str, Any]]:
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


def add_project(name: str) -> Dict[str, Any]:
    projects = load_projects()
    for p in projects:
        if p["name"] == name:
            return p
    project_path = str(PROJECTS_BASE_DIR / name)
    new_project: Dict[str, Any] = {"name": name, "path": project_path, "active": False}
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


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def write_log(sender: str, message: str) -> None:
    """Append a line to the user-facing agent.log (shown via Logs button)."""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%H:%M:%S")
        f.write(f"[{timestamp}] {sender}: {message}\n")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def pw_get(path: str) -> Dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{PHONE_WORKER_URL}{path}",
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            res = await resp.json()
            return cast(Dict[str, Any], res)


async def pw_post(path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = data if data is not None else {}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{PHONE_WORKER_URL}{path}",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            res = await resp.json()
            return cast(Dict[str, Any], res)


async def fm_get(path: str) -> str:
    url = f"{FILE_SERVICE_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            if resp.status == 200:
                return text
            raise RuntimeError(f"HTTP {resp.status}: {text}")


async def restart_service() -> Tuple[bool, str]:
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
            return False, f"Error: {err}"
    except Exception as e:
        return False, f"Exception: {e}"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

_sent_text_prefixes: Dict[str, str] = {}
_MAX_SENT_PREFIXES = 200  # cap to prevent unbounded growth


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
            return True
    return False


def _mark_sent(text: str) -> None:
    global _sent_text_prefixes
    prefix = _text_prefix(text)
    if prefix:
        _sent_text_prefixes[prefix] = text
        # Trim to prevent unbounded growth across long sessions
        if len(_sent_text_prefixes) > _MAX_SENT_PREFIXES:
            # Drop oldest half
            keys = list(_sent_text_prefixes.keys())
            for k in keys[:_MAX_SENT_PREFIXES // 2]:
                del _sent_text_prefixes[k]


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

async def get_current_messages() -> List[Dict[str, str]]:
    try:
        data = await pw_get("/snapshot/text")
        return cast(List[Dict[str, str]], data.get("messages", []))
    except Exception:
        return []


async def get_snapshot_hash() -> Optional[str]:
    try:
        data = await pw_get("/snapshot/hash")
        return cast(Optional[str], data.get("hash"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

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
    baseline_hashes: Set[str] = {m["hash"] for m in baseline}
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
                if ever_sent and stable_polls >= 10:
                    break
                if not ever_sent and stable_polls >= 50:
                    break
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
                if _is_duplicate(text):
                    continue
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
    parts = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)]
    for part in parts:
        await bot.send_message(chat_id, part, **kwargs)
        await asyncio.sleep(0.3)


async def background_watcher() -> None:
    global _bg_watcher_running, _bg_last_hash, _bg_baseline_hashes
    if _bg_watcher_running:
        return
    _bg_watcher_running = True
    await asyncio.sleep(5)
    try:
        while True:
            await asyncio.sleep(BG_WATCH_INTERVAL)
            if _polling_active or _active_chat_id is None:
                continue
            current_hash = await get_snapshot_hash()
            if current_hash is None or current_hash == _bg_last_hash:
                continue
            _bg_last_hash = current_hash
            current_msgs = await get_current_messages()
            new_msgs = [m for m in current_msgs if m["hash"] not in _bg_baseline_hashes]
            for msg in new_msgs:
                _bg_baseline_hashes.add(msg["hash"])
                if msg["role"] == "user":
                    continue
                text = msg["text"]
                if _is_duplicate(text):
                    continue
                await send_long_message(_active_chat_id, f"📡 🤖 {text}")
                _mark_sent(text)
            _persist_baseline()
    finally:
        _bg_watcher_running = False


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def get_main_menu() -> types.ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for txt in [
        "Prompt", "Refresh", "Plan", "Task", "Walkthrough",
        "Mode/Model", "New Chat", "Stop", "Projects", "Brain Files",
        "Status", "Logs",
    ]:
        builder.button(text=txt)
    builder.adjust(3)
    return builder.as_markup(resize_keyboard=True)


def get_mode_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Fast", callback_data="mode_Fast")
    builder.button(text="Planning", callback_data="mode_Planning")
    return builder.as_markup()


_model_cache: Dict[str, Any] = {"models": [], "fetched_at": 0.0}


async def fetch_available_models() -> List[str]:
    now = time.time()
    if _model_cache["models"] and (now - _model_cache["fetched_at"]) < 300:
        return cast(List[str], _model_cache["models"])
    try:
        data = await pw_get("/models")
        models = cast(List[str], data.get("models", []))
        if models:
            _model_cache["models"] = models
            _model_cache["fetched_at"] = now
            return models
    except Exception:
        pass
    return cast(List[str], _model_cache["models"])


async def get_model_keyboard() -> types.InlineKeyboardMarkup:
    models = await fetch_available_models()
    builder = InlineKeyboardBuilder()
    for m in models:
        builder.button(text=m, callback_data=f"model_{m}"[:64])
    builder.button(text="🔄 Refresh", callback_data="models_refresh")
    builder.adjust(2)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def is_allowed(uid: int) -> bool:
    return uid == ALLOWED_USER_ID


@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    global _active_chat_id
    _active_chat_id = message.chat.id
    tg_log.info(f"Bot started by user {message.from_user.id}, chat_id={message.chat.id}")
    await message.answer("Antigravity Assistant active.", reply_markup=get_main_menu())


@dp.message(F.text == "Status")
async def btn_status(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    try:
        health = await pw_get("/health")
        pw_status = health.get("status", "unknown")
        pc_info = health.get("phone_connect", {})
        lines = [
            f"**phone_worker:** {pw_status}",
            f"**polling_active:** {_polling_active}",
            f"**bg_watcher:** {'running' if _bg_watcher_running else 'stopped'}",
            f"**baseline_hashes:** {len(_bg_baseline_hashes)}",
            f"**sent_prefixes:** {len(_sent_text_prefixes)}",
            f"**last_hash:** {_bg_last_hash or 'none'}",
        ]
        if isinstance(pc_info, dict):
            lines.append(f"**phone_connect:** {pc_info.get('status', str(pc_info))}")
        await message.answer("\n".join(lines))
    except Exception as e:
        await message.answer(f"Status check failed: {e}")


@dp.message(F.text == "Refresh")
async def btn_refresh(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    global _active_chat_id
    _active_chat_id = message.chat.id
    try:
        msgs = await get_current_messages()
        if not msgs:
            await message.answer("Snapshot пустой или phone_worker недоступен.")
            return
        last = msgs[-1]
        if last["role"] == "assistant":
            text = last["text"]
            if not _is_duplicate(text):
                await send_long_message(message.chat.id, f"🔄 🤖 {text}")
                _mark_sent(text)
                _bg_baseline_hashes.add(last["hash"])
                _persist_baseline()
                return
        await message.answer(f"Сообщений в snapshot: {len(msgs)}. Новых нет.")
    except Exception as e:
        await message.answer(f"Refresh error: {e}")


@dp.message(F.text == "Plan")
async def btn_plan(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    try:
        content = await fm_get("/latest/plan")
        await _send_or_file(message, content, "plan.md")
    except Exception as e:
        await message.answer(f"Plan недоступен: {e}")


@dp.message(F.text == "Task")
async def btn_task(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    try:
        content = await fm_get("/latest/task")
        await _send_or_file(message, content, "task.md")
    except Exception as e:
        await message.answer(f"Task недоступен: {e}")


@dp.message(F.text == "Walkthrough")
async def btn_walkthrough(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    try:
        content = await fm_get("/latest/walkthrough")
        await _send_or_file(message, content, "walkthrough.md")
    except Exception as e:
        await message.answer(f"Walkthrough недоступен: {e}")


@dp.message(F.text == "Logs")
async def btn_logs(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    try:
        if LOG_FILE.exists():
            content = LOG_FILE.read_text(encoding="utf-8")
            tail = content[-3000:] if len(content) > 3000 else content
            await message.answer(f"```\n{tail}\n```", parse_mode="Markdown")
        else:
            await message.answer("agent.log пуст или не существует.")
    except Exception as e:
        await message.answer(f"Logs error: {e}")


@dp.message(F.text == "Brain Files")
async def btn_brain_files(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    try:
        data = await pw_get("/snapshot/text")
        count = data.get("count", 0)
        raw_len = data.get("raw_length", 0)
        await message.answer(f"Snapshot: {count} блоков, {raw_len} символов HTML.")
    except Exception as e:
        await message.answer(f"Brain Files error: {e}")


@dp.message(F.text == "New Chat")
async def btn_new_chat(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    try:
        await pw_post("/new-chat")
        # Reset deduplication state for fresh session
        _sent_text_prefixes.clear()
        _bg_baseline_hashes.clear()
        _persist_baseline()
        await message.answer("Новый чат открыт, состояние сброшено.")
    except Exception as e:
        await message.answer(f"New Chat error: {e}")


@dp.message(F.text == "Stop")
async def btn_stop(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    try:
        await pw_post("/stop")
        await message.answer("Генерация остановлена.")
    except Exception as e:
        await message.answer(f"Stop error: {e}")


@dp.message(F.text == "Projects")
async def btn_projects(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    projects = load_projects()
    if not projects:
        await message.answer("Проекты не найдены.")
        return
    builder = InlineKeyboardBuilder()
    for p in projects:
        marker = "✅ " if p.get("active") else ""
        builder.button(text=f"{marker}{p['name']}", callback_data=f"proj_{p['name']}"[:64])
    builder.adjust(1)
    await message.answer("Выбери проект:", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("proj_"))
async def cb_select_project(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_allowed(callback.from_user.id) or not callback.data:
        return
    name = callback.data[5:]  # strip "proj_"
    target = set_active_project(name)
    if isinstance(callback.message, types.Message):
        if target:
            await callback.message.answer(
                f"Активный проект: **{name}**\n"
                f"Путь: `{target['path']}`\n\n"
                "Перезапусти сервис для применения: используй кнопку Status или "
                "выполни `systemctl --user restart antigravity-assistant`",
                parse_mode="Markdown",
            )
        else:
            await callback.message.answer(f"Проект '{name}' не найден.")
    await callback.answer()


@dp.message(F.text == "Mode/Model")
async def btn_settings(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id):
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="Change Mode", callback_data="settings_mode")
    builder.button(text="Change Model", callback_data="settings_model")
    await message.answer("Settings:", reply_markup=builder.as_markup())


@dp.callback_query(F.data == "settings_mode")
async def cb_settings_mode(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_allowed(callback.from_user.id):
        return
    if isinstance(callback.message, types.Message):
        await callback.message.answer("Select Mode:", reply_markup=get_mode_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "settings_model")
async def cb_settings_model(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_allowed(callback.from_user.id):
        return
    if isinstance(callback.message, types.Message):
        await callback.message.answer("Select Model:", reply_markup=await get_model_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("mode_"))
async def cb_set_mode(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_allowed(callback.from_user.id) or not callback.data:
        return
    mode = callback.data.replace("mode_", "")
    if isinstance(callback.message, types.Message):
        await pw_post("/set-mode", {"mode": mode})
        await callback.message.answer(f"Mode set to {mode}")
    await callback.answer()


@dp.callback_query(F.data.startswith("model_"))
async def cb_set_model(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_allowed(callback.from_user.id) or not callback.data:
        return
    model = callback.data.replace("model_", "")
    if isinstance(callback.message, types.Message):
        await pw_post("/set-model", {"model": model})
        await callback.message.answer(f"Model set to {model}")
    await callback.answer()


@dp.callback_query(F.data == "models_refresh")
async def cb_models_refresh(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not is_allowed(callback.from_user.id):
        return
    _model_cache["models"] = []
    _model_cache["fetched_at"] = 0.0
    if isinstance(callback.message, types.Message):
        await callback.message.answer("Select Model:", reply_markup=await get_model_keyboard())
    await callback.answer("Обновлено")


# ---------------------------------------------------------------------------
# Free-text prompt handler (must be last)
# ---------------------------------------------------------------------------

_MENU_BUTTONS = frozenset([
    "Prompt", "Refresh", "Plan", "Task", "Walkthrough",
    "Mode/Model", "New Chat", "Stop", "Projects", "Brain Files",
    "Status", "Logs",
])


@dp.message(F.text)
async def handle_prompt(message: types.Message) -> None:
    if not message.from_user or not is_allowed(message.from_user.id) or not message.text:
        return
    global _active_chat_id
    _active_chat_id = message.chat.id

    text = message.text.strip()
    if text in _MENU_BUTTONS:
        return

    tg_log.info(f"Sending prompt: {text[:80]}")
    write_log("USER", text)
    try:
        await pw_post("/send_message", {"text": text})
        await message.answer("✅ Промпт отправлен.")
        asyncio.create_task(poll_for_response(message.chat.id))
    except Exception as e:
        tg_log.error(f"Failed to send prompt: {e}")
        await message.answer(f"❌ Ошибка отправки: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _send_or_file(message: types.Message, content: str, filename: str) -> None:
    """Send content as message if short, or as file if long."""
    if len(content) <= 4000:
        await message.answer(f"```\n{content}\n```", parse_mode="Markdown")
    else:
        artifact = ARTIFACTS_DIR / filename
        artifact.write_text(content, encoding="utf-8")
        await message.answer_document(
            FSInputFile(artifact),
            caption=f"{filename} ({len(content)} chars)",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    tg_log.info("=== tg_bot starting ===")
    asyncio.create_task(background_watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
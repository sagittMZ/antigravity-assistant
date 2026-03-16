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
- Manage multiple projects
- Session logging
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
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
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

# Polling interval for checking AI responses (seconds)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3.0"))
# How long to poll for a response before giving up (seconds)
POLL_TIMEOUT = float(os.getenv("POLL_TIMEOUT", "300"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- State ----------

_known_message_hashes: set[str] = set()
_polling_active = False
_last_snapshot_hash: Optional[str] = None
_waiting_for_response = False
_pending_status_msg: Optional[types.Message] = None


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
    if PROJECTS_FILE.exists():
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # Default: use the one from .env
    default_path = os.getenv("ANTIGRAVITY_PROJECT_DIR", "")
    if default_path:
        return [{"name": Path(default_path).name, "path": default_path, "active": True}]
    return []


def save_projects(projects: list[dict]):
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def get_active_project() -> Optional[dict]:
    projects = load_projects()
    for p in projects:
        if p.get("active"):
            return p
    return projects[0] if projects else None


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
    """Poll for new messages from Antigravity.
    Sends new messages back to the Telegram chat."""
    global _polling_active, _last_snapshot_hash, _waiting_for_response

    if _polling_active:
        return

    _polling_active = True
    _waiting_for_response = True
    effective_timeout = timeout or POLL_TIMEOUT
    start_time = time.time()

    # Get initial snapshot hash
    _last_snapshot_hash = await get_snapshot_hash()

    # Take a baseline of existing messages
    baseline = await get_current_messages()
    baseline_hashes = {m["hash"] for m in baseline}

    consecutive_same = 0
    prev_hash = _last_snapshot_hash

    try:
        while time.time() - start_time < effective_timeout:
            await asyncio.sleep(POLL_INTERVAL)

            current_hash = await get_snapshot_hash()

            if current_hash == prev_hash:
                consecutive_same += 1
                # If no changes for 15 consecutive polls (45 sec with 3s interval),
                # AI has probably finished
                if consecutive_same > 15 and consecutive_same > 5:
                    break
                continue

            consecutive_same = 0
            prev_hash = current_hash

            # Snapshot changed — check for new messages
            current_msgs = await get_current_messages()
            new_msgs = [m for m in current_msgs if m["hash"] not in baseline_hashes]

            for msg in new_msgs:
                baseline_hashes.add(msg["hash"])

                role_emoji = "\U0001f916" if msg["role"] != "user" else "\U0001f464"
                text = msg["text"]

                # Send to Telegram (split long messages)
                await send_long_message(chat_id, f"{role_emoji} {text}")
                write_log("AI_RESPONSE", text[:200])

            # If we got assistant messages and snapshot stabilized, we can stop
            assistant_msgs = [m for m in new_msgs if m["role"] != "user"]
            if assistant_msgs and consecutive_same > 3:
                break

    except Exception as e:
        write_log("POLL_ERROR", str(e))
        await bot.send_message(chat_id, f"Polling error: {e}")
    finally:
        _polling_active = False
        _waiting_for_response = False


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
    builder.button(text="Logs")
    builder.button(text="Brain Files")
    builder.button(text="Status")
    builder.adjust(3, 3, 2, 2)
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


# ---------- Auth ----------

def is_allowed(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID


# ---------- Handlers ----------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    await message.answer(
        "*Antigravity Assistant*\n\n"
        "Send any text as a prompt to Antigravity AI.\n"
        "Use buttons for quick actions:\n\n"
        "*Prompt* — type and send prompt\n"
        "*Plan* — implementation plan\n"
        "*Task* — current task\n"
        "*Walkthrough* — walkthrough\n"
        "*Mode/Model* — switch mode or model\n"
        "*New Chat* — start new conversation\n"
        "*Stop* — stop current generation\n"
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

    # Active project
    project = get_active_project()
    if project:
        status_parts.append(f"Project: {project['name']}")

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
            _known_message_hashes.clear()
            _last_snapshot_hash = None
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

    text = message.text.strip()
    if not text:
        return

    # Skip button texts
    button_texts = {
        "Prompt", "Plan", "Task", "Walkthrough",
        "Mode/Model", "New Chat", "Stop", "Logs",
        "Brain Files", "Status",
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

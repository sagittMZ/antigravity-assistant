import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.types import FSInputFile
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
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "agent.log"

FILE_SERVICE_URL = os.getenv("FILE_SERVICE_URL", "http://127.0.0.1:8787").strip()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def load_session():
    if SESSION_FILE.exists():
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"thread_id": str(int(time.time()))}


def save_session(session_data):
    session_data["last_updated"] = datetime.now().isoformat()
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)


current_session = load_session()


def write_log(sender: str, message: str):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%H:%M:%S")
        f.write(f"[{timestamp}] {sender}: {message}\n")


def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="🆕 New session")
    builder.button(text="📋 Plan")
    builder.button(text="📝 Task")
    builder.button(text="📖 Walkthrough")
    builder.button(text="📜 Logs")
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


async def fetch_text(endpoint: str) -> str:
    url = f"{FILE_SERVICE_URL}{endpoint}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            text = await resp.text()
            if resp.status == 200:
                return text
            raise RuntimeError(f"HTTP {resp.status}: {text}")


def is_allowed(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    await message.answer(
        "🛠 Antigravity Assistant.\n"
        "Buttons:\n"
        "🆕 New session — clear context\n"
        "📋 Plan — show implementation_plan.md\n"
        "📝 Task — show task.md\n"
        "📖 Walkthrough — show walkthrough.md (if exists)\n"
        "📜 Logs — last log entries",
        reply_markup=get_main_menu(),
    )


@dp.message(F.text == "🆕 New session")
async def btn_new_session(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    current_session["thread_id"] = str(int(time.time()))
    save_session(current_session)
    write_log("SYSTEM", "New session created (context cleared).")
    await message.answer("✨ Context cleared. New session created.", reply_markup=get_main_menu())


@dp.message(F.text == "📋 Plan")
async def btn_plan(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        content = await fetch_text("/latest/plan")
    except Exception as e:
        await message.answer(f"❌ Failed to get plan: {e}", reply_markup=get_main_menu())
        return

    write_log("PLAN_VIEW", "User requested latest implementation plan.")
    if len(content) < 3500:
        await message.answer(
            f"📋 *implementation_plan.md*:\n\n```markdown\n{content}\n```",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )
    else:
        tmp_path = ARTIFACTS_DIR / "implementation_plan_latest.md"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        await message.answer_document(
            FSInputFile(tmp_path),
            caption="📋 implementation_plan.md",
            reply_markup=get_main_menu(),
        )


@dp.message(F.text == "📝 Task")
async def btn_task(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        content = await fetch_text("/latest/task")
    except Exception as e:
        await message.answer(f"❌ Failed to get task: {e}", reply_markup=get_main_menu())
        return

    write_log("TASK_VIEW", "User requested current task.")
    if len(content) < 3500:
        await message.answer(
            f"📝 *task.md*:\n\n```markdown\n{content}\n```",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )
    else:
        tmp_path = ARTIFACTS_DIR / "task_latest.md"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        await message.answer_document(
            FSInputFile(tmp_path),
            caption="📝 task.md",
            reply_markup=get_main_menu(),
        )


@dp.message(F.text == "📖 Walkthrough")
async def btn_walkthrough(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        content = await fetch_text("/latest/walkthrough")
    except Exception as e:
        await message.answer(f"❌ Failed to get walkthrough: {e}", reply_markup=get_main_menu())
        return

    write_log("WALKTHROUGH_VIEW", "User requested walkthrough.")
    if len(content) < 3500:
        await message.answer(
            f"📖 *walkthrough.md*:\n\n```markdown\n{content}\n```",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )
    else:
        tmp_path = ARTIFACTS_DIR / "walkthrough_latest.md"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        await message.answer_document(
            FSInputFile(tmp_path),
            caption="📖 walkthrough.md",
            reply_markup=get_main_menu(),
        )


@dp.message(F.text == "📜 Logs")
async def btn_logs(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    if not LOG_FILE.exists():
        await message.answer("Log file is empty yet.", reply_markup=get_main_menu())
        return
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()[-20:]
    log_text = "".join(lines)
    await message.answer(
        f"📜 Last logs:\n```text\n{log_text}\n```",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


@dp.message()
async def handle_prompt(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    write_log("CHAT", message.text)
    await message.answer(
        "Message recorded in log. Use buttons to view plans/tasks.",
        reply_markup=get_main_menu(),
    )


async def main():
    print("🚀 Telegram bot started. Waiting for updates...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

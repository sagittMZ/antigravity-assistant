# AI Workflow & Policies for Antigravity Assistant

This file defines the architecture, tech stack, and strict engineering principles for AI assistants (Antigravity, Cursor, etc.) working with this repository.

## 1. Project Overview & Architecture
This project is a lightweight, asynchronous Telegram bot that acts as a headless bridge to a local Antigravity AI instance via the Phone Connect Node.js server. 

**Core Stack:**
- **Language:** Python 3.8+
- **Bot Framework:** `aiogram` (v3.x)
- **Web/API:** `FastAPI`, `aiohttp` (for async HTTP requests)
- **Parsing:** `BeautifulSoup4` (HTML/DOM parsing)
- **State Management:** SQLite (via `app/state.py`)
- **Process Management:** `subprocess` with `os.killpg` (Process Group isolation), managed globally via `systemd`.

## 2. Language Policy
- **Communication:** All chat communication with the user must be in **Russian**.
- **Code & Repository (Git):** All code, variables, docstrings, inline comments, commit messages, and file names must be strictly in **English**.
- **Working Documents & Artifacts (Not in Git):** All working documents, `implementation_plan.md`, `task.md`, `walkthrough.md`, analytical reports, and any other internal files meant for the user must be written in **Russian**. These artifacts should generally not be committed to the repository unless explicitly requested.

## 3. Engineering & QA Principles (Non-Negotiable)
When proposing code changes or architecture updates, you must adhere to the following principles:

- **Zero Zombie Processes:** When managing subprocesses (e.g., in `launcher.py`), always use `start_new_session=True` and kill the entire process group (`os.killpg`) to prevent memory leaks and orphaned Node.js/Electron instances.
- **No Regex for HTML:** Never use Regular Expressions to parse DOM trees or HTML snapshots from Antigravity. Always use `BeautifulSoup4`.
- **Stateless Handlers:** The Telegram bot (`tg_bot.py`) must remain stateless in memory. All persistent state (message deduplication hashes, active chat IDs) must be written to the SQLite store (`app/state.py`).
- **Async First:** Never use synchronous blocking calls (`requests`, `time.sleep()`, synchronous file I/O for large files) inside `tg_bot.py`, `phone_worker.py`, or `file_monitor.py`. Use `aiohttp` and `asyncio.sleep()`.
- **Centralized Logging:** Do not use `print()`. All output must be routed through `app/logger.py` with `RotatingFileHandler` to prevent disk overflow. Handle exceptions gracefully and log the tracebacks.

## 4. Workflow & Execution Strategy
- **Simple over clever:** Prefer robust, explainable solutions over complex abstractions.
- **Small, safe steps:** Execute one logical step at a time (e.g., update one file, verify it works, then move to the next).
- **Edge-Case Focus:** Always consider what happens if Phone Connect is dead, the API returns 502, or Telegram rate-limits the bot. Fail gracefully and notify the user via the bot interface.

## 5. Instruction Hierarchy
If instructions conflict, follow this priority order:
1. System-level defaults: `~/.gemini/GEMINI.md` (if exists).
2. This `AI_WORKFLOW.md` file.
3. The direct user prompt in the current chat session.
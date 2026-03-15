# Antigravity Assistant

Helper stack around Google Antigravity IDE and Phone Connect. This tool provides a bridge to monitor and control your AI encoding sessions via Telegram.

- **One-click launch**: Start the entire stack with a single command.
- **Telegram Interface**: View plans, tasks, and logs remotely.
- **File Service**: Access latest brain files and project source code.
- **Remote Control**: Send messages to the assistant directly from Telegram.

---

## 🏗 Components

All application code lives in the `app/` directory:

- **`app/launcher.py`**: Main entry point. Orchestrates the startup of all services, including Antigravity IDE and Phone Connect.
- **`app/file_monitor.py`**: FastAPI service exposing:
  - `GET /latest/plan` → Serves `implementation_plan.md` from the latest brain session.
  - `GET /latest/task` → Serves `task.md` from the latest brain session.
  - `GET /latest/walkthrough` → Serves `walkthrough.md` if present.
  - `GET /project/file?path=...` → Access arbitrary project files by relative path.
- **`app/phone_worker.py`**: FastAPI + Playwright.
  - Manages a headless Chromium instance with Phone Connect open.
  - `POST /init`: Sends an initial message to the assistant.
  - `POST /send_message`: Sends arbitrary text commands to the agent (remote control).
- **`app/tg_bot.py`**: Telegram bot built with `aiogram`.
  - Interactive buttons for **Plan**, **Task**, **Walkthrough**, and **Logs**.
  - Automatically handles file delivery for large documents via the `artifacts/` folder.
  - Logs user interactions into `logs/agent.log`.

---

## 🛠 Setup

### Prerequisites
- Python 3.8+
- [Antigravity IDE](https://github.com/google-deepmind/antigravity) installed and in `PATH`.
- Phone Connect project (fork of `antigravity_phone_chat`).
- Telegram bot token (from [@BotFather](https://t.me/botfather)) and your Telegram User ID.

### Installation
1. Clone the repository and navigate to the directory:
   ```bash
   cd antigravity-assistant
   ```
2. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

### Configuration
1. Create `.env` from the example:
   ```bash
   cp .env.example .env
   ```
2. Fill in the following variables in `.env`:
   - `BOT_TOKEN`: Your Telegram bot token.
   - `ALLOWED_USER_ID`: Your Telegram User ID (to restrict access).
   - `PHONE_CONNECT_URL`: URL where Phone Connect is available (tunnel or local).
   - `ANTIGRAVITY_PROJECT_DIR`: Path to your current Antigravity project.
   - `PHONE_CONNECT_DIR`: Path to your Phone Connect project.
   - `LAUNCH_ANTIGRAVITY`: `true/false` (should launcher start the IDE).
   - `AUTO_INIT_SESSION`: `true/false` (should launcher initialize the session).

---

## 🚀 Running

Start the entire stack with:
```bash
python3 -m app.launcher
```

The launcher will:
1. Start **Antigravity IDE** (if port 9000 is free).
2. Start **Phone Connect** via its own launcher.
3. Start **phone_worker** (Playwright + FastAPI) on `127.0.0.1:8788`.
4. Initialize the session via `/init` (optional).
5. Start **file_monitor** on `127.0.0.1:8787`.
6. Start the **Telegram bot**.

Use `Ctrl+C` in the launcher terminal to stop all services at once.

---

## 📊 Logs and Artifacts

- **Logs**: Service logs are stored in `logs/` (e.g., `Antigravity.log`, `tg_bot.log`).
- **Agent Log**: `agent.log` tracks bot actions and user commands.
- **Artifacts**: Exported markdown files are stored in `artifacts/` to be served as documents if they are too large for Telegram messages.

*Both directories are ignored by git and can be cleared at any time.*

---

## 📝 Notes
- **Brain Path**: Antigravity brain directory is expected at `~/.gemini/antigravity/brain`.
- **Extensibility**: This project is designed to be extended with Git integration, additional remote control commands, etc.

---

## 🔗 Connect
Follow the project author for updates:  
[![Twitter](https://img.shields.io/badge/Twitter-1DA1F2?style=for-the-badge&logo=twitter&logoColor=white)](https://x.com/s_a_g_i_t_t)

---

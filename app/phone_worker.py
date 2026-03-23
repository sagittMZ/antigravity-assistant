"""
phone_worker.py — FastAPI gateway to Phone Connect REST API.

CHANGES vs original:
- Removed print() — replaced with pw_log (RotatingFileHandler via app.logger).
  Fixes AI_WORKFLOW.md violation: "Do not use print()".
- Added _auth_lock (asyncio.Lock) around _ensure_auth to prevent concurrent
  coroutines (background_watcher + poll_for_response) from firing duplicate
  POST /login requests simultaneously. Double-checked inside the lock.
- No Playwright — this file uses only aiohttp REST calls to Phone Connect.
  Playwright was already removed in the previous iteration; confirmed clean.
- All business logic (snapshot parsing, BeautifulSoup, clean_text) unchanged.
"""
from __future__ import annotations

import asyncio
import os
import re
import hashlib
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import aiohttp
import ssl as _ssl

from app.logger import setup_logger

# Enforce safe DOM parsing — no regex on HTML trees.
try:
    from bs4 import BeautifulSoup
except ImportError:
    raise RuntimeError("BeautifulSoup is missing. Run: pip install beautifulsoup4")

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

pw_log = setup_logger("phone_worker", "phone_worker.log")

PHONE_CONNECT_URL = os.getenv(
    "PHONE_CONNECT_LOCAL_URL", "http://127.0.0.1:3000"
).rstrip("/")
PHONE_CONNECT_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

app = FastAPI(title="Phone Worker — Antigravity Assistant")

_cookie_jar: Optional[aiohttp.CookieJar] = None
_authenticated = False

# Lock prevents two concurrent coroutines from both entering _ensure_auth
# and firing duplicate POST /login requests when _authenticated is False.
_auth_lock = asyncio.Lock()


def _make_ssl_context() -> Optional[_ssl.SSLContext]:
    """Create a permissive SSL context for self-signed Phone Connect certs."""
    if PHONE_CONNECT_URL.startswith("https"):
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        return ctx
    return None


def _get_cookie_jar() -> aiohttp.CookieJar:
    global _cookie_jar
    if _cookie_jar is None:
        _cookie_jar = aiohttp.CookieJar(unsafe=True)
    return _cookie_jar


async def _ensure_auth(session: aiohttp.ClientSession) -> bool:
    """Authenticate against Phone Connect if not already done.

    Uses double-checked locking: check outside the lock for the common
    (already-authenticated) path, then re-check inside the lock to handle
    the race where two coroutines both see _authenticated=False at the same time.
    """
    global _authenticated
    if _authenticated:
        return True
    if not PHONE_CONNECT_PASSWORD:
        _authenticated = True
        return True

    async with _auth_lock:
        # Re-check after acquiring the lock — another coroutine may have
        # authenticated while we were waiting.
        if _authenticated:
            return True
        try:
            async with session.post(
                f"{PHONE_CONNECT_URL}/login",
                json={"password": PHONE_CONNECT_PASSWORD},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if data.get("success"):
                    _authenticated = True
                    pw_log.info("Authenticated against Phone Connect.")
                    return True
                pw_log.warning(f"Phone Connect auth rejected: {data}")
                return False
        except Exception as exc:
            # Was print() — now goes to rotating log file.
            pw_log.warning(f"Phone Connect auth failed: {exc}")
            return False


async def _pc_request(method: str, path: str, json_data: dict = None) -> dict:
    """Make an authenticated HTTP request to Phone Connect."""
    jar = _get_cookie_jar()
    ssl_ctx = _make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else None

    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as session:
        await _ensure_auth(session)
        url = f"{PHONE_CONNECT_URL}{path}"
        headers = {
            "Content-Type": "application/json",
            # Required to bypass ngrok's browser-warning interstitial page.
            "ngrok-skip-browser-warning": "true",
        }
        timeout = aiohttp.ClientTimeout(total=15)

        try:
            kw = dict(headers=headers, timeout=timeout)
            if method == "GET":
                async with session.get(url, **kw) as resp:
                    if resp.status == 401:
                        global _authenticated
                        _authenticated = False
                        raise HTTPException(502, "Phone Connect authentication lost")
                    return await resp.json()
            else:
                async with session.post(url, json=json_data or {}, **kw) as resp:
                    if resp.status == 401:
                        _authenticated = False
                        raise HTTPException(502, "Phone Connect authentication lost")
                    return await resp.json()
        except aiohttp.ClientError as exc:
            raise HTTPException(502, f"Phone Connect unreachable: {exc}")


# --- Pydantic request models ---

class SendMessageRequest(BaseModel):
    text: str

class SetModeRequest(BaseModel):
    mode: str

class SetModelRequest(BaseModel):
    model: str


# --- API endpoints ---

@app.get("/health")
async def health():
    try:
        pc = await _pc_request("GET", "/health")
        return {"status": "ok", "phone_connect": pc}
    except Exception as exc:
        return {"status": "degraded", "phone_connect_error": str(exc)}


@app.post("/send_message")
async def send_message(req: SendMessageRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Empty text")
    result = await _pc_request("POST", "/send", {"message": text})
    return {"status": "ok", "result": result}


@app.get("/snapshot")
async def get_snapshot():
    return await _pc_request("GET", "/snapshot")


@app.get("/snapshot/text")
async def get_snapshot_text():
    raw = await _pc_request("GET", "/snapshot")
    html = raw.get("html", "")
    if not html:
        return {"messages": [], "raw_length": 0}
    messages = parse_messages_from_html(html)
    return {"messages": messages, "count": len(messages), "raw_length": len(html)}


@app.get("/app-state")
async def get_app_state():
    return await _pc_request("GET", "/app-state")


@app.post("/set-mode")
async def set_mode(req: SetModeRequest):
    if req.mode not in ("Fast", "Planning"):
        raise HTTPException(400, "Mode must be 'Fast' or 'Planning'")
    return await _pc_request("POST", "/set-mode", {"mode": req.mode})


@app.post("/set-model")
async def set_model(req: SetModelRequest):
    return await _pc_request("POST", "/set-model", {"model": req.model})


@app.get("/models")
async def get_available_models():
    return await _pc_request("GET", "/models")


@app.post("/stop")
async def stop_generation():
    return await _pc_request("POST", "/stop")


@app.post("/new-chat")
async def new_chat():
    return await _pc_request("POST", "/new-chat")


@app.get("/chat-history")
async def get_chat_history():
    return await _pc_request("GET", "/chat-history")


@app.post("/init")
async def init_session():
    try:
        result = await _pc_request(
            "POST",
            "/send",
            {"message": os.getenv("PHONE_INIT_MESSAGE", "init session")},
        )
        return {"status": "ok", "result": result}
    except Exception as exc:
        raise HTTPException(500, f"Init error: {exc}")


# ---------------------------------------------------------------------------
# Text cleaning utilities
# ---------------------------------------------------------------------------
# These operate on plain text (after BS4 extraction), NOT on raw HTML.
# Regex is only used here on extracted plain text — never on DOM trees.
# ---------------------------------------------------------------------------

_RE_WS = re.compile(r"[ \t]+")
_RE_MULTI_NL = re.compile(r"\n{3,}")
_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_CSS = re.compile(
    r"(?:^|\n)\s*[\w\-.*#@:>\[\]=~^|,\s]+\s*\{[^}]*\}", re.MULTILINE
)
_RE_FRAGMENT = re.compile(r"^[A-Za-z]+[),.:;]+$")

_RE_GIT = re.compile(
    r"(?:^|\n)\s*(?:"
    r"diff --git\b|index [0-9a-f]+\.\.[0-9a-f]+|"
    r"--- a/|"
    r"\+\+\+ b/|"
    r"@@ .+? @@|"
    r"commit [0-9a-f]{7,40}\b|"
    r"Author:\s|Date:\s|Merge:\s|"
    r"(?:modified|deleted|renamed|new file):\s+"
    r").*",
    re.MULTILINE,
)

_RE_SHELL = re.compile(
    r"(?:^|\n)\s*(?:"
    r"npm\s+(?:warn|info|notice|WARN|ERR!)\b|"
    r"\$\s+\S|>\s+\S+@\S+\s|"
    r"added \d+ packages?\b|found \d+ vulnerabilities?\b|"
    r"up to date\b|"
    r"[✓✗⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"
    r").*",
    re.MULTILINE,
)
_RE_DECORATIVE = re.compile(r"[─━═▓░▒█▄▀]{4,}")
_RE_AGENT_HEADER = re.compile(
    r"\[\d{1,2}/\d{1,2}/\d{2,4}[^\]]+\][^:]+:\s*(?:🤖\s*)?", re.IGNORECASE
)
_RE_SYS_LOGS = re.compile(
    r"(\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\b)", re.IGNORECASE
)

_UI_CHROME: set[str] = {
    "always run", "cancel", "collapse all", "expand all",
    "open", "proceed", "copy", "retry", "undo", "running command",
    "running...", "running", "ran command", "relocate", "exit code 0",
    "exit code 1", "exit code 2", "background steps", "progress updates",
    "files edited", "generating...", "generated", "thinking..", "thinking...",
    "created task", "edited task", "created", "edited",
    "analyzed implementation plan", "analyzed task", "analyzed walkthrough",
    "analyzed conversation", "error while analyzing directory",
    "cannot list directory", "conversation sections",
    "which does not exist.", "started investigating",
    "finding conversation logs", "checking older artifacts", "stage:",
}


def _clean_text(raw: str) -> str:
    text = _RE_ANSI.sub("", raw)
    text = _RE_CSS.sub("", text)
    text = _RE_GIT.sub("", text)
    text = _RE_SHELL.sub("", text)
    text = _RE_DECORATIVE.sub("", text)
    text = _RE_WS.sub(" ", text)
    text = _RE_AGENT_HEADER.sub("", text)
    text = _RE_SYS_LOGS.sub(r"\n\n\1", text)

    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if stripped.lower() in _UI_CHROME:
            continue
        if re.match(r"^~?[/\\]", stripped) and len(stripped) < 80:
            continue
        if re.match(r"^\s*[\w-]+\s*:\s*[^;]+;\s*$", stripped):
            continue
        if re.match(r"^\s*[$#>]\s+", stripped):
            continue
        if len(stripped) < 4 and _RE_FRAGMENT.match(stripped):
            continue
        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    return _RE_MULTI_NL.sub("\n\n", text).strip()


def _is_meaningful(text: str) -> bool:
    if len(text) < 10:
        return False
    words = text.split()
    if len(words) < 4:
        return False
    short_tokens = sum(1 for w in words if len(w) <= 2)
    if len(words) > 0 and short_tokens / len(words) > 0.6:
        return False
    return True


def _stable_hash(text: str) -> str:
    prefix = text[:200].strip()
    return hashlib.md5(prefix.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Safe DOM parsing via BeautifulSoup4
# ---------------------------------------------------------------------------
# RULE: Never use regex directly on HTML. BS4 handles the DOM tree.
# Regex is only applied AFTER get_text() — i.e. on plain extracted strings.
# ---------------------------------------------------------------------------

def parse_messages_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    # 1. Decompose heavy tags that contain no useful text content.
    for tag in soup(["style", "script", "noscript", "template", "svg", "canvas"]):
        tag.decompose()

    # 2. Remove IDE chrome elements (terminals, code editors).
    for tag in soup.find_all(
        class_=re.compile(r"xterm|terminal|cm-editor|cm-content|monaco-editor", re.I)
    ):
        tag.decompose()

    messages = []

    # 3. Try to find structured message blocks first.
    blocks = soup.find_all(
        lambda t: t.name == "div"
        and (
            t.has_attr("data-message")
            or any("message" in c.lower() for c in t.get("class", []))
        )
    )

    if not blocks:
        # Fallback: treat the entire page as a single assistant message.
        # FIXED: original code had a comment placeholder here but no return —
        # function silently returned [] on fallback path.
        text = soup.get_text(separator=" ", strip=True)
        text = _clean_text(text)
        if _is_meaningful(text):
            return [{"role": "assistant", "text": text, "hash": _stable_hash(text)}]
        return []

    # Structured extraction from identified message blocks.
    for block in blocks:
        text = block.get_text(separator=" ", strip=True)
        text = _clean_text(text)
        if not _is_meaningful(text):
            continue

        role = "assistant"
        classes = " ".join(block.get("class", [])).lower()
        if "user" in classes:
            role = "user"

        messages.append({"role": role, "text": text, "hash": _stable_hash(text)})

    return messages

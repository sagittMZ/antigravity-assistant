"""
phone_worker.py — FastAPI gateway to Phone Connect REST API.
"""
from __future__ import annotations

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

# Имплементация безопасного DOM-парсера
try:
    from bs4 import BeautifulSoup
except ImportError:
    raise RuntimeError("BeautifulSoup is missing. Run: pip install beautifulsoup4")

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

PHONE_CONNECT_URL = os.getenv(
    "PHONE_CONNECT_LOCAL_URL", "http://127.0.0.1:3000"
).rstrip("/")
PHONE_CONNECT_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

app = FastAPI(title="Phone Worker — Antigravity Assistant")

_cookie_jar: Optional[aiohttp.CookieJar] = None
_authenticated = False

def _make_ssl_context() -> Optional[_ssl.SSLContext]:
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
    global _authenticated
    if _authenticated:
        return True
    if not PHONE_CONNECT_PASSWORD:
        _authenticated = True
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
                return True
            return False
    except Exception as exc:
        print(f"⚠️ Phone Connect auth failed: {exc}")
        return False

async def _pc_request(method: str, path: str, json_data: dict = None) -> dict:
    jar = _get_cookie_jar()
    ssl_ctx = _make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else None

    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as session:
        await _ensure_auth(session)
        url = f"{PHONE_CONNECT_URL}{path}"
        headers = {
            "Content-Type": "application/json",
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

class SendMessageRequest(BaseModel):
    text: str

class SetModeRequest(BaseModel):
    mode: str

class SetModelRequest(BaseModel):
    model: str

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
            "POST", "/send",
            {"message": os.getenv("PHONE_INIT_MESSAGE", "init session")},
        )
        return {"status": "ok", "result": result}
    except Exception as exc:
        raise HTTPException(500, f"Init error: {exc}")

# --- Text Cleaners (Non-HTML) ---
_RE_WS = re.compile(r'[ \t]+')
_RE_MULTI_NL = re.compile(r'\n{3,}')
_RE_ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_RE_CSS = re.compile(r'(?:^|\n)\s*[\w\-.*#@:>\[\]=~^|,\s]+\s*\{[^}]*\}', re.MULTILINE)
_RE_FRAGMENT = re.compile(r'^[A-Za-z]+[),.:;]+$')

_RE_GIT = re.compile(
    r'(?:^|\n)\s*(?:'
    r'diff --git\b|index [0-9a-f]+\.\.[0-9a-f]+|'
    r]--- a/|'
    r'\+\+\+ b/|'
    r'@@ .+? @@|'
    r'commit [0-9a-f]{7,40}\b|'
    r'Author:\s|Date:\s|Merge:\s|'
    r'(?:modified|deleted|renamed|new file):\s+'
    r').*', re.MULTILINE,
)

_RE_SHELL = re.compile(
    r'(?:^|\n)\s*(?:'
    r'npm\s+(?:warn|info|notice|WARN|ERR!)\b|'
    r'\$\s+\S|>\s+\S+@\S+\s|'
    r'added \d+ packages?\b|found \d+ vulnerabilities?\b|'
    r'up to date\b|'
    r'[✓✗⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]'
    r').*', re.MULTILINE,
)
_RE_DECORATIVE = re.compile(r'[─━═▓░▒█▄▀]{4,}')
_RE_AGENT_HEADER = re.compile(r'\[\d{1,2}/\d{1,2}/\d{2,4}[^\]]+\][^:]+:\s*(?:🤖\s*)?', re.IGNORECASE)
_RE_SYS_LOGS = re.compile(r'(\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\b)', re.IGNORECASE)

_UI_CHROME: set[str] = {
    "always run", "cancel", "collapse all", "expand all",
    "open", "proceed", "copy", "retry", "undo", "running command",
    "running...", "running", "ran command", "relocate", "exit code 0",
    "exit code 1", "exit code 2", "background steps", "progress updates",
    "files edited", "generating...", "generated", "thinking..", "thinking...",
    "created task", "edited task", "created", "edited", "analyzed implementation plan",
    "analyzed task", "analyzed walkthrough", "analyzed conversation",
    "error while analyzing directory", "cannot list directory", "conversation sections",
    "which does not exist.", "started investigating", "finding conversation logs",
    "checking older artifacts", "stage:"
}

def _clean_text(raw: str) -> str:
    text = _RE_ANSI.sub("", raw)
    text = _RE_CSS.sub("", text)
    text = _RE_GIT.sub("", text)
    text = _RE_SHELL.sub("", text)
    text = _RE_DECORATIVE.sub("", text)
    text = _RE_WS.sub(" ", text)
    text = _RE_AGENT_HEADER.sub("", text)
    text = _RE_SYS_LOGS.sub(r'\n\n\1', text)

    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if stripped.lower() in _UI_CHROME:
            continue
        if re.match(r'^~?[/\\]', stripped) and len(stripped) < 80:
            continue
        if re.match(r'^\s*[\w-]+\s*:\s*[^;]+;\s*$', stripped):
            continue
        if re.match(r'^\s*[$#>]\s+', stripped):
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

# --- SAFE DOM PARSING VIA BEAUTIFULSOUP ---

def parse_messages_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    # 1. Nuke heavy/garbage tags that we don't need text from
    for tag in soup(["style", "script", "noscript", "template", "svg", "canvas"]):
        tag.decompose()

    # 2. Nuke specific IDE elements (terminals, raw code editors)
    for tag in soup.find_all(class_=re.compile(r'xterm|terminal|cm-editor|cm-content|monaco-editor', re.I)):
        tag.decompose()

    messages = []
    
    # 3. Attempt to find specific message blocks
    blocks = soup.find_all(lambda t: t.name == "div" and (t.has_attr("data-message") or any("message" in c.lower() for c in t.get("class", []))))

    if not blocks:
        # Fallback: if no specific blocks found, extract all clean text from body
        text = soup.get_text(separator="\n\n", strip=True)
        text = _clean_text(text)
        if _is_meaningful(text):
            messages.append({
                "role": "unknown",
                "text": text,
                "hash": _stable_hash(text)
            })
    else:
        # Structured extraction
        for block in blocks:
            text = block.get_text(separator="\n\n", strip=True)
            text = _clean_text(text)
            if not _is_meaningful(text):
                continue

            role = "assistant"
            classes = " ".join(block.get("class", [])).lower()
            if "user" in classes:
                role = "user"

            messages.append({
                "role": role,
                "text": text,
                "hash": _stable_hash(text)
            })

    return messages
"""
phone_worker.py — FastAPI gateway to Phone Connect REST API.

Instead of launching a headless browser, this module calls the
Phone Connect HTTP endpoints directly (the Node.js server exposes
/send, /snapshot, /health, /set-mode, /set-model, /stop,
/new-chat, /chat-history, /app-state).

This keeps the architecture simpler and more reliable.
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

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

# Phone Connect local URL (the Node.js server, NOT the ngrok tunnel)
# Use https:// if Phone Connect has SSL certs generated
PHONE_CONNECT_URL = os.getenv("PHONE_CONNECT_LOCAL_URL", "http://127.0.0.1:3000").rstrip("/")
# Password for Phone Connect auth
PHONE_CONNECT_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

app = FastAPI(title="Phone Worker — Antigravity Assistant")

# Session cookie jar to persist auth between calls
_cookie_jar: Optional[aiohttp.CookieJar] = None
_authenticated = False


def _make_ssl_context():
    """Create a permissive SSL context for local self-signed certs."""
    if PHONE_CONNECT_URL.startswith("https"):
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        return ctx
    return None


def get_cookie_jar() -> aiohttp.CookieJar:
    global _cookie_jar
    if _cookie_jar is None:
        _cookie_jar = aiohttp.CookieJar(unsafe=True)
    return _cookie_jar


async def ensure_auth(session: aiohttp.ClientSession) -> bool:
    """Authenticate with Phone Connect if we haven't already."""
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
    except Exception as e:
        print(f"⚠️ Phone Connect auth failed: {e}")
        return False


async def pc_request(method: str, path: str, json_data: dict = None) -> dict:
    """Make an authenticated request to Phone Connect."""
    jar = get_cookie_jar()
    ssl_ctx = _make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else None
    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as session:
        await ensure_auth(session)

        url = f"{PHONE_CONNECT_URL}{path}"
        headers = {
            "Content-Type": "application/json",
            "ngrok-skip-browser-warning": "true",
        }

        try:
            if method == "GET":
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 401:
                        global _authenticated
                        _authenticated = False
                        raise HTTPException(status_code=502, detail="Phone Connect authentication lost")
                    return await resp.json()
            else:
                async with session.post(url, json=json_data or {}, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 401:
                        _authenticated = False
                        raise HTTPException(status_code=502, detail="Phone Connect authentication lost")
                    return await resp.json()
        except aiohttp.ClientError as e:
            raise HTTPException(status_code=502, detail=f"Phone Connect unreachable: {e}")


# ---------- Pydantic models ----------

class SendMessageRequest(BaseModel):
    text: str

class SetModeRequest(BaseModel):
    mode: str

class SetModelRequest(BaseModel):
    model: str


# ---------- Endpoints ----------

@app.get("/health")
async def health():
    try:
        pc_health = await pc_request("GET", "/health")
        return {"status": "ok", "phone_connect": pc_health}
    except Exception as e:
        return {"status": "degraded", "phone_connect_error": str(e)}


@app.post("/send_message")
async def send_message(req: SendMessageRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty text")
    result = await pc_request("POST", "/send", {"message": req.text.strip()})
    return {"status": "ok", "result": result}


@app.get("/snapshot")
async def get_snapshot():
    return await pc_request("GET", "/snapshot")


@app.get("/snapshot/text")
async def get_snapshot_text():
    raw = await pc_request("GET", "/snapshot")
    html = raw.get("html", "")
    if not html:
        return {"messages": [], "raw_length": 0}
    messages = parse_messages_from_html(html)
    return {"messages": messages, "count": len(messages), "raw_length": len(html)}


@app.get("/app-state")
async def get_app_state():
    return await pc_request("GET", "/app-state")


@app.post("/set-mode")
async def set_mode(req: SetModeRequest):
    if req.mode not in ("Fast", "Planning"):
        raise HTTPException(status_code=400, detail="Mode must be 'Fast' or 'Planning'")
    return await pc_request("POST", "/set-mode", {"mode": req.mode})


@app.post("/set-model")
async def set_model(req: SetModelRequest):
    return await pc_request("POST", "/set-model", {"model": req.model})

@app.get("/models")
async def get_available_models():
    """Get list of available models from Antigravity via CDP dropdown scraping."""
    return await pc_request("GET", "/models")

@app.post("/stop")
async def stop_generation():
    return await pc_request("POST", "/stop")


@app.post("/new-chat")
async def new_chat():
    return await pc_request("POST", "/new-chat")


@app.get("/chat-history")
async def get_chat_history():
    return await pc_request("GET", "/chat-history")


@app.post("/init")
async def init_session():
    try:
        result = await pc_request("POST", "/send", {"message": os.getenv("PHONE_INIT_MESSAGE", "init session")})
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Init error: {e}")


# ---------- HTML parsing helper ----------

# Regex patterns compiled once at module level for performance

# Patterns to remove entire blocks (tag + content)
_RE_REMOVE_BLOCKS = re.compile(
    r'<(?:style|script|noscript|template|svg|canvas)[^>]*>.*?</(?:style|script|noscript|template|svg|canvas)>',
    re.DOTALL | re.IGNORECASE,
)

# xterm / terminal containers — match the opening tag through its closing tag
# Antigravity uses xterm.js for terminal output; these elements contain raw
# escape sequences, ANSI codes, rows of styled spans, etc.
_RE_XTERM_BLOCKS = re.compile(
    r'<div[^>]*class="[^"]*(?:xterm|terminal)[^"]*"[^>]*>.*?</div>',
    re.DOTALL | re.IGNORECASE,
)

# Code editor containers (CodeMirror / Monaco)
_RE_EDITOR_BLOCKS = re.compile(
    r'<div[^>]*class="[^"]*(?:cm-editor|cm-content|cm-gutters|monaco-editor|editor-container)[^"]*"[^>]*>.*?</div>',
    re.DOTALL | re.IGNORECASE,
)

# Inline CSS rules that leak into text (e.g. "body { ... }" or ".class { ... }")
_RE_CSS_RULES = re.compile(
    r'(?:^|\n)\s*(?:[\w\-.*#@:>\[\]=~^|,\s]+)\s*\{[^}]*\}',
    re.MULTILINE,
)

# ANSI / terminal escape sequences
_RE_ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

# Repeated special characters that indicate decorative lines / progress bars
_RE_DECORATIVE_LINES = re.compile(r'[─━═▓░▒█▄▀]{4,}')

# Git command output noise patterns
_RE_GIT_NOISE = re.compile(
    r'(?:^|\n)\s*(?:'
    r'diff --git\b|index [0-9a-f]+\.\.[0-9a-f]+|'
    r'--- a/|'
    r'\+\+\+ b/|'
    r'@@ .+? @@|'
    r'commit [0-9a-f]{7,40}\b|'
    r'Author:\s|'
    r'Date:\s|'
    r'Merge:\s|'
    r'(?:modified|deleted|renamed|new file):\s+'
    r').*',
    re.MULTILINE,
)

# npm / shell progress noise
_RE_SHELL_NOISE = re.compile(
    r'(?:^|\n)\s*(?:'
    r'npm\s+(?:warn|info|notice|WARN|ERR!)\b|'
    r'\$\s+\S|'
    r'>\s+\S+@\S+\s|'
    r'added \d+ packages?\b|'
    r'found \d+ vulnerabilities?\b|'
    r'up to date\b|'
    r'✓|✗|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏'
    r').*',
    re.MULTILINE,
)

# Strip HTML tags
_RE_TAGS = re.compile(r'<[^>]+>')

# Collapse whitespace
_RE_MULTI_NEWLINES = re.compile(r'\n{3,}')
_RE_MULTI_SPACES = re.compile(r'[ \t]{2,}')


def _strip_noise(text: str) -> str:
    """Remove technical garbage from extracted text."""
    text = _RE_ANSI.sub('', text)
    text = _RE_CSS_RULES.sub('', text)
    text = _RE_GIT_NOISE.sub('', text)
    text = _RE_SHELL_NOISE.sub('', text)
    text = _RE_DECORATIVE_LINES.sub('', text)
    return text


def parse_messages_from_html(html: str) -> list[dict]:
    """Parse snapshot HTML into clean message objects.

    The HTML comes from Antigravity's chat panel via CDP captureSnapshot.
    It may contain terminal output, xterm elements, CSS rules, git diffs,
    progress bars, and other UI noise that must be filtered out.
    """
    messages = []

    # Phase 1: Remove entire noisy DOM blocks
    cleaned = _RE_REMOVE_BLOCKS.sub('', html)
    cleaned = _RE_XTERM_BLOCKS.sub('', cleaned)
    cleaned = _RE_EDITOR_BLOCKS.sub('', cleaned)

    # Phase 2: Try to split by message blocks (data-message or class=message)
    blocks = re.split(r'(?=<div[^>]*(?:data-message|class="[^"]*message)[^>]*>)', cleaned)

    if len(blocks) <= 1:
        # No message containers found — fallback to text extraction
        text = _RE_TAGS.sub('\n', cleaned)
        text = _strip_noise(text)
        text = _RE_MULTI_SPACES.sub(' ', text)
        text = _RE_MULTI_NEWLINES.sub('\n\n', text).strip()

        # Split into logical chunks by double newlines
        lines = [l.strip() for l in text.split('\n') if l.strip() and len(l.strip()) > 2]
        if lines:
            current_block = []
            for line in lines:
                # Skip lines that look like CSS or technical noise
                if _is_noise_line(line):
                    continue
                current_block.append(line)
                if len('\n'.join(current_block)) > 200:
                    text_content = '\n'.join(current_block)
                    messages.append({
                        "role": "unknown",
                        "text": text_content[:2000],
                        "hash": hashlib.md5(text_content.encode()).hexdigest()[:8],
                    })
                    current_block = []
            if current_block:
                text_content = '\n'.join(current_block)
                if len(text_content.strip()) > 5:
                    messages.append({
                        "role": "unknown",
                        "text": text_content[:2000],
                        "hash": hashlib.md5(text_content.encode()).hexdigest()[:8],
                    })
    else:
        for block in blocks[1:]:
            # Remove nested noisy elements within each block
            block_cleaned = _RE_XTERM_BLOCKS.sub('', block)
            block_cleaned = _RE_EDITOR_BLOCKS.sub('', block_cleaned)

            text = _RE_TAGS.sub(' ', block_cleaned)
            text = _strip_noise(text)
            text = _RE_MULTI_SPACES.sub(' ', text).strip()

            if len(text) < 5:
                continue

            # Skip blocks that are entirely noise
            if _is_noise_block(text):
                continue

            role = "assistant"
            if 'user' in block.lower()[:200]:
                role = "user"

            messages.append({
                "role": role,
                "text": text[:2000],
                "hash": hashlib.md5(text.encode()).hexdigest()[:8],
            })

    return messages


def _is_noise_line(line: str) -> bool:
    """Check if a single line looks like technical noise."""
    stripped = line.strip()

    # Very short lines are usually artifacts
    if len(stripped) < 3:
        return True

    # CSS-like patterns
    if re.match(r'^[\w\-.*#@:>\[\]=~^|,\s]+\s*\{', stripped):
        return True
    if stripped.endswith('}') and '{' not in stripped:
        return True

    # CSS property lines
    if re.match(r'^\s*[\w-]+\s*:\s*[^;]+;\s*$', stripped):
        return True

    # Terminal prompt or command patterns
    if re.match(r'^\s*[\$#>]\s+', stripped):
        return True

    # Hex color codes / CSS values standing alone
    if re.match(r'^(?:#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)|var\(--[^)]+\))\s*;?\s*$', stripped):
        return True

    # File paths standing alone (not in sentence context)
    if re.match(r'^(?:/[\w\-./]+|\.{1,2}/[\w\-./]+)\s*$', stripped):
        return True

    # Repeated dots / dashes / underscores (progress indicators)
    if re.match(r'^[.\-_=]{5,}$', stripped):
        return True

    return False


def _is_noise_block(text: str) -> bool:
    """Check if an entire text block is technical noise."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return True

    noise_count = sum(1 for l in lines if _is_noise_line(l))

    # If more than 70% of lines are noise, skip the whole block
    if len(lines) > 0 and noise_count / len(lines) > 0.7:
        return True

    return False

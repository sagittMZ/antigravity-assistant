"""
phone_worker.py — FastAPI gateway to Phone Connect REST API.

Proxies requests to the Phone Connect Node.js server and provides
a clean /snapshot/text endpoint that extracts meaningful chat messages
from raw HTML snapshots of the Antigravity IDE.

Endpoints:
    GET  /health         Health check (including Phone Connect status).
    POST /send_message   Forward a prompt to Antigravity.
    GET  /snapshot        Raw snapshot from Phone Connect.
    GET  /snapshot/text   Parsed messages (filtered, clean text).
    GET  /app-state       Current mode/model from Antigravity.
    POST /set-mode        Switch Fast / Planning.
    POST /set-model       Switch AI model.
    GET  /models          Available models list.
    POST /stop            Stop current generation.
    POST /new-chat        Start a new conversation.
    GET  /chat-history    Conversation history.
    POST /init            Send an initial "wake-up" message.
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

PHONE_CONNECT_URL = os.getenv(
    "PHONE_CONNECT_LOCAL_URL", "http://127.0.0.1:3000"
).rstrip("/")
PHONE_CONNECT_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

app = FastAPI(title="Phone Worker — Antigravity Assistant")

_cookie_jar: Optional[aiohttp.CookieJar] = None
_authenticated = False


# ---------------------------------------------------------------------------
# HTTP transport helpers
# ---------------------------------------------------------------------------

def _make_ssl_context() -> Optional[_ssl.SSLContext]:
    """Permissive SSL context for local self-signed certs."""
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
    """Authenticate with Phone Connect if not already authenticated."""
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
    """Authenticated request to Phone Connect."""
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


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    text: str

class SetModeRequest(BaseModel):
    mode: str

class SetModelRequest(BaseModel):
    model: str


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

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
    """Available models from Antigravity UI dropdown."""
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


# ---------------------------------------------------------------------------
# HTML → clean messages parser
# ---------------------------------------------------------------------------
#
# The snapshot HTML is a raw clone of Antigravity's chat panel DOM.
# It contains:
#   - Actual user prompts and assistant final answers  (KEEP)
#   - Thinking / reasoning steps ("Thought for 10s", "Prioritizing…")  (DROP)
#   - UI chrome buttons ("Always run", "Cancel", "Collapse all", etc.)  (DROP)
#   - Terminal / xterm output blocks                                    (DROP)
#   - Code editor blocks (CodeMirror / Monaco)                          (DROP)
#   - CSS / style blocks                                                (DROP)
#   - Progress bars, ANSI sequences, git diff headers                   (DROP)
#
# Strategy:
#   1. Strip entire noisy DOM subtrees (<style>, <script>, xterm, editor).
#   2. Replace tags with SPACE (not newline!) to join adjacent <span>s.
#   3. Apply aggressive text-level noise filters.
#   4. Only keep blocks that look like real conversation content.

# -- Compiled regexes (module level, compiled once) --

# Entire tag+content blocks to nuke
_RE_NUKE_BLOCKS = re.compile(
    r'<(?:style|script|noscript|template|svg|canvas)[^>]*>.*?'
    r'</(?:style|script|noscript|template|svg|canvas)>',
    re.DOTALL | re.IGNORECASE,
)

_RE_XTERM = re.compile(
    r'<div[^>]*class="[^"]*(?:xterm|terminal)[^"]*"[^>]*>.*?</div>',
    re.DOTALL | re.IGNORECASE,
)

_RE_EDITOR = re.compile(
    r'<div[^>]*class="[^"]*(?:cm-editor|cm-content|cm-gutters|'
    r'monaco-editor|editor-container)[^"]*"[^>]*>.*?</div>',
    re.DOTALL | re.IGNORECASE,
)

# Block-level tags → newline (preserves paragraph structure)
_RE_BLOCK_TAGS = re.compile(
    r'</?(?:div|p|br|h[1-6]|li|ul|ol|tr|blockquote|section|article|'
    r'header|footer|details|summary|pre|hr)[^>]*>',
    re.IGNORECASE,
)

# Remaining HTML tags → single space (keeps words on the same line)
_RE_TAGS = re.compile(r'<[^>]+>')

# Collapse runs of whitespace
_RE_WS = re.compile(r'[ \t]+')
_RE_MULTI_NL = re.compile(r'\n{3,}')

# HTML entities
_RE_ENTITY = re.compile(r'&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);')

# ANSI escape sequences
_RE_ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

# CSS rules that leaked into text
_RE_CSS = re.compile(
    r'(?:^|\n)\s*[\w\-.*#@:>\[\]=~^|,\s]+\s*\{[^}]*\}',
    re.MULTILINE,
)

# ---------- Antigravity UI noise ----------

# Exact short phrases that are UI buttons / labels
_UI_CHROME: set[str] = {
    # Buttons
    "always run", "cancel", "collapse all", "expand all",
    "open", "proceed", "copy", "retry", "undo",
    # Status labels
    "running command", "running...", "running", "ran command",
    "relocate", "exit code 0", "exit code 1", "exit code 2",
    "background steps", "progress updates", "files edited",
    "generating...", "generated", "thinking..", "thinking...",
    "created task", "edited task", "created", "edited",
    # Analysis status labels
    "analyzed implementation plan", "analyzed task",
    "analyzed walkthrough", "analyzed conversation",
    "error while analyzing directory", "cannot list directory",
    "conversation sections", "which does not exist.",
    "started investigating", "finding conversation logs",
    "checking older artifacts",
    # Additional UI labels from real usage
    "stage:",
}

# Thinking / reasoning step patterns (regex)
# These are the "Thought for Xs" bubbles and the internal reasoning
# lines Antigravity shows while working.  The gerund-phrase pattern
# catches the vast majority of them ("Assessing X", "Evaluating Y").
_RE_THINKING_STEPS = re.compile(
    r'^\s*(?:'
    r'Thought for \d+\s*s'
    # Gerund-phrase reasoning lines ("Analyzing X", "Evaluating Y")
    r'|(?:Prioritizing|Refining|Locating|Exploring|Investigating'
    r'|Reviewing|Accessing|Pinpointing|Analyzing|Assessing'
    r'|Evaluating|Synthesizing|Researching|Checking|Reading'
    r'|Determining|Clarifying|Considering|Identifying|Verifying'
    r'|Examining|Inspecting|Gathering|Mapping|Tracing|Scanning'
    r'|Comparing|Compiling|Summarizing|Integrating|Navigating'
    r'|Interpreting|Formulating|Structuring|Consolidating'
    r'|Confirming|Validating|Understanding|Preparing|Outlining'
    r'|Planning|Drafting|Generating|Processing|Calculating'
    r'|Fetching|Retrieving|Correlating|Decomposing|Parsing'
    r'|Contextualizing|Cross-referencing|Deliberating'
    r'|Diagnosing|Hypothesizing|Inferring|Iterating'
    r'|Cataloging|Benchmarking|Profiling|Auditing) [A-Z].*'
    # "I'm now ..." self-narration
    r'|I\'m (?:now |currently )?(?:prioritizing|refining|focusing'
    r'|focused|exploring|investigating|recalling|leveraging|zeroing'
    r'|examining|analyzing|assessing|evaluating|synthesizing'
    r'|researching|checking|reading|diving|looking|working'
    r'|reviewing|determining|trying|going|starting|moving'
    r'|proceeding|continuing|considering|comparing|providing'
    r'|addressing|tackling|handling|preparing|building'
    r'|implementing|creating|developing|writing|updating'
    r'|finalizing|completing|wrapping|summarizing|compiling)'
    # "I've made/analyzed/found..." progress narration
    r'|I\'ve (?:made|analyzed|found|completed|finished|checked'
    r'|verified|reviewed|confirmed|identified|gathered'
    r'|synthesized|evaluated|determined|established) '
    # Numbered progress items ("1 Synthesizing readiness status...")
    r'|\d+\s+(?:Synthesizing|Checking|Researching|Reading'
    r'|Analyzing|Verifying|Reviewing|Evaluating|Gathering'
    r'|Identifying|Mapping|Processing|Fetching|Scanning'
    r'|Comparing|Finding|Determining|Investigating) '
    # SAME% / percentage markers from progress bars
    r'|SAME%'
    r'|\d{1,3}%'
    r').*$',
    re.MULTILINE,
)

# Short fragments that are clearly not real content
# (e.g. single word from UI: "Function),", "Web", "Push),")
_RE_FRAGMENT = re.compile(r'^[A-Za-z]+[),.:;]+$')

# Git / terminal / shell noise
_RE_GIT = re.compile(
    r'(?:^|\n)\s*(?:'
    r'diff --git\b|index [0-9a-f]+\.\.[0-9a-f]+|'
    r'--- a/|'
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


def _decode_entities(text: str) -> str:
    """Minimal HTML entity decode."""
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")
    # Drop any remaining entities
    text = _RE_ENTITY.sub("", text)
    return text


def _clean_text(raw: str) -> str:
    """Apply all text-level noise filters."""
    text = _RE_ANSI.sub("", raw)
    text = _RE_CSS.sub("", text)
    text = _RE_GIT.sub("", text)
    text = _RE_SHELL.sub("", text)
    text = _RE_DECORATIVE.sub("", text)

    # Collapse inline whitespace BEFORE line-based filters so that
    # regexes like _RE_THINKING_STEPS can match "Assessing AI" even
    # when the HTML produced "Assessing   AI" (multiple spaces from
    # adjacent <span> tags).
    text = _RE_WS.sub(" ", text)

    # Now apply thinking-step regex (needs single-space text)
    text = _RE_THINKING_STEPS.sub("", text)

    # Remove UI chrome lines and other noise
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if stripped.lower() in _UI_CHROME:
            continue
        # Standalone path like ~/…/crewtask-v2
        if re.match(r'^~?[/\\]', stripped) and len(stripped) < 80:
            continue
        # CSS property lines
        if re.match(r'^\s*[\w-]+\s*:\s*[^;]+;\s*$', stripped):
            continue
        # Shell prompts
        if re.match(r'^\s*[$#>]\s+', stripped):
            continue
        # Very short junk fragments (single word + punctuation)
        if len(stripped) < 4 and _RE_FRAGMENT.match(stripped):
            continue
        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    text = _RE_MULTI_NL.sub("\n\n", text)
    return text.strip()


def _is_meaningful(text: str) -> bool:
    """Return True if the text block is likely a real chat message."""
    if len(text) < 10:
        return False
    # Count words: real messages have at least a few
    words = text.split()
    if len(words) < 4:
        return False
    # If mostly single-char tokens → noise
    short_tokens = sum(1 for w in words if len(w) <= 2)
    if len(words) > 0 and short_tokens / len(words) > 0.6:
        return False
    return True


def _stable_hash(text: str) -> str:
    """Hash based on the first 200 chars of cleaned text.

    While AI streams a response, the text block grows but the beginning
    stays the same.  Using only the prefix for hashing means a growing
    message keeps the same hash across snapshots, preventing duplicates
    in Telegram during streaming.
    """
    prefix = text[:200].strip()
    return hashlib.md5(prefix.encode()).hexdigest()[:8]


def parse_messages_from_html(html: str) -> list[dict]:
    """Extract clean chat messages from Antigravity snapshot HTML.

    Returns a list of dicts: {"role": str, "text": str, "hash": str}.
    """
    messages: list[dict] = []

    # Phase 1: strip noisy DOM subtrees
    cleaned = _RE_NUKE_BLOCKS.sub("", html)
    cleaned = _RE_XTERM.sub("", cleaned)
    cleaned = _RE_EDITOR.sub("", cleaned)

    # Phase 2: try to split by message containers
    blocks = re.split(
        r'(?=<div[^>]*(?:data-message|class="[^"]*message)[^>]*>)',
        cleaned,
    )

    if len(blocks) <= 1:
        # Fallback: treat the whole HTML as one block
        text = _RE_BLOCK_TAGS.sub("\n", cleaned)
        text = _RE_TAGS.sub(" ", text)
        text = _decode_entities(text)
        text = _clean_text(text)
        if _is_meaningful(text):
            messages.append({
                "role": "unknown",
                "text": text[:2000],
                "hash": _stable_hash(text),
            })
    else:
        for block in blocks[1:]:
            # Remove nested noisy subtrees
            block = _RE_XTERM.sub("", block)
            block = _RE_EDITOR.sub("", block)

            # Block-level tags → newlines (paragraph structure)
            # Inline tags → spaces (keeps words on same line)
            text = _RE_BLOCK_TAGS.sub("\n", block)
            text = _RE_TAGS.sub(" ", text)
            text = _decode_entities(text)
            text = _clean_text(text)

            if not _is_meaningful(text):
                continue

            role = "assistant"
            if "user" in block.lower()[:200]:
                role = "user"

            messages.append({
                "role": role,
                "text": text[:2000],
                "hash": _stable_hash(text),
            })

    return messages

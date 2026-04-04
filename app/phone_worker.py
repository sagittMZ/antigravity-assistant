from __future__ import annotations

import asyncio
import os
import re
import hashlib
from pathlib import Path
from typing import Optional, Any, cast, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import aiohttp
import ssl as _ssl

from app.logger import setup_logger

try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    raise RuntimeError("BeautifulSoup is missing. Run: pip install beautifulsoup4 lxml")

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
_auth_lock = asyncio.Lock()


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

    async with _auth_lock:
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
                if isinstance(data, dict) and data.get("success"):
                    _authenticated = True
                    pw_log.info("Authenticated against Phone Connect.")
                    return True
                pw_log.warning(f"Phone Connect auth rejected: {data}")
                return False
        except Exception as exc:
            pw_log.warning(f"Phone Connect auth failed: {exc}")
            return False


# NOTE: Dict[str, Any] instead of dict[str, Any] — Python 3.8 compatibility.
# dict[str, Any] as a generic alias is only supported from Python 3.9+.
# FastAPI introspects return type annotations at import time via Pydantic,
# which triggers the 'type object is not subscriptable' error on 3.8.
async def _pc_request(
    method: str,
    path: str,
    json_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
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
            if method == "GET":
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 401:
                        global _authenticated
                        _authenticated = False
                        raise HTTPException(502, "Phone Connect authentication lost")
                    res = await resp.json()
                    return cast(Dict[str, Any], res)
            else:
                payload = json_data if json_data is not None else {}
                async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status == 401:
                        _authenticated = False
                        raise HTTPException(502, "Phone Connect authentication lost")
                    res = await resp.json()
                    return cast(Dict[str, Any], res)
        except aiohttp.ClientError as exc:
            raise HTTPException(502, f"Phone Connect unreachable: {exc}")


class SendMessageRequest(BaseModel):
    text: str

class SetModeRequest(BaseModel):
    mode: str

class SetModelRequest(BaseModel):
    model: str


@app.get("/health")
async def health() -> Dict[str, Any]:
    try:
        pc = await _pc_request("GET", "/health")
        return {"status": "ok", "phone_connect": pc}
    except Exception as exc:
        return {"status": "degraded", "phone_connect_error": str(exc)}

@app.post("/send_message")
async def send_message(req: SendMessageRequest) -> Dict[str, Any]:
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Empty text")
    result = await _pc_request("POST", "/send", {"message": text})
    return {"status": "ok", "result": result}

@app.get("/snapshot")
async def get_snapshot() -> Dict[str, Any]:
    return await _pc_request("GET", "/snapshot")

@app.get("/snapshot/hash")
async def get_snapshot_hash_fast() -> Dict[str, Any]:
    jar = _get_cookie_jar()
    ssl_ctx = _make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else None

    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as session:
        await _ensure_auth(session)
        url = f"{PHONE_CONNECT_URL}/snapshot"
        headers = {"ngrok-skip-browser-warning": "true"}

        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 401:
                    global _authenticated
                    _authenticated = False
                    raise HTTPException(502, "Phone Connect authentication lost")

                hasher = hashlib.md5()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    hasher.update(chunk)
                return {"hash": hasher.hexdigest()}
        except Exception as exc:
            raise HTTPException(502, f"Phone Connect unreachable: {exc}")

@app.get("/snapshot/text")
async def get_snapshot_text() -> Dict[str, Any]:
    raw = await _pc_request("GET", "/snapshot")
    html = str(raw.get("html", ""))
    if not html:
        return {"messages": [], "raw_length": 0}
    messages = parse_messages_from_html(html)
    return {"messages": messages, "count": len(messages), "raw_length": len(html)}

@app.get("/app-state")
async def get_app_state() -> Dict[str, Any]:
    return await _pc_request("GET", "/app-state")

@app.get("/is-generating")
async def is_generating() -> Dict[str, Any]:
    """
    Returns {"generating": bool} — True when Antigravity is mid-generation.
    Used by tg_bot.py to gate final response delivery in collect-and-hold mode.
    Falls back to False on any error so polling can still make progress.
    """
    try:
        status = await _pc_request("GET", "/chat-status")
        has_chat = status.get("hasChat", False)
        editor_found = status.get("editorFound", True)
        if has_chat and not editor_found:
            return {"generating": True, "source": "editor_locked"}
        return {"generating": False, "source": "editor_available"}
    except Exception as exc:
        pw_log.debug(f"is-generating fallback: {exc}")
        return {"generating": False, "source": "error_fallback"}


@app.post("/set-mode")
async def set_mode(req: SetModeRequest) -> Dict[str, Any]:
    if req.mode not in ("Fast", "Planning"):
        raise HTTPException(400, "Mode must be 'Fast' or 'Planning'")
    return await _pc_request("POST", "/set-mode", {"mode": req.mode})

@app.post("/set-model")
async def set_model(req: SetModelRequest) -> Dict[str, Any]:
    return await _pc_request("POST", "/set-model", {"model": req.model})

@app.get("/models")
async def get_available_models() -> Dict[str, Any]:
    return await _pc_request("GET", "/models")

@app.post("/stop")
async def stop_generation() -> Dict[str, Any]:
    return await _pc_request("POST", "/stop")

@app.post("/new-chat")
async def new_chat() -> Dict[str, Any]:
    return await _pc_request("POST", "/new-chat")

@app.get("/chat-history")
async def get_chat_history() -> Dict[str, Any]:
    return await _pc_request("GET", "/chat-history")

@app.post("/init")
async def init_session() -> Dict[str, Any]:
    try:
        result = await _pc_request(
            "POST", "/send",
            {"message": os.getenv("PHONE_INIT_MESSAGE", "init session")},
        )
        return {"status": "ok", "result": result}
    except Exception as exc:
        raise HTTPException(500, f"Init error: {exc}")


# ---------------------------------------------------------------------------
# Text detection and cleaning
# ---------------------------------------------------------------------------

_RE_WS        = re.compile(r"[ \t]+")
_RE_MULTI_NL  = re.compile(r"\n{3,}")
_RE_ANSI      = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_CSS       = re.compile(r"(?:^|\n)\s*[\w\-.*#@:>\[\]=~^|,\s]+\s*\{[^}]*\}", re.MULTILINE)
_RE_FRAGMENT  = re.compile(r"^[A-Za-z]+[),.:;]+$")

_RE_GIT = re.compile(
    r"(?:^|\n)\s*(?:diff --git\b|index [0-9a-f]+\.\.[0-9a-f]+|"
    r"--- a/|\+\+\+ b/|@@ .+? @@|commit [0-9a-f]{7,40}\b|"
    r"Author:\s|Date:\s|Merge:\s|(?:modified|deleted|renamed|new file):\s+).*",
    re.MULTILINE,
)
_RE_SHELL = re.compile(
    r"(?:^|\n)\s*(?:npm\s+(?:warn|info|notice|WARN|ERR!)\b|\$\s+\S|>\s+\S+@\S+\s|"
    r"added \d+ packages?\b|found \d+ vulnerabilities?\b|up to date\b|"
    r"[✓✗⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]).*",
    re.MULTILINE,
)
_RE_DECORATIVE   = re.compile(r"[─━═▓░▒█▄▀]{4,}")
_RE_AGENT_HEADER = re.compile(
    r"\[\d{1,2}/\d{1,2}/\d{2,4}[^\]]+\][^:]+:\s*(?:🤖\s*)?", re.IGNORECASE
)
_RE_SYS_LOGS = re.compile(
    r"(\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\b)", re.IGNORECASE
)

_UI_CHROME: set = {
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

# FIX: CSS class patterns for thinking/reasoning container blocks.
_THINKING_CLASS_RE = re.compile(
    r"thinking|reasoning|collapsible|expandable|thought|planning-step|inner-monologue",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# FIX: Antigravity response formatter
# Strips system/tool noise emitted during generation, extracts "Thought for Xs"
# step headers as a compact italic summary, and restores paragraph structure.
# Root bug: Antigravity DOM collapses everything to one line; noise patterns
# like "Relocate open_in_new ... Thought for" consumed subsequent Thought blocks.
# ---------------------------------------------------------------------------

_RE_AG_NOISE = re.compile(
    r'(?:'
    r'Worked for \d+[^\s]*(?:\s\w+)?'                              # "Worked for 25s"
    r'|Explored \d+[^T]*?(?=Thought|Analyzed|Ran|$)'                  # "Explored 6 files"
    r'|Analyzed \S+(?:\s+(?!Analyzed|Thought|Ran|Checked|Exit|Always|Relocate)\S+){0,4}(?:\s+#L[\d-]+)?'
    r'|Ran background command'
    r'|Checked command status'
    r'|Relocate open_in_new.*?(?=Thought for|\Z)'     # stop before next Thought block
    r'|content_copy|Always run'
    r'|Exit code \d+'
    r'|undo\b|open_in_new)',
    re.IGNORECASE | re.DOTALL,
)

# "Thought for Xs Title Case Header" — stop at first-person pronoun.
_RE_AG_THOUGHT = re.compile(
    r'Thought for (\d+)s?\s+([A-Z][^.!?]+?)(?=\s{1,3}(?:I\'ve|I\'m|I |My |The |Now |It ))',
)

_RE_AG_NUMBERED = re.compile(r'(?<=[а-яёА-ЯЁa-zA-Z.!? ])\s+(\d{1,2}\.\s+[А-ЯЁA-Z])')
_RE_AG_PRIORITY = re.compile(r'(?<=[.!?а-яёА-ЯЁ])\s+(P[1-5]:|QA:|Stage:)')
_RE_AG_CYRILLIC = re.compile(r'[А-ЯЁ][а-яёА-ЯЁ]')


def format_ag_response(raw: str) -> str:
    """
    Format an Antigravity agent response for clean Telegram delivery.

    1. Remove system/tool noise tokens.
    2. Extract "Thought for Xs <Title>" headers → compact italic chain.
    3. Locate final answer start (first Cyrillic capital).
    4. Restore numbered-list and priority-label line breaks.

    Safe to call on any string; returns input unchanged when no patterns match.
    """
    text = _RE_AG_NOISE.sub(' ', raw)
    text = re.sub(r'[ \t]{2,}', ' ', text).strip()

    thought_headers: List[str] = []
    spans: List[Any] = []
    for m in _RE_AG_THOUGHT.finditer(text):
        thought_headers.append(m.group(2).strip())
        spans.append((m.start(), m.end()))
    for start, end in reversed(spans):
        text = text[:start] + ' ' + text[end:]
    text = re.sub(r'[ \t]{2,}', ' ', text).strip()

    cy = _RE_AG_CYRILLIC.search(text)
    final = text[cy.start():].strip() if cy else text.strip()

    final = _RE_AG_NUMBERED.sub(r'\n\n\1', final)
    final = _RE_AG_PRIORITY.sub(r'\n\1', final)
    final = re.sub(r'\n{3,}', '\n\n', final).strip()

    parts: List[str] = []
    if thought_headers:
        parts.append(f"_💭 {' → '.join(thought_headers)}_\n")
    if final:
        parts.append(final)

    return "\n".join(parts)


def _is_word_waterfall(text: str) -> bool:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 10:
        return False
    short = sum(1 for l in lines if len(l.split()) <= 2)
    return (short / len(lines)) > 0.60


def _collapse_waterfall(text: str) -> str:
    lines = text.split("\n")
    paragraphs: List[List[str]] = [[]]

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraphs[-1]:
                paragraphs.append([])
        else:
            paragraphs[-1].append(stripped)

    parts = [" ".join(para) for para in paragraphs if para]
    return "\n\n".join(parts)


def _extract_text_smart(tag: Any) -> str:
    text = str(tag.get_text(separator="\n", strip=True))
    if _is_word_waterfall(text):
        text = _collapse_waterfall(text)
    return text


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
    cleaned: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append("")
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
        cleaned.append(line)

    text = "\n".join(cleaned)
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
    # FIX: hash full text instead of first 200 chars.
    # Prefix-only caused false collisions when the thinking block grew beyond
    # 200 chars: the prefix stayed identical, so the final answer appended at
    # the end was silently ignored as a duplicate.
    return hashlib.md5(text.strip().encode()).hexdigest()[:8]


def _is_thinking_block(tag: Any) -> bool:
    """Return True when the DOM element is a thinking/reasoning container."""
    classes = " ".join(_safe_get_classes(tag))
    if _THINKING_CLASS_RE.search(classes):
        return True
    aria = str(tag.get("aria-label", "")).lower()
    return "thinking" in aria or "reasoning" in aria


def _safe_get_classes(tag: Any) -> List[str]:
    cls_attr = tag.get("class")
    if isinstance(cls_attr, list):
        return [str(c) for c in cls_attr]
    if isinstance(cls_attr, str):
        return [cls_attr]
    return []


# ---------------------------------------------------------------------------
# DOM parsing
# ---------------------------------------------------------------------------

def parse_messages_from_html(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["style", "script", "noscript", "template", "svg", "canvas"]):
        tag.decompose()

    for tag in soup.find_all(
        class_=re.compile(r"xterm|terminal|cm-editor|cm-content|monaco-editor", re.I)
    ):
        tag.decompose()

    # FIX: remove thinking containers before parsing to prevent intermediate
    # reasoning steps being delivered to Telegram as assistant messages.
    for tag in soup.find_all(lambda t: t.name == "div" and _is_thinking_block(t)):
        tag.decompose()

    messages: List[Dict[str, str]] = []

    blocks = soup.find_all(
        lambda t: t.name == "div"
        and (
            t.has_attr("data-message")
            or any("message" in c.lower() for c in _safe_get_classes(t))
        )
    )

    if not blocks:
        text = _extract_text_smart(soup)
        if len(text) > 15000:
            text = text[:15000] + "\n\n...[TRUNCATED]..."
        text = _clean_text(text)
        if _is_meaningful(text):
            return [{"role": "assistant", "text": text, "hash": _stable_hash(text)}]
        return []

    for block in blocks:
        text = _extract_text_smart(block)
        if len(text) > 15000:
            text = text[:15000] + "\n\n...[TRUNCATED]..."

        text = _clean_text(text)
        if not _is_meaningful(text):
            continue

        role = "assistant"
        classes = " ".join(_safe_get_classes(block)).lower()
        if "user" in classes:
            role = "user"

        messages.append({"role": role, "text": text, "hash": _stable_hash(text)})

    return messages
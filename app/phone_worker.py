"""
phone_worker.py — FastAPI gateway to Phone Connect REST API.

Instead of launching a headless browser, this module calls the
Phone Connect HTTP endpoints directly (the Node.js server exposes
/send, /snapshot, /health, /set-mode, /set-model, /stop,
/new-chat, /chat-history, /app-state).

This keeps the architecture simpler and more reliable.
"""

import os
import re
import hashlib
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import aiohttp

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

# Phone Connect local URL (the Node.js server, NOT the ngrok tunnel)
PHONE_CONNECT_URL = os.getenv("PHONE_CONNECT_LOCAL_URL", "http://127.0.0.1:3000").rstrip("/")
# Password for Phone Connect auth
PHONE_CONNECT_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

app = FastAPI(title="Phone Worker — Antigravity Assistant")

# Session cookie jar to persist auth between calls
_cookie_jar: Optional[aiohttp.CookieJar] = None
_authenticated = False


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
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
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

def parse_messages_from_html(html: str) -> list[dict]:
    messages = []
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    blocks = re.split(r'(?=<div[^>]*(?:data-message|class="[^"]*message)[^>]*>)', html)

    if len(blocks) <= 1:
        cleaned = re.sub(r'<[^>]+>', '\n', html)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        lines = [l.strip() for l in cleaned.split('\n') if l.strip() and len(l.strip()) > 2]
        if lines:
            current_block = []
            for line in lines:
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
                messages.append({
                    "role": "unknown",
                    "text": text_content[:2000],
                    "hash": hashlib.md5(text_content.encode()).hexdigest()[:8],
                })
    else:
        for block in blocks[1:]:
            cleaned = re.sub(r'<[^>]+>', ' ', block)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            if len(cleaned) < 5:
                continue
            role = "assistant"
            if 'user' in block.lower()[:200]:
                role = "user"
            messages.append({
                "role": role,
                "text": cleaned[:2000],
                "hash": hashlib.md5(cleaned.encode()).hexdigest()[:8],
            })

    return messages

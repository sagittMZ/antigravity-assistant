import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from playwright.sync_api import Playwright, sync_playwright, Page, Browser

BASE_DIR = Path(__file__).parent.parent  # project root
load_dotenv(BASE_DIR / ".env")

PHONE_CONNECT_URL = os.getenv("PHONE_CONNECT_URL", "").strip()
if not PHONE_CONNECT_URL:
    raise RuntimeError("PHONE_CONNECT_URL is not set in .env")

START_MESSAGE = os.getenv("PHONE_INIT_MESSAGE", "init session")

app = FastAPI()

_playwright: Optional[Playwright] = None
_browser: Optional[Browser] = None
_page: Optional[Page] = None


class SendMessageRequest(BaseModel):
    text: str


def ensure_page() -> Page:
    """
    Ensure Playwright browser/page is started and Phone Connect UI is ready.
    """
    global _playwright, _browser, _page

    if _page is not None:
        return _page

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=True)
    _page = _browser.new_page()

    # Open Phone Connect page and wait for network idle
    _page.goto(PHONE_CONNECT_URL, wait_until="networkidle")

    # Wait for message input and send button to become available
    _page.wait_for_selector("textarea#messageInput")
    _page.wait_for_selector("button#sendBtn")

    return _page


def send_text(text: str) -> None:
    """Fill message input and click Send in Phone Connect."""
    page = ensure_page()
    page.fill("textarea#messageInput", text)
    page.click("button#sendBtn")


@app.post("/init")
def init_session():
    """
    Send initial message to Phone Connect to initialize session.

    This is optional: launcher can call it based on flag.
    """
    try:
        send_text(START_MESSAGE)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Playwright init error: {e}")
    return {"status": "ok", "message": START_MESSAGE}


@app.post("/send_message")
def send_message(req: SendMessageRequest):
    """
    Send arbitrary text to Phone Connect.
    Will be used by Telegram bot as a remote control.
    """
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty text")

    try:
        send_text(req.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Playwright send error: {e}")

    return {"status": "ok", "sent": req.text}


@app.on_event("shutdown")
def shutdown_event():
    global _playwright, _browser, _page
    if _browser is not None:
        _browser.close()
    if _playwright is not None:
        _playwright.stop()
    _browser = None
    _page = None

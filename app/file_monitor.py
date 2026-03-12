import os
import glob
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

app = FastAPI()

# Path to Antigravity brain sessions (~/.gemini/antigravity/brain)
BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity/brain")

# Root of the current project (where Antigravity writes code)
# Important: keep it in sync with your actual project path
PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", "~/antigravity/projects/crewtask-v2")).expanduser()


def find_latest_dir() -> Path:
    """Return the latest brain session directory by mtime."""
    if not os.path.isdir(BRAIN_DIR):
        raise FileNotFoundError(f"Brain dir not found: {BRAIN_DIR}")

    dirs = [Path(p) for p in glob.glob(f"{BRAIN_DIR}/*") if os.path.isdir(p)]
    if not dirs:
        raise FileNotFoundError("No brain sessions found")

    return max(dirs, key=lambda p: p.stat().st_mtime)


def read_file_from_latest(filename: str) -> str:
    """Read file (e.g. implementation_plan.md) from latest brain session."""
    latest = find_latest_dir()
    target = latest / filename
    if not target.exists():
        raise FileNotFoundError(f"{filename} not found in {latest}")
    return target.read_text(encoding="utf-8")


@app.get("/latest/plan", response_class=PlainTextResponse)
async def latest_plan():
    """Return implementation_plan.md from the latest brain session."""
    try:
        content = read_file_from_latest("implementation_plan.md")
        return content
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/latest/task", response_class=PlainTextResponse)
async def latest_task():
    """Return task.md from the latest brain session."""
    try:
        content = read_file_from_latest("task.md")
        return content
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/latest/walkthrough", response_class=PlainTextResponse)
async def latest_walkthrough():
    """Return walkthrough.md from the latest brain session, if present."""
    try:
        content = read_file_from_latest("walkthrough.md")
        return content
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/project/file", response_class=PlainTextResponse)
async def project_file(path: str):
    """
    Read arbitrary file from the project root by relative path.

    Example: /project/file?path=src/main.py
    """
    root = PROJECT_ROOT.resolve()
    full = (root / path).resolve()
    if not str(full).startswith(str(root)):
        raise HTTPException(status_code=403, detail="Path outside project root")
    if not full.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return full.read_text(encoding="utf-8")

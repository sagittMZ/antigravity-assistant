"""
file_monitor.py — FastAPI service for accessing Antigravity brain
session artifacts and project files.
"""
from __future__ import annotations

import os
import glob as glob_mod
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from app.logger import setup_logger

app = FastAPI(title="File Monitor — Antigravity Assistant")
fm_log = setup_logger("file_monitor", "file_monitor.log")

BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity/brain")

PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", os.getenv(
    "ANTIGRAVITY_PROJECT_DIR", "~/antigravity/projects/crewtask-v2"
))).expanduser()

_BRAIN_EXCLUDE_DIRS = {"tempmediaStorage", "temp", ".tmp"}

def find_latest_dir() -> Path:
    if not os.path.isdir(BRAIN_DIR):
        fm_log.error(f"Brain dir not found: {BRAIN_DIR}")
        raise FileNotFoundError(f"Brain dir not found: {BRAIN_DIR}")
    dirs = [
        Path(p)
        for p in glob_mod.glob(f"{BRAIN_DIR}/*")
        if os.path.isdir(p) and Path(p).name not in _BRAIN_EXCLUDE_DIRS
    ]
    if not dirs:
        fm_log.error("No brain sessions found")
        raise FileNotFoundError("No brain sessions found")
    return max(dirs, key=lambda p: p.stat().st_mtime)

def read_file_from_latest(filename: str) -> str:
    latest = find_latest_dir()
    target = latest / filename
    if not target.exists():
        fm_log.warning(f"{filename} not found in {latest}")
        raise FileNotFoundError(f"{filename} not found in {latest}")
    return target.read_text(encoding="utf-8")

def list_brain_files() -> list[dict]:
    try:
        latest = find_latest_dir()
        files = []
        for f in sorted(latest.iterdir()):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                })
        return files
    except FileNotFoundError:
        return []

@app.get("/latest/plan", response_class=PlainTextResponse)
async def latest_plan():
    try:
        return read_file_from_latest("implementation_plan.md")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/latest/task", response_class=PlainTextResponse)
async def latest_task():
    try:
        return read_file_from_latest("task.md")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/latest/walkthrough", response_class=PlainTextResponse)
async def latest_walkthrough():
    try:
        return read_file_from_latest("walkthrough.md")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/latest/files")
async def latest_files():
    return {"files": list_brain_files()}

@app.get("/latest/file", response_class=PlainTextResponse)
async def latest_file(name: str):
    try:
        return read_file_from_latest(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/project/file", response_class=PlainTextResponse)
async def project_file(path: str):
    root = PROJECT_ROOT.resolve()
    full = (root / path).resolve()
    if not str(full).startswith(str(root)):
        fm_log.error(f"Attempted path traversal outside root: {path}")
        raise HTTPException(status_code=403, detail="Path outside project root")
    if not full.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return full.read_text(encoding="utf-8")

@app.get("/health")
async def health():
    brain_ok = os.path.isdir(BRAIN_DIR)
    project_ok = PROJECT_ROOT.exists()
    return {
        "status": "ok" if brain_ok else "degraded",
        "brain_dir": str(BRAIN_DIR),
        "brain_accessible": brain_ok,
        "project_root": str(PROJECT_ROOT),
        "project_accessible": project_ok,
    }
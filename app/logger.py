"""
logger.py — Centralized logging configuration.
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

def setup_logger(name: str, log_filename: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # Avoid adding handlers multiple times if instantiated again
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / log_filename, 
            maxBytes=5 * 1024 * 1024, # 5 MB
            backupCount=2, 
            encoding="utf-8"
        )
        formatter = logging.Formatter('[%(asctime)s] %(levelname)s [%(name)s]: %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
    return logger
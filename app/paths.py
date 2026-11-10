"""项目里所有大文件的位置——全部放在程序文件夹内（例如 E:\\translater），不占 C 盘。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
CACHE = ROOT / ".cache"
OLLAMA_DIR = ROOT / "ollama"                # Ollama 程序本体
OLLAMA_MODELS = MODELS / "ollama"           # Ollama 下载的大模型


def find_ollama() -> str | None:
    for cand in (
        OLLAMA_DIR / "ollama.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
    ):
        if cand.exists():
            return str(cand)
    return shutil.which("ollama")


def ollama_env() -> dict:
    env = dict(os.environ)
    env["OLLAMA_MODELS"] = str(OLLAMA_MODELS)
    return env

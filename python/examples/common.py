# -*- coding: utf-8 -*-
"""共享设置 —— 对应 TS 版的 setup.ts。
每个示例都调用 open_agent(),拿到 harness 和它的 root 会话。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON_DIR = HERE.parent
REPO = PYTHON_DIR.parent  # 仓库根,与 Node 版共用同一份 .env

sys.path.insert(0, str(PYTHON_DIR))  # 让 import pidurable 生效

from pidurable import (  # noqa: E402
    BASH,
    FauxProvider,
    Harness,
    Models,
    OpenRouterProvider,
    ToolRegistry,
    text_of,
)

DATA_DIR = PYTHON_DIR / ".data"
DB_FILE = DATA_DIR / "agent.sqlite"


def _load_dotenv() -> None:
    """读取仓库根目录的 .env(与 Node 版共用)。已存在的环境变量优先。"""
    file = REPO / ".env"
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and os.environ.get(key) is None:
            os.environ[key] = value


_load_dotenv()
PROVIDER = "openrouter" if os.environ.get("PROVIDER") == "openrouter" else "faux"

# 保存在会话 pi.agent 文档里的模型选择
MODEL = (
    {"provider": "openrouter", "modelId": os.environ.get("MODEL") or "anthropic/claude-sonnet-5.5"}
    if PROVIDER == "openrouter"
    else {"provider": "faux", "modelId": "echo"}
)


def open_agent():
    """打开(或重新打开)同一个 SQLite 文件。同一个文件就是同一个会话。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    models = Models()
    if PROVIDER == "openrouter":
        models.set_provider(
            "openrouter",
            OpenRouterProvider(os.environ.get("OPENROUTER_API_KEY", ""), MODEL["modelId"]),
        )
    else:
        models.set_provider("faux", FauxProvider())

    registry = ToolRegistry()
    registry.install(BASH)  # read, write, edit, bash 里的 bash

    harness = Harness.open(DB_FILE, models=models, registry=registry, model_ref=MODEL, cwd=DATA_DIR)
    return harness, harness.root


def print_transcript(root) -> None:
    """打印会话里所有已存储的条目:编号、种类和一段短文本。"""
    for entry in root.entries():
        print("  #{:<3} {:<15} {}".format(entry["id"], entry["kind"], text_of(entry["content"])[:90]))

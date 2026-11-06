"""源码与 Windows 打包版本共用的资源、用户数据路径。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_dir() -> Path:
    override = os.environ.get("YKT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "YuketangOCRAssistant"
    return Path(__file__).resolve().parent


def stop_requested() -> bool:
    path = os.environ.get("YKT_STOP_FILE")
    return bool(path and Path(path).exists())

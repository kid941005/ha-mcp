# -*- coding: utf-8 -*-
"""统一日志配置：带 call_id 的文本日志，便于按一次工具调用排障。"""
from __future__ import annotations

import contextvars
import logging
import sys

from .config import LOG_LEVEL

_CALL_ID = contextvars.ContextVar("call_id", default="-")


class _CallIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = _CALL_ID.get()
        return True


def setup_logging() -> None:
    """安装一次根 logger handler；重复调用只更新级别。"""
    level_name = (LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, "_homeassistant_xiaozhiserver", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s [%(call_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handler.addFilter(_CallIdFilter())
        handler._homeassistant_xiaozhiserver = True  # type: ignore[attr-defined]
        root.addHandler(handler)


def set_call_id(value: str):
    return _CALL_ID.set(value or "-")


def reset_call_id(token) -> None:
    _CALL_ID.reset(token)

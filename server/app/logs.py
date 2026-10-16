"""运行日志：按天滚动落盘 + 供 /api/logs 读取的解析与过滤。

日志格式与 main.py 的控制台输出一致：%(asctime)s %(levelname)s %(name)s %(message)s
"""
from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "paper-agent.log"

# 只读文件尾部，避免日志增长后每次请求都全量读取
MAX_READ_BYTES = 512 * 1024

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (\w+) ([\w.]+) (.*)$")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def setup_logging() -> None:
    """控制台 + 按天滚动文件双输出，保留 7 天。"""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:  # 已配置过（测试重复调用）则不重复加 handler
        return
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    LOG_DIR.mkdir(exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def parse_line(line: str) -> dict | None:
    """把一行日志解析为 {time, level, name, message}；非标准格式行返回 None。"""
    m = LINE_RE.match(line)
    if m is None:
        return None
    return {"time": m.group(1), "level": m.group(2), "name": m.group(3), "message": m.group(4)}


def read_tail_lines(path: Path = LOG_FILE, max_bytes: int = MAX_READ_BYTES) -> list[str]:
    """读取文件末尾若干字节并按行返回；文件不存在返回空表。"""
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 首行可能被截断，丢弃
    return lines


def query_logs(
    limit: int = 200, level: str = "DEBUG", keyword: str = "", path: Path = LOG_FILE
) -> dict:
    """取尾部日志，按级别阈值与关键词过滤后返回最近 limit 条（时间正序）。"""
    level = level.upper() if level.upper() in LEVELS else "DEBUG"
    threshold = LEVELS.index(level)
    kw = keyword.strip().lower()

    raw_lines = read_tail_lines(path)
    total_lines = len(raw_lines)
    entries: list[dict] = []
    for line in raw_lines:
        parsed = parse_line(line)
        if parsed is None:
            continue
        lv = LEVELS.index(parsed["level"]) if parsed["level"] in LEVELS else 0
        if lv < threshold:
            continue
        if kw and kw not in (parsed["message"] + " " + parsed["name"]).lower():
            continue
        entries.append(parsed)

    truncated = len(entries) > limit
    return {
        "file": path.name,  # 只回文件名，不暴露服务器绝对路径
        "total_lines": total_lines,
        "truncated": truncated,
        "entries": entries[-limit:] if limit > 0 else [],
    }

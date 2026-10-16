from app.logs import parse_line, query_logs, read_tail_lines


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_line_standard_format():
    entry = parse_line(
        "2026-09-16 09:00:21,350 ERROR app.routers.runs 手动批次失败"
    )
    assert entry == {
        "time": "2026-09-16 09:00:21,350",
        "level": "ERROR",
        "name": "app.routers.runs",
        "message": "手动批次失败",
    }


def test_parse_line_non_standard_returns_none():
    assert parse_line("some random text") is None
    assert parse_line("") is None


def test_read_tail_lines_missing_file(tmp_path):
    assert read_tail_lines(tmp_path / "nope.log") == []


def test_query_logs_filters_and_limit(tmp_path):
    p = tmp_path / "paper-agent.log"
    _write(
        p,
        [
            "2026-09-16 08:00:00,000 INFO app.pipeline 批次 #1 开始（manual 触发）",
            "2026-09-16 08:00:01,000 INFO uvicorn.access GET /api/health 200",
            "2026-09-16 08:00:02,000 WARNING app.pipeline 论文源 arxiv 检索失败：超时",
            "2026-09-16 08:00:03,000 ERROR app.pipeline 提炼失败 [2603.01234] 标题：429",
            "非日志行",
        ],
    )
    # 级别阈值：WARNING 起步会滤掉两条 INFO
    result = query_logs(level="WARNING", path=p)
    assert [e["level"] for e in result["entries"]] == ["WARNING", "ERROR"]
    assert result["total_lines"] == 5
    # 关键词只命中消息/来源名
    result = query_logs(keyword="arxiv", path=p)
    assert len(result["entries"]) == 1
    assert result["entries"][0]["level"] == "WARNING"
    # limit 取尾部
    result = query_logs(limit=1, path=p)
    assert len(result["entries"]) == 1
    assert result["entries"][0]["level"] == "ERROR"
    assert result["truncated"] is True

import json
from types import SimpleNamespace

import pytest

from app.llm_client import LlmClient, LlmError, extract_json, validate_digest


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_with_prose():
    text = '好的，以下是解读：\n```json\n{"a": [1, 2]}\n```\n希望有帮助'
    assert extract_json(text) == {"a": [1, 2]}


def test_extract_json_no_object_raises():
    with pytest.raises(ValueError):
        extract_json("完全没有 JSON 的回答")


VALID_DIGEST = {
    "one_liner": "一句话",
    "outline": [{"section": "引言", "summary": "背景"}],
    "key_points": [{"point": "要点", "detail": "细节"}],
    "implementation": "实现过程",
    "principle": "原理",
    "limitations": "局限",
    "difficulty": 2,
}


def test_validate_digest_clamps_difficulty():
    data = dict(VALID_DIGEST, difficulty=99)
    assert validate_digest(data)["difficulty"] == 5
    data = dict(VALID_DIGEST, difficulty="not-a-number")
    assert validate_digest(data)["difficulty"] == 3


def test_validate_digest_missing_key_raises():
    bad = {k: v for k, v in VALID_DIGEST.items() if k != "principle"}
    with pytest.raises(ValueError, match="principle"):
        validate_digest(bad)


def test_validate_digest_coerces_list_text_fields():
    """模型可能把 implementation 输出成步骤数组，必须收敛为换行文本（否则 TEXT 列插入报 1241）。"""
    data = dict(
        VALID_DIGEST,
        implementation=["1. 第一步", "2. 第二步"],
        principle={"k": "v"},
    )
    out = validate_digest(data)
    assert out["implementation"] == "1. 第一步\n2. 第二步"
    assert isinstance(out["principle"], str)


def test_validate_digest_filters_non_dict_items():
    data = dict(
        VALID_DIGEST,
        outline=[{"section": "引言", "summary": "背景"}, ["错误项", "应被过滤"]],
        key_points=[["标题", "说明"], {"point": "要点", "detail": "细节"}],
    )
    out = validate_digest(data)
    assert out["outline"] == [{"section": "引言", "summary": "背景"}]
    assert out["key_points"] == [{"point": "要点", "detail": "细节"}]


def test_validate_digest_scalar_outline_raises_valueerror():
    """回归：outline 为标量时必须抛 ValueError（可读的解析失败），而不是迭代时的裸 TypeError。"""
    with pytest.raises(ValueError, match="outline"):
        validate_digest(dict(VALID_DIGEST, outline=5))
    with pytest.raises(ValueError, match="key_points"):
        validate_digest(dict(VALID_DIGEST, key_points={"a": 1}))


def test_validate_digest_all_non_dict_items_raises_valueerror():
    with pytest.raises(ValueError, match="章节条目"):
        validate_digest(dict(VALID_DIGEST, outline=["只有字符串"]))
    with pytest.raises(ValueError, match="要点条目"):
        validate_digest(dict(VALID_DIGEST, key_points=[1, 2, 3]))


def test_validate_digest_diagrams_absent_defaults_empty():
    out = validate_digest(dict(VALID_DIGEST))
    assert out["diagrams"] == []


def test_validate_digest_diagrams_valid_passthrough():
    diagrams = [
        {"type": "flow", "caption": "整体流程", "mermaid": 'flowchart TD\n  A["输入"] --> B["输出"]'},
        {
            "type": "architecture",
            "caption": "系统分层",
            "mermaid": 'flowchart LR\n  subgraph S1["存储层"]\n    A["索引"]\n  end',
        },
    ]
    out = validate_digest(dict(VALID_DIGEST, diagrams=diagrams))
    assert out["diagrams"] == diagrams


def test_validate_digest_diagrams_invalid_items_dropped():
    """非 dict 项、未知 type、非 flowchart 开头的 mermaid 全部丢弃；代码围栏剥掉后保留。"""
    diagrams = [
        "not-a-dict",
        {"type": "pie", "caption": "坏类型", "mermaid": 'flowchart TD\n  A["x"]'},
        {"type": "flow", "caption": "坏语法", "mermaid": "sequenceDiagram\n  A->>B: hi"},
        {"type": "flow", "caption": "带围栏", "mermaid": "```mermaid\nflowchart TD\n  A[\"x\"] --> B[\"y\"]\n```"},
    ]
    out = validate_digest(dict(VALID_DIGEST, diagrams=diagrams))
    assert out["diagrams"] == [
        {"type": "flow", "caption": "带围栏", "mermaid": 'flowchart TD\n  A["x"] --> B["y"]'}
    ]


def test_validate_digest_diagrams_capped_at_two():
    diagrams = [
        {"type": "flow", "caption": f"图{i}", "mermaid": f'flowchart TD\n  A["{i}"]'} for i in range(3)
    ]
    out = validate_digest(dict(VALID_DIGEST, diagrams=diagrams))
    assert len(out["diagrams"]) == 2


def _fake_client(outputs: list[str], tokens: list[int]):
    """构造假 SDK 客户端：按顺序返回预置回复。"""
    calls = {"index": 0}

    def create(**kwargs):
        i = calls["index"]
        calls["index"] += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outputs[i]))],
            usage=SimpleNamespace(total_tokens=tokens[i]),
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_digest_success_with_fenced_json():
    client = _fake_client(["```json\n" + json.dumps(VALID_DIGEST, ensure_ascii=False) + "\n```"], [1200])
    zc = LlmClient(api_key="fake", client=client)
    data, tokens = zc.digest_with_tokens("标题", "摘要", "全文内容")
    assert data["one_liner"] == "一句话"
    assert tokens == 1200


def test_digest_retries_then_raises():
    bad_then_worse = ["这不是 JSON", "也不是 {json}"]
    client = _fake_client(bad_then_worse, [10, 10])
    zc = LlmClient(api_key="fake", client=client)
    with pytest.raises(LlmError, match="无法解析"):
        zc.digest_with_tokens("标题", "摘要")


def test_digest_retry_recovers():
    outputs = ["坏输出", json.dumps(VALID_DIGEST, ensure_ascii=False)]
    client = _fake_client(outputs, [10, 900])
    zc = LlmClient(api_key="fake", client=client)
    data, tokens = zc.digest_with_tokens("标题", "摘要")
    assert data["outline"][0]["section"] == "引言"
    assert tokens == 910


def test_missing_api_key_raises():
    with pytest.raises(LlmError, match="未配置"):
        LlmClient(api_key="")


def _err(status_code: int | None, message: str | None = None):
    """带/不带 status_code 的异常，模拟限流、网络抖动与参数校验错。"""
    exc = Exception(message if message is not None else f"simulated error status={status_code}")
    if status_code is not None:
        exc.status_code = status_code
    return exc


def _fake_client_raise(exc):
    class Completions:
        @staticmethod
        def create(**kwargs):
            raise exc

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    return Client()


def test_json_mode_survives_transient_errors():
    """429 限流/网络抖动（无状态码）不得关闭 JSON 模式——否则输出永久退化。"""
    for status in (429, None, 500):
        zc = LlmClient(api_key="fake", client=_fake_client_raise(_err(status)))
        with pytest.raises(Exception):
            zc._create([{"role": "user", "content": "hi"}])
        assert zc._json_mode_ok is True


def test_json_mode_disabled_only_on_param_error():
    """仅 400 且报文点名 response_format 才回退：去掉 response_format 重试并永久关闭 JSON 模式。"""
    calls = []

    class Completions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs.get("response_format"))
            if "response_format" in kwargs:
                raise _err(400, "Invalid parameter: response_format not supported")
            return "ok"

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    zc = LlmClient(api_key="fake", client=Client())
    assert zc._create([{"role": "user", "content": "hi"}]) == "ok"
    assert calls == [{"type": "json_object"}, None]
    assert zc._json_mode_ok is False


def test_unrelated_4xx_keeps_json_mode():
    """401（key 失效）/ 与 response_format 无关的 400（如超长）不得关闭 JSON 模式。

    回归：曾经任何 4xx 都会永久关闭 JSON 模式，一次 key 故障后整个进程的
    提炼全部退化成非 JSON 模式、解析失败率飙升。
    """
    for status, message in ((401, "invalid api key"), (400, "max_tokens exceeds limit")):
        zc = LlmClient(api_key="fake", client=_fake_client_raise(_err(status, message)))
        with pytest.raises(Exception):
            zc._create([{"role": "user", "content": "hi"}])
        assert zc._json_mode_ok is True


def test_normalize_intent_defensive():
    from app.llm_client import normalize_intent
    import pytest

    out = normalize_intent(
        {"keywords": ["kg", "x"], "authors": ["`Fei-Fei Li`"], "from_date": "2026-01-01",
         "to_date": "2025-01-01", "sort_hint": "citations"}
    )
    assert out["authors"] == ["Fei-Fei Li"]  # 去围栏
    assert out["from_date"] == "2025-01-01" and out["to_date"] == "2026-01-01"  # 日期颠倒自动交换
    assert out["sort_hint"] == "citations"
    # 非法输入收敛
    bad = normalize_intent({"keywords": ["ok"], "authors": [], "from_date": "bad", "to_date": None, "sort_hint": "weird"})
    assert bad["from_date"] is None and bad["sort_hint"] is None
    with pytest.raises(ValueError):
        normalize_intent({"keywords": [], "authors": []})


def _recording_client(content: str, captured: dict):
    """记录 create 参数的假 SDK，用于断言 prompt/模型/超时。"""
    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(total_tokens=100),
        )
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_digest_figure_notes_injected_into_prompt():
    """图表转述须置于全文之前，并带忠实性规则段（设计 §7）。"""
    captured = {}
    zc = LlmClient(
        "k",
        client=_recording_client(json.dumps(VALID_DIGEST, ensure_ascii=False), captured),
    )
    zc.digest_with_tokens("T", "A", "正文全文", "图1(架构):组件A连接组件B")
    prompt = captured["messages"][1]["content"]
    assert "【原文图表转述（由视觉模型识读，可能有误）】" in prompt
    assert "图1(架构):组件A连接组件B" in prompt
    assert "以正文为准" in prompt
    # 注入段须在正文全文之前（rindex：模板规则区也出现过"全文："字样）
    assert prompt.index("原文图表转述") < prompt.rindex("全文：")


def test_digest_without_figure_notes_prompt_unchanged():
    captured = {}
    zc = LlmClient(
        "k",
        client=_recording_client(json.dumps(VALID_DIGEST, ensure_ascii=False), captured),
    )
    zc.digest_with_tokens("T", "A", "正文全文")
    assert "原文图表转述" not in captured["messages"][1]["content"]


def test_describe_image_uses_vision_model_and_timeout(tmp_path):
    """识图调用必须：用 vision_model、显式 60s 超时、base64 图片消息体。"""
    captured = {}
    zc = LlmClient("k", client=_recording_client("转述结果", captured), vision_model="test-vision")
    img = tmp_path / "fig-001.jpg"
    img.write_bytes(b"fake-jpeg-bytes")
    out = zc.describe_image(str(img), "论文标题", "Figure 1")
    assert out == "转述结果"
    assert captured["model"] == "test-vision"
    assert captured["timeout"] == 60
    parts = captured["messages"][0]["content"]
    assert any(p["type"] == "image_url" for p in parts)
    assert any(p["type"] == "text" and "论文标题" in p["text"] for p in parts)

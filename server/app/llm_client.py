"""通用 OpenAI 兼容 LLM 客户端：任何兼容 OpenAI 协议的服务（智谱/DeepSeek/Qwen/
Ollama/vLLM/OpenRouter 等）都只是不同的 base_url + api_key + model 组合。

provider 专属能力的边界：
- chat_thinking 配置非空时才随对话请求发送思考档位（extra_body，GLM-5.x 用，
  其他厂商不配置即不发送）；
- 流式返回的 reasoning_content 增量为可选字段，防御式读取，厂商不返回即无感知；
- embeddings 的 dimensions 参数被拒时自动去参重试（见 embed）。
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)

MAX_FULLTEXT_CHARS = 60_000

DIGEST_SYSTEM = (
    "你是资深科研论文解读助手。你只输出一个合法的 JSON 对象，不输出任何其他文字、解释或代码围栏。"
    "你的第一原则是忠实：所有内容必须源自论文原文，绝不杜撰、脑补或添加论文没有的信息。"
    "你写出的解读必须好读：像一位真正读懂论文的同事，用自然流畅的中文讲给别人听。"
)

DIGEST_PROMPT = """请深入解读下面这篇论文，输出 JSON（键固定，中文内容）：
{{
  "one_liner": "一句话说清论文做了什么，不超过 60 字的完整句子",
  "outline": [{{"section": "章节名", "summary": "该章实际做了什么、结论是什么，2-3 句连贯的话"}}],
  "key_points": [{{"point": "要点标题（15字内，说人话）", "detail": "3-5 句连贯的话，把'是什么、为什么这样设计、证据是什么'讲成一段小短文（每句至少 25 字）"}}],
  "implementation": "实现过程拆解（分 6-10 个编号步骤，每步 80-150 字，见下方要求）",
  "principle": "原理拆解，250-450 字（见下方要求）",
  "limitations": "局限与值得关注的原因，150-250 字，末尾附『通俗度 X/5：…』说明",
  "difficulty": 1,
  "diagrams": [{{"type": "flow", "caption": "整体流程", "mermaid": "flowchart TD\\n  A[\\"对话历史\\"] --> B[\\"摘要索引\\"]\\n  B --> C[\\"多路检索\\"]\\n  C --> D[\\"生成回复\\"]"}}]
}}

【读者与文风——决定解读能不能读下去，与忠实性同等重要】
- 读者画像：有技术背景、但不了解这篇论文细分领域的工程师。他们要的是"读懂"，请你讲给他们听。
- 写自然连贯的中文句子：主谓宾完整，一句话讲清一件事，用句号断句，长句拆成两句。
  禁止电报式短语堆砌——反例："多任务联合、共享编码器、损失加权"；应写成："训练时把多个任务放在一起，共享同一个编码器，再用加权损失来平衡各任务的学习"。
- 句与句之间要有逻辑衔接（因为、因此、其中、相比之下、具体来说），让人顺着读下去，而不是并列罗列事实。
- 数字与专名自然写进句子——正例："在 GSM8K 数据集上把准确率提高了 3.2 个百分点"；
  不要写成括号注记串——反例："效果提升（GSM8K，+3.2%，Table 2）"。
- 术语管理：专业术语第一次出现时写中文并附英文原文（如"检索增强生成（RAG）"），之后全文统一用同一个叫法，不要中英文混着叫。
- 禁止翻译腔与空话：不写"被提出""进行了实现""效果显著""具有重要意义"这类表达。

【忠实性——高于一切】
- 只能写论文原文里有的内容。模块名、方法名、数据集、指标、数字、实验设置必须能在原文中找到出处；不允许为了"看起来完整"而补齐论文没说的环节。不确定或原文缺失的信息，明确写"论文未说明"，绝不猜测。
- 禁止无依据的拔高或引申：不得添加论文没有的评价、展望或"作者认为"；principle 中的类比仅用于建立直觉，类比之后必须回到论文真实机制，不得把类比内容当成论文事实。
- 凡是评价性论断，必须紧跟论文里的具体证据（模块/方法名、数据集、指标名和数字）。原文没给数字就写"论文未给出量化对比"。

【各字段写法】
- one_liner：完整句子，说清"做了什么 + 怎么做到 + 达到什么效果"。
- outline：4-8 条，按论文真实章节；summary 写该章做了什么、得到什么，不写"本章介绍了…"。
- key_points：5-8 条，覆盖：核心问题、方法的关键设计（逐个说清为什么这样设计）、主要实验结论（带数字）、消融或分析发现。每条 detail 里至少出现一个具体名词或数字。
- implementation：讲"这篇东西是怎么做出来的"，写成 6-10 个编号步骤（"1. …""2. …"，步骤间换行），覆盖：整体流程每一步的输入输出、关键模块的内部设计与衔接（模块叫什么、内部怎么工作、和其他模块怎么交互）、训练/推理配置（数据规模、训练轮数、超参数——论文给了就写）、数据如何流动、工程上值得注意的细节。每步是一段通顺的话，并讲清与上一步的承接（上一步的输出如何变成下一步的输入）；写不满说明还没读进论文，回去重读方法章节。读代码的人应能据此在脑中复现整个方案。
- principle：用大白话讲透核心机制的因果链（至少 3 个环节的"因为…所以…"），250-450 字：为什么这样设计会有效、信息在各环节怎么变换，配一个生活化类比；类比只建直觉，随后必须回到论文的真实机制。
- difficulty 为 1-5 整数（1=人人能懂，5=需要较多基础）。
- 若只有摘要没有全文：implementation 与 key_points 只写从摘要能确证的内容并注明"仅基于摘要"，缺失处写"论文未说明"，不要编造论文未说的细节。

【图示 diagrams——几乎每篇论文都要画一张，按论文类型选画法】
- diagrams 为数组，最多 2 个对象；仅摘要没有全文时输出空数组 []（信息不足以忠实画图）。逐类判断：
  - "flow"（流程/框架图，必有一张）：按论文类型选择画法——
    方法/工程类：画方法流水线，输入 → 若干处理阶段 → 输出；
    纯理论/数学证明类：画论证框架图——问题设定、关键定义、核心定理与关键引理之间的推导依赖（哪条引理支撑哪条定理、由什么推出什么）、主要推论与结论；
    综述调研类：画主题分类框架，论文对各研究方向的分类组织；
  - "architecture"（系统架构图，可选）：论文贡献是工程系统/框架/引擎/平台，有组件划分与交互（分层结构、模块接口）时再加一张，用 subgraph 按层分组。
- caption：20 字以内，说明这张图展示什么。
- mermaid 语法硬性约束，违反即渲染失败：
  - 只能以 flowchart TD 或 flowchart LR 开头，禁止 sequenceDiagram、stateDiagram、graph 等其他类型；
  - 节点 id 用字母加数字（如 A、B2），标签一律用双引号包裹：A["编码器"] --> B["重排序器"]；
  - 节点总数不超过 18；边上需要条件/数据时加标签：A -->|"top-k"| B；
  - 架构图分组：subgraph S1["存储层"] … end；
  - 禁止 style/class 语句、%% 注释、HTML 标签、emoji；mermaid 字符串内的换行用 \\n 转义。
- 图的忠实性与其他字段相同：节点与连线必须是论文原文出现的模块名、阶段与真实数据流，论文没写的环节不要画，宁缺毋滥。

论文标题：{title}
摘要：{abstract}
{body}"""


VISION_PROMPT = (
    "你是论文图表解读专家。转述这张出自论文《{title}》的图片。\n"
    "图注：{caption}（可能为空，以图内实际内容为准）\n"
    "输出四段，中文：\n"
    "1.【类型】方法架构图 / 实验结果图 / 公式 / 示意图 / 表格截图 / 其他\n"
    "2.【公式】若含数学公式，逐条转写为 LaTeX（$$...$$ 包裹），符号保持原样，禁止化简或改写\n"
    "3.【数据】若是数据图：坐标轴含义、图例、关键数值与趋势（能读多少写多少）\n"
    "4.【结构】若是架构/流程图：组件名称与连接关系，按图中原文命名\n"
    '只描述图中真实可见的内容，看不清的写"图中不可辨"，不推测不补全。合计 300 字内。'
)

FIGURE_NOTES_RULES = (
    "\n使用规则：转述可用于理解与叙述（公式、数值、组件名可引用）；"
    '引用其中内容时表述为"据原文图 N"；评价性结论仍须正文证据；转述与正文冲突时，以正文为准。\n\n'
)


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_intent(data: dict) -> dict:
    """模型输出的意图做防御性归一化：类型修正、数量截断、日期校验。"""
    def _names(value, limit):
        if not isinstance(value, list):
            return []
        out = []
        for name in value[:limit]:
            clean = str(name).strip()
            for ch in "`\"'":
                clean = clean.strip(ch)
            clean = clean.replace(",", " ").strip()
            if 2 <= len(clean) <= 80:
                out.append(clean)
        return out

    keywords = _names(data.get("keywords"), 5)
    authors = _names(data.get("authors"), 3)
    dates = []
    for key in ("from_date", "to_date"):
        value = data.get(key)
        dates.append(value if isinstance(value, str) and DATE_RE.match(value) else None)
    from_date, to_date = dates
    if from_date and to_date and from_date > to_date:
        from_date, to_date = to_date, from_date
    sort_hint = data.get("sort_hint")
    if sort_hint not in ("latest", "citations", "relevance"):
        sort_hint = None
    if not keywords and not authors:
        raise ValueError("意图中没有可用的检索条件")
    return {
        "keywords": keywords,
        "authors": authors,
        "from_date": from_date,
        "to_date": to_date,
        "sort_hint": sort_hint,
    }


class LlmError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象：兼容裸 JSON 与 ```json 围栏。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("输出中未找到 JSON 对象")
    return json.loads(text[start : end + 1], strict=False)  # strict=False 容忍字符串内的控制字符


def _as_text(value) -> str:
    """模型偶尔会把文本字段输出成数组（如分步骤列表）或其他类型，统一收敛为字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


DIAGRAM_TYPES = ("flow", "architecture")


def _clean_diagrams(value) -> list[dict]:
    """diagrams 可选字段清洗：剥代码围栏、逐项校验，非法项静默丢弃，最多保留 2 张。

    有意不放进 validate_digest 的 required——diagrams 缺失/全非法只是"该论文不配图"，
    不应让整次提炼失败（老模型输出与测试夹具也没有这个键）。
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") not in DIAGRAM_TYPES:
            continue
        mermaid = _as_text(item.get("mermaid", "")).strip()
        fence = re.search(r"```(?:mermaid)?\s*(.+?)```", mermaid, re.S)
        if fence:
            mermaid = fence.group(1).strip()
        if not mermaid.lower().startswith("flowchart") or len(mermaid) > 4000:
            continue
        out.append(
            {
                "type": item["type"],
                "caption": _as_text(item.get("caption", "")).strip()[:60],
                "mermaid": mermaid,
            }
        )
        if len(out) == 2:
            break
    return out


def validate_digest(data: dict) -> dict:
    required = [
        "one_liner",
        "outline",
        "key_points",
        "implementation",
        "principle",
        "limitations",
        "difficulty",
    ]
    for key in required:
        if key not in data:
            raise ValueError(f"缺少字段 {key}")
    for key in ("one_liner", "implementation", "principle", "limitations"):
        data[key] = _as_text(data[key])
    # 先校验可迭代，再过滤 dict 项，防止模型输出标量/字典时迭代抛 TypeError
    if not isinstance(data["outline"], list) or not data["outline"]:
        raise ValueError("outline 必须为非空数组")
    if not isinstance(data["key_points"], list) or not data["key_points"]:
        raise ValueError("key_points 必须为非空数组")
    data["outline"] = [s for s in data["outline"] if isinstance(s, dict)]
    data["key_points"] = [k for k in data["key_points"] if isinstance(k, dict)]
    if not data["outline"]:
        raise ValueError("outline 内没有可识别的章节条目")
    if not data["key_points"]:
        raise ValueError("key_points 内没有可识别的要点条目")
    try:
        data["difficulty"] = max(1, min(5, int(data["difficulty"])))
    except (TypeError, ValueError):
        data["difficulty"] = 3
    data["diagrams"] = _clean_diagrams(data.get("diagrams"))
    return data


class LlmClient:
    """OpenAI 兼容的对话/视觉/embedding 客户端。client 参数仅供测试注入。"""

    def __init__(
        self,
        api_key: str,
        model: str = "",
        client=None,
        base_url: str | None = None,
        vision_model: str | None = None,
        chat_thinking: str = "",
        embedding_model: str = "embedding-3",
        embedding_dims: int = 1024,
    ):
        if not api_key:
            raise LlmError("未配置 LLM API Key")
        self.model = model
        self.vision_model = vision_model or None
        self.chat_thinking = chat_thinking or ""  # 非空才随对话发送思考档位（如 GLM-5.x 的 low）
        self.embedding_model = embedding_model
        self.embedding_dims = embedding_dims
        # base_url 指向具体服务（如智谱 Coding Plan 通道、Ollama、vLLM）；空 = 官方默认
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = client or OpenAI(**kwargs)
        self._json_mode_ok = True  # 某些模型不支持 response_format，失败后自动关闭

    def _create(self, messages: list[dict], **extra):
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=0.3,
            max_tokens=12000,  # 推理模型的思考 token 也会计入，留足余量防截断
            **extra,
        )
        if self._json_mode_ok:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            # 仅当 API 明确因 response_format 参数被拒（400 且报文点名该参数）才永久关闭
            # JSON 模式并去参重试；401/403/404（key 或模型名问题）、超长输入等其他 4xx
            # 与本参数无关，必须原样抛出，避免一次无关故障让整个进程的提炼退化成非 JSON 模式
            status = getattr(exc, "status_code", None)
            param_rejected = status == 400 and "response_format" in str(exc).lower()
            if (
                not self._json_mode_ok
                or status is None
                or status == 429
                or status >= 500
                or not param_rejected
            ):
                raise
            self._json_mode_ok = False
            logger.warning("模型 %s 不支持 response_format（%s），已关闭 JSON 模式", self.model, exc)
            kwargs.pop("response_format", None)
            return self._client.chat.completions.create(**kwargs)

    def digest_with_tokens(
        self,
        title: str,
        abstract: str,
        fulltext: str | None = None,
        figure_notes: str | None = None,
    ) -> tuple[dict, int]:
        body = ""
        if fulltext:
            clipped = fulltext[:MAX_FULLTEXT_CHARS]
            note = "（原文过长，已截断）" if len(fulltext) > MAX_FULLTEXT_CHARS else ""
            body = f"全文：{clipped}{note}"
        else:
            body = "全文：无 HTML 版全文，请基于标题与摘要解读，内容保持克制。"
        if figure_notes:
            # 图表转述置于全文之前：先建图景再读正文；规则段是忠实性防线（设计 §7）
            body = (
                "【原文图表转述（由视觉模型识读，可能有误）】\n"
                + figure_notes
                + FIGURE_NOTES_RULES
                + body
            )
        prompt = DIGEST_PROMPT.format(title=title, abstract=abstract or "（无摘要）", body=body)

        messages = [
            {"role": "system", "content": DIGEST_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        total_tokens = 0
        for attempt in range(2):
            completion = self._create(messages)
            content = completion.choices[0].message.content or ""
            if completion.usage is not None:
                total_tokens += completion.usage.total_tokens or 0
            try:
                return validate_digest(extract_json(content)), total_tokens
            except (ValueError, json.JSONDecodeError) as exc:
                if attempt == 1:
                    raise LlmError(f"模型输出无法解析为合法解读 JSON：{exc}") from exc
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            f"你的输出解析失败：{exc}。请严格只输出一个合法 JSON 对象，键与格式要求不变。"
                            "特别注意：diagrams 里的 mermaid 是 JSON 字符串，其中的双引号必须转义写成 \\\"，"
                            "换行必须写成 \\n，不得出现裸双引号。"
                        ),
                    },
                ]
        raise LlmError("unreachable")

    def describe_image(self, image_path: str, title: str, caption: str) -> str:
        """视觉模型转述单张原文图片。60s 硬超时：识图在提炼互斥锁内，不允许拖住锁。"""
        b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
        completion = self._client.chat.completions.create(
            model=self.vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": VISION_PROMPT.format(title=title, caption=caption or "（无）")},
                    ],
                }
            ],
            temperature=0.2,
            max_tokens=1500,
            timeout=60,
        )
        return (completion.choices[0].message.content or "").strip()

    def chat_stream(self, system: str, messages: list[dict]):
        """多轮对话流式输出：yield ("reasoning"|"content", 文本增量)。

        部分推理模型（GLM-5.x/DeepSeek-R1 等）始终思考不可关闭，对话场景用低档降低思考耗时；
        chat_thinking 未配置时不发送该参数。reasoning 增量用于前端展示"思考中"，
        content 才是正式回复。
        """
        extra = {}
        if self.chat_thinking:
            extra["extra_body"] = {"thinking": {"type": "enabled", "level": self.chat_thinking}}
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            temperature=0.3,
            max_tokens=6000,
            stream=True,
            timeout=120,  # 连接停滞最多挂 2 分钟，缩短断连后的线程占用
            **extra,
        )
        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield "reasoning", reasoning
                content = getattr(delta, "content", None)
                if content:
                    yield "content", content
        finally:
            # 主动关闭上游 SSE 连接：客户端断开后同步生成器无法被取消，
            # 不关会悬挂到读超时
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def parse_query_intent(self, text: str, today: str) -> dict:
        """把自然语言检索需求解析成结构化意图（在线检索用）。

        today 传当天日期（YYYY-MM-DD）供模型换算"近期/近半年"这类相对时间。
        返回归一化后的 {keywords, authors, from_date, to_date, sort_hint}。
        """
        system = (
            "你是论文检索意图解析器。把用户的检索需求解析成 JSON（键固定）：\n"
            '{"keywords": ["英文小写主题词", ...],  // 0-5 个，仅界定研究领域，不含时间词与泛词\n'
            '"authors": ["英文作者姓名", ...],  // 需求指向特定人/团队时给出规范英文名'
            "（如 Fei-Fei Li），否则空数组\n"
            '"from_date": "YYYY-MM-DD" 或 null,  // 时间范围起点；相对时间（近期/近半年）'
            f"按今天 {today} 换算成具体日期\n"
            '"to_date": "YYYY-MM-DD" 或 null,\n'
            '"sort_hint": "latest" 或 "citations" 或 "relevance"  // 用户想按时间/影响力/相关度找\n'
            "}\n只输出 JSON，不要解释。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]
        for _ in range(2):
            completion = self._create(messages)
            raw = (completion.choices[0].message.content or "").strip()
            if raw:
                try:
                    return normalize_intent(extract_json(raw))
                except (ValueError, json.JSONDecodeError):
                    pass
        raise LlmError("检索意图解析失败，请换种说法重试")

    def embed(self, text: str) -> list[float]:
        try:
            resp = self._client.embeddings.create(
                model=self.embedding_model, input=text, dimensions=self.embedding_dims
            )
        except Exception:
            # 个别网关不支持 dimensions 参数时按模型默认维度重试
            resp = self._client.embeddings.create(model=self.embedding_model, input=text)
        return list(resp.data[0].embedding)

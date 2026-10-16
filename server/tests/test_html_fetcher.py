from app.html_fetcher import _make_soup, extract_figures, extract_text

SAMPLE_HTML = """
<html>
  <head><title>Paper</title><style>body{color:red}</style>
  <script>window.tracking = 1;</script></head>
  <body>
    <nav>LTM header search menu</nav>
    <header>arXiv logo</header>
    <h1>AgentFugue: Multi-Agent Orchestration</h1>
    <section id="s1"><h2>1 Introduction</h2>
      <p>Multi-agent systems  are   hard to coordinate.</p>
    </section>
    <section id="s2"><h2>2 Method</h2><p>We propose a protocol.</p></section>
    <footer>Bibliographic details</footer>
  </body>
</html>
"""


def test_extract_text_removes_noise():
    text = extract_text(SAMPLE_HTML)
    assert "AgentFugue" in text
    assert "coordinate" in text          # 正文保留（含空白折叠）
    assert "tracking" not in text        # script 移除
    assert "color:red" not in text       # style 移除
    assert "menu" not in text            # nav 移除
    assert "Bibliographic" not in text   # footer 移除


def test_extract_text_collapses_whitespace():
    text = extract_text(SAMPLE_HTML)
    assert "systems are" in text         # 多个空格折叠为一个
    assert "\n\n\n" not in text


def test_extract_text_truncates_to_cap():
    long_html = "<html><body>" + ("<p>word </p>" * 100_000) + "</body></html>"
    text = extract_text(long_html)
    assert len(text) <= 60_000


MATH_HTML = """
<html><body>
  <p>The loss is <math alttext="\\mathcal{L} = \\alpha \\cdot t + c_{t}">svg-fallback-content</math> in practice.</p>
  <p>Broken one: <math>unparseable mrow content</math>.</p>
</body></html>
"""


def test_extract_text_restores_latex_from_alttext():
    """latexml 公式带 alttext（LaTeX 源码），提取时替换为 $...$，SVG/MathML 碎片不进正文。"""
    text = extract_text(MATH_HTML)
    assert "$\\mathcal{L} = \\alpha \\cdot t + c_{t}$" in text
    assert "svg-fallback-content" not in text
    assert "unparseable mrow content" not in text  # 无 alttext 的公式不留碎片


FIGURE_HTML = """
<html><body>
  <img src="/static/base/1.0.1/images/logo-primary.svg"/>
  <figure>
    <img src="2606.25161v1/x1.png"/>
    <figcaption>Figure 1: Overall architecture of the framework</figcaption>
  </figure>
  <img src="https://arxiv.org/html/2606.25161/4.7.png" alt="equation 4.7"/>
  <img src="2606.25161v1/x1.png"/>
  <img src="data:image/png;base64,AAAA"/>
</body></html>
"""


def test_extract_figures_resolves_and_pairs():
    """图标排除、相对 URL 解析（arXiv 惯例：src 带版本目录）、figcaption/alt 配对、同 URL 去重。"""
    soup = _make_soup(FIGURE_HTML)
    figs = extract_figures(soup, "https://arxiv.org/html/2606.25161")
    assert [f["url"] for f in figs] == [
        "https://arxiv.org/html/2606.25161v1/x1.png",
        "https://arxiv.org/html/2606.25161/4.7.png",
    ]
    assert figs[0]["caption"] == "Figure 1: Overall architecture of the framework"
    assert figs[1]["caption"] == "equation 4.7"
    assert [f["idx"] for f in figs] == [1, 2]

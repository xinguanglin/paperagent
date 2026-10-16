"""手工上传论文的解析：魔数检测 + PDF/DOCX/DOC 文本提取。

- PDF：PyMuPDF 直解（文本质量最好）
- DOCX：python-docx 提取段落与表格
- DOC（旧二进制）：本机装有 Word 时经 COM 静默转 docx 再解析；未装则明确报错
重库（fitz/docx/pywin32）延迟导入：无上传流量时不加载。
"""
from __future__ import annotations

import logging
import re
import sys
import tempfile
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_STORED_CHARS = 1_000_000  # 解析文本入库上限（MEDIUMTEXT 充裕，防异常大文件撑爆内存/库）

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # 旧版 Office 二进制（.doc）

_SAFE_NAME = re.compile(r"[^\w.\-\u4e00-\u9fff]+")  # 文件名清洗：仅留字母数字点横杠下划线和中文


def sanitize_filename(name: str) -> str:
    """去掉路径部分与危险字符，只留安全字符（防路径穿越/保留名攻击）。"""
    base = Path(name.replace("\\", "/")).name  # 去目录部分
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "paper"
    return cleaned[:120]


def detect_format(head: bytes) -> str:
    """按魔数判定真实格式（不信任扩展名，防改名绕过）。返回 'pdf'/'docx'/'doc'。"""
    if head.startswith(PDF_MAGIC):
        return "pdf"
    if head.startswith(ZIP_MAGIC):
        return "docx"  # docx 即 zip；内部是否真为 Word 文档由 extract 阶段校验
    if head.startswith(OLE_MAGIC):
        return "doc"
    raise ValueError("无法识别的文件格式（仅支持 PDF / DOC / DOCX），或文件已损坏")


def word_available() -> bool:
    """注册表探测本机是否装有 Word（不启动进程、不弹窗）。"""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application")
        return True
    except OSError:
        return False


def extract_pdf(data: bytes) -> str:
    import fitz  # PyMuPDF

    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def extract_docx(data: bytes) -> str:
    from docx import Document

    try:
        doc = Document(BytesIO(data))  # 这版 python-docx 只收文件对象/路径，不收裸 bytes
    except Exception as exc:  # 非 Word 的 zip / 损坏文档：不同版本异常类型不同，统一收敛
        raise ValueError(f"这不是有效的 Word 文档或文件已损坏（{exc}）") from exc
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:  # 评测数字常在表格里
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append("\t".join(cells))
    return "\n".join(parts)


def extract_doc_via_word(data: bytes, workdir: Path | None = None) -> str:
    """经本机 Word COM 把 .doc 静默转成 .docx 后解析。任何失败都转成可读的 ValueError。"""
    import pythoncom
    import win32com.client

    workdir = workdir or Path(tempfile.mkdtemp(prefix="paper_upload_"))
    workdir.mkdir(parents=True, exist_ok=True)
    doc_in = workdir / "word_convert_in.doc"
    doc_out = workdir / "word_convert_out.docx"
    doc_in.write_bytes(data)
    pythoncom.CoInitialize()
    word = None
    try:
        # DispatchEx 起独立实例，不碰用户正在打开的 Word 窗口
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(str(doc_in.resolve()), ReadOnly=True)
        doc.SaveAs2(str(doc_out.resolve()), FileFormat=16)  # 16 = wdFormatXMLDocument(.docx)
        doc.Close(False)
        return extract_docx(doc_out.read_bytes())
    except Exception as exc:
        logger.warning("Word COM 转换 .doc 失败：%s", exc)
        raise ValueError(f".doc 解析失败（Word 转换出错）：{exc}") from exc
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()
        for p in (doc_in, doc_out):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def parse_paper_file(data: bytes, filename: str) -> tuple[str, str]:
    """解析上传文件，返回 (格式, 正文文本)。所有失败路径都抛带中文说明的 ValueError。"""
    fmt = detect_format(data[:8])
    try:
        if fmt == "pdf":
            text = extract_pdf(data)
        elif fmt == "docx":
            text = extract_docx(data)
        elif word_available():
            text = extract_doc_via_word(data)
        else:
            raise ValueError(
                "本机未安装 Microsoft Word，无法解析 .doc——请转存为 .docx 或 PDF 后重新上传"
            )
    except ValueError:
        raise
    except Exception as exc:  # 损坏文件、加密文档等：隔离解析异常，不让它带堆栈到上层
        logger.exception("上传文件解析异常（%s）", filename)
        raise ValueError(f"文件解析出错：{exc}") from exc

    text = text.strip()
    if not text:
        raise ValueError("未从文件中提取到文本（可能是扫描版/图片型 PDF，或空文档）")
    return fmt, text[:MAX_STORED_CHARS]


def guess_title(text: str, fallback: str) -> str:
    """从正文猜标题：取第一行像标题的文本（长度 4-200），失败退回文件名。"""
    for line in text.splitlines():
        s = line.strip()
        if 4 <= len(s) <= 200:
            return s
    return fallback

"""极简 Word（.docx）写入器：标准库手写 OOXML，零第三方依赖。

只为论文整理文档这一个场景服务：标题、小节、正文、列表与超链接，
全部用直接格式化（字号/加粗/颜色），不依赖 styles.xml 与 numbering.xml，
Word、WPS 与手机 QQ 的文件预览都能正常打开。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
    '</Types>'
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>'
)

_HYPERLINK_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)

# 超链接关系 id 从 rId100 起，避开根关系的 rId1。
_FIRST_LINK_ID = 100


def _run_xml(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
    size: int | None = None,
) -> str:
    props: list[str] = []
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _paragraph_xml(
    runs: str,
    *,
    indent: int = 0,
    before: int = 0,
    after: int = 0,
) -> str:
    ppr: list[str] = []
    spacing = (
        f'<w:spacing w:before="{before}" w:after="{after}" w:line="276" '
        'w:lineRule="auto"/>'
    )
    ppr.append(spacing)
    if indent:
        ppr.append(f'<w:ind w:left="{indent}"/>')
    return f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{runs}</w:p>"


class MiniDocx:
    """按块积累段落，最后一把写成 zip 包。"""

    def __init__(self) -> None:
        self._paragraphs: list[str] = []
        self._links: list[tuple[str, str]] = []

    def title(self, text: str) -> None:
        self._paragraphs.append(
            _paragraph_xml(
                _run_xml(text, bold=True, color="1F3864", size=36), after=120
            )
        )

    def heading(self, text: str) -> None:
        self._paragraphs.append(
            _paragraph_xml(
                _run_xml(text, bold=True, color="2E5496", size=26), before=240, after=80
            )
        )

    def paragraph(self, text: str, *, italic: bool = False, color: str | None = None) -> None:
        self._paragraphs.append(
            _paragraph_xml(_run_xml(text, italic=italic, color=color, size=21), after=80)
        )

    def meta(self, text: str) -> None:
        self._paragraphs.append(
            _paragraph_xml(_run_xml(text, italic=True, color="7F7F7F", size=18), after=120)
        )

    def bullet(self, text: str) -> None:
        # 不引入 numbering.xml，用字面圆点加缩进表达列表语义。
        self._paragraphs.append(
            _paragraph_xml(
                f"{_run_xml('• ', color='2E5496', size=21)}{_run_xml(text, size=21)}",
                indent=240,
                after=40,
            )
        )

    def link(self, label: str, url: str) -> None:
        rel_id = f"rId{_FIRST_LINK_ID + len(self._links)}"
        self._links.append((rel_id, url))
        prefix = _run_xml(f"{label}：", size=21)
        linked = (
            f'<w:hyperlink r:id="{rel_id}">'
            f'<w:r><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/>'
            f'<w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr>'
            f'<w:t xml:space="preserve">{escape(url)}</w:t></w:r>'
            f"</w:hyperlink>"
        )
        self._paragraphs.append(_paragraph_xml(f"{prefix}{linked}", indent=240, after=80))

    def save(self, path: Path) -> None:
        rels: list[str] = []
        for rel_id, url in self._links:
            rels.append(
                f'<Relationship Id="{rel_id}" Type="{_HYPERLINK_REL_TYPE}" '
                f'Target="{escape(url, {chr(34): "&quot;"})}" TargetMode="External"/>'
            )
        document_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels)
            + "</Relationships>"
        )
        body = "".join(self._paragraphs)
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<w:body>{body}"
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
            "</w:sectPr></w:body></w:document>"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
            archive.writestr("_rels/.rels", _ROOT_RELS)
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", document_rels)

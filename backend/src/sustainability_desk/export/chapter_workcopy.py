# ABOUTME: 章工作稿——把一章投影成 Markdown 并交给 genoffice 生成真正的 .docx，供业务方按章审阅修改。
# ABOUTME: 回读只产出块级「变更提议」，不自动写回；Report 仍是正文 SSOT，采纳走既有编辑通道。
# ABOUTME(en): Chapter work copy — project one chapter to Markdown, let genoffice write a real .docx for review.
# ABOUTME(en): Read-back yields block-level proposals only; Report stays the SSOT and applying uses the existing edit path.
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from sustainability_desk.contract.chapter_generation import (
    ChapterRevisionProposal,
    ChapterRevisionReport,
    ChapterWorkcopyBlock,
    ChapterWorkcopyManifest,
)
from sustainability_desk.contract.chapter_plan import chapter_sections
from sustainability_desk.contract.knowledge_packages import knowledge_package_of
from sustainability_desk.contract.language import list_separator
from sustainability_desk.contract.models import Block, Report, Section
from sustainability_desk.contract.renderability import renderable_table_rows
from sustainability_desk.contract.section_titles import (
    paragraph_fingerprint,
    resolved_display_title,
)
from sustainability_desk.contract.visibility import visible
from sustainability_desk.export.docx_renderer import inline_text

WORKCOPY_SCHEMA_VERSION = "sustainability_desk.chapter_workcopy.v1"
GENOFFICE_CLI_ENV = "SUSTAINABILITY_DESK_GENOFFICE_CLI"


class GenOfficeUnavailableError(RuntimeError):
    """本机没有 genoffice CLI：工作稿功能不可用，报告主链不受影响。"""


class GenOfficeCommandError(RuntimeError):
    """genoffice 返回非零或不可解析的结果；code 进日志与界面提示，detail 只进日志。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WorkcopySnapshotBlock:
    """导出瞬间的一块正文；只留在服务端，用于回读对齐（不进提交给协作方的清单）。"""

    block_id: str
    section_key: str
    text: str


@dataclass(frozen=True)
class WorkcopyBundle:
    """一章的工作稿：Markdown 源 + 公开清单 + 服务端对齐快照。"""

    markdown: str
    manifest: ChapterWorkcopyManifest
    snapshot: tuple[WorkcopySnapshotBlock, ...]


@dataclass(frozen=True)
class WorkcopyReadback:
    """回读结果：正文段落、批注与修订原样保留，供后续按需投影。"""

    paragraphs: tuple[str, ...]
    comments: tuple[dict[str, Any], ...]
    revisions: tuple[dict[str, Any], ...]


def _normalize(text: str) -> str:
    """对齐用的文本归一：去零宽、折叠空白、去首尾。

    只用于比较，不回写；全角/半角差异保留——那可能是作者真的改过的内容。
    """

    cleaned = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _paragraph_text(block: Block, report: Report) -> str:
    return inline_text(block.content, report).strip()


def _cell_text(cell: Any, report: Report) -> str:
    """单元格的展示文本。真相是 `value`；Plate 文本骨架（children）只是编辑器骨架，
    其元素是裸 dict（无 Inline 模型），不能交给 `inline_text` 解析。"""

    value = cell.value
    if isinstance(value, list):
        separator = list_separator(knowledge_package_of(report).language)
        return _escape_cell(separator.join(str(item) for item in value if str(item).strip()))
    if value is None:
        return ""
    return _escape_cell(str(value))


def _table_markdown(block: Block, report: Report) -> str:
    """表格按 markdown 三线表投影；表格改动在 v1 不回读（见 diff_workcopy 说明）。"""

    rows = renderable_table_rows(block.table, report, block.id)
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        cells = [_cell_text(cell, report) for cell in row.children]
        if not cells:
            continue
        lines.append("| " + " | ".join(cells) + " |")
        if row.headerRow:
            lines.append("| " + " | ".join("---" for _ in cells) + " |")
    return "\n".join(lines)


def _section_heading(section: Section, report: Report) -> str:
    level = min(max(section.headingLevel, 1), 6)
    return "#" * level + " " + resolved_display_title(section, report)


def _readable_blocks(section: Section, report: Report) -> Iterable[Block]:
    for block in section.blocks:
        if not visible(block, report) or block.state == "omitted":
            continue
        yield block


def build_workcopy(
    report: Report,
    *,
    report_id: UUID,
    chapter_key: str,
    contract_version: str,
    state_seq: int,
    exported_at: datetime | None = None,
) -> WorkcopyBundle:
    """把一章投影成工作稿；标题层级与引用解析与正式导出共用同一套实现。"""

    lines: list[str] = []
    anchors: list[ChapterWorkcopyBlock] = []
    snapshot: list[WorkcopySnapshotBlock] = []
    for section in chapter_sections(report, chapter_key):
        lines.append(_section_heading(section, report))
        lines.append("")
        for block in _readable_blocks(section, report):
            if block.type == "paragraph":
                text = _paragraph_text(block, report)
                if not text:
                    continue
                lines.append(("- " if block.listType else "") + text)
                lines.append("")
                anchors.append(
                    ChapterWorkcopyBlock(
                        block_id=block.id,
                        section_key=section.key,
                        kind="paragraph",
                        text_fingerprint=paragraph_fingerprint(
                            [{"kind": "text", "text": text}]
                        ),
                    )
                )
                snapshot.append(
                    WorkcopySnapshotBlock(
                        block_id=block.id, section_key=section.key, text=text
                    )
                )
            elif block.type == "table" and block.table is not None:
                table = _table_markdown(block, report)
                if table:
                    lines.append(table)
                    lines.append("")
            # 图片与派生图表不进工作稿：没有可编辑文本，回读也无从对齐。
    manifest = ChapterWorkcopyManifest(
        schema_version=WORKCOPY_SCHEMA_VERSION,
        report_id=report_id,
        chapter_key=chapter_key,
        contract_version=contract_version,
        state_seq=state_seq,
        exported_at=(exported_at or datetime.now(timezone.utc)).isoformat(),
        blocks=anchors,
    )
    return WorkcopyBundle(
        markdown="\n".join(lines).strip() + "\n",
        manifest=manifest,
        snapshot=tuple(snapshot),
    )


_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def chapter_slug(chapter_key: str) -> str:
    """章 key → 可安全用作文件名的一段。

    章 key 来自 URL；直接拼进路径会让 `../` 逃出产物根目录——GET 会写到根外，
    回读接口会读到根外。只保留字母数字与 `._-`，其余折叠为下划线。
    """

    slug = _UNSAFE_PATH_CHARS.sub("_", chapter_key).strip("._")
    return (slug or "chapter")[:120]


def snapshot_path(
    root: Path, *, report_id: UUID, chapter_key: str, state_seq: int
) -> Path:
    """工作稿快照的落盘位置：按报告/章/状态序号分文件，回读据此对齐同一份正文。

    文件名用 `chapter_slug` 归一：同一个章在导出与回读两侧得到同一个文件名，
    而任意 URL 片段都无法把路径引出 root。
    """

    return (
        root
        / "chapter-workcopy"
        / str(report_id)
        / f"{chapter_slug(chapter_key)}-{state_seq}.json"
    )


def store_snapshot(root: Path, bundle: WorkcopyBundle) -> Path:
    """保存导出快照（正文 + 清单），供协作方回传时对齐；不含内部字段与路径。"""

    manifest = bundle.manifest
    path = snapshot_path(
        root,
        report_id=manifest.report_id,
        chapter_key=manifest.chapter_key,
        state_seq=manifest.state_seq,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "snapshot": [
            {"block_id": item.block_id, "section_key": item.section_key, "text": item.text}
            for item in bundle.snapshot
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_snapshot(path: Path) -> tuple[ChapterWorkcopyManifest, tuple[WorkcopySnapshotBlock, ...]]:
    """读回导出快照；文件缺失即「这份工作稿不是本机导出的」，由调用方拒绝回读。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = ChapterWorkcopyManifest.model_validate(payload["manifest"])
    snapshot = tuple(
        WorkcopySnapshotBlock(
            block_id=item["block_id"],
            section_key=item["section_key"],
            text=item["text"],
        )
        for item in payload.get("snapshot") or ()
    )
    return manifest, snapshot


def genoffice_cli() -> str:
    """定位 genoffice CLI；未安装时明确报不可用，而不是静默降级成纯 Markdown。"""
    configured = os.getenv(GENOFFICE_CLI_ENV)
    if configured:
        return configured
    found = shutil.which("genoffice")
    if found:
        return found
    raise GenOfficeUnavailableError("genoffice CLI 未安装或不在 PATH")


def _run_genoffice(args: list[str], *, timeout: float = 180.0) -> dict[str, Any]:
    cli = genoffice_cli()
    completed = subprocess.run(
        [cli, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    stdout = completed.stdout.strip()
    if not stdout:
        raise GenOfficeCommandError("empty_output", completed.stderr.strip()[:500])
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise GenOfficeCommandError("invalid_json", stdout[:500]) from error
    if payload.get("status") != "ok":
        code = str(payload.get("error") or "command_failed")
        raise GenOfficeCommandError(code, json.dumps(payload, ensure_ascii=False)[:500])
    return payload


def render_workcopy_docx(bundle: WorkcopyBundle, out_path: Path) -> Path:
    """用 genoffice 把工作稿 Markdown 写成真正的 .docx（原生 OOXML，Word 可直接打开）。

    正式交付物仍由 `export/docx_renderer.render_final_docx` 产出；本函数只产
    「可供协作方批注修改的章节工作稿」，两种产物语义不同，不得互相替代。
    """

    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chapter-workcopy-") as tmp:
        source = Path(tmp) / "chapter.md"
        source.write_text(bundle.markdown, encoding="utf-8")
        _run_genoffice(
            [
                "create",
                "--type",
                "docx",
                "--from",
                str(source),
                "--out",
                str(out_path),
                "--force",
                "--json",
            ]
        )
    if not out_path.exists():
        raise GenOfficeCommandError("output_missing", str(out_path))
    return out_path


def attach_review_comments(
    docx_path: Path,
    *,
    comments: Iterable[tuple[int, str]],
    author: str = "Sustainability Workbench",
) -> int:
    """把系统的待确认项写成 Word 批注，锚在指定段落序号；返回写入条数。

    批注是给协作方看的，不改变正文；正文判定与写回仍由确定性闸门与 Report 承担。
    """

    ops = [
        {"op": "add_comment", "blockIndex": index, "comment": body, "author": author}
        for index, body in comments
        if index >= 0 and body.strip()
    ]
    if not ops:
        return 0
    with tempfile.TemporaryDirectory(prefix="chapter-comments-") as tmp:
        ops_path = Path(tmp) / "ops.json"
        ops_path.write_text(
            json.dumps({"ops": ops}, ensure_ascii=False), encoding="utf-8"
        )
        _run_genoffice(
            ["docs", "apply", str(docx_path), "--ops", str(ops_path), "--force", "--json"]
        )
    return len(ops)


def read_workcopy(docx_path: Path) -> WorkcopyReadback:
    """回读协作方的 .docx：正文段落 + 批注 + 修订（含 blockIndex）。"""

    payload = _run_genoffice(
        [
            "docs",
            "read",
            str(docx_path),
            "--json",
            "--full",
            "--comments",
            "--revisions",
        ]
    )
    detail = payload.get("detail") or {}
    paragraphs: list[str] = []
    for item in detail.get("items") or []:
        if item.get("type") not in {"paragraph", "listItem"}:
            continue
        text = _normalize(str(item.get("text") or ""))
        if text:
            paragraphs.append(text)
    return WorkcopyReadback(
        paragraphs=tuple(paragraphs),
        comments=tuple(detail.get("comments") or ()),
        revisions=tuple(detail.get("revisions") or ()),
    )


def _anchor_section(
    snapshot: tuple[WorkcopySnapshotBlock, ...], index: int
) -> str:
    if not snapshot:
        return ""
    if index < 0:
        return snapshot[0].section_key
    return snapshot[min(index, len(snapshot) - 1)].section_key


def diff_workcopy(
    snapshot: tuple[WorkcopySnapshotBlock, ...],
    readback: WorkcopyReadback,
    *,
    chapter_key: str,
    base_state_seq: int,
) -> ChapterRevisionReport:
    """块级变更提议：以导出快照与回读段落做序列对齐，产出 modified/added/removed。

    对齐用 SequenceMatcher 而非逐段位置比较：作者在章中间插入一段时，后续段落
    仍然被认成「同一块」，不会整章变成一堆 modified。

    v1 只处理正文段落：标题走「章节标题」通道，表格/图片在 Word 里不做结构级回读
    （需要行列级 diff，超出「按章写作」的当前需求）。无法对齐的段落计入
    `unmatched_paragraphs`，不静默丢弃。
    """

    before = [_normalize(item.text) for item in snapshot]
    after = [_normalize(text) for text in readback.paragraphs]
    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    proposals: list[ChapterRevisionProposal] = []
    unmatched = 0

    def add_paragraphs(start: int, end: int, anchor_index: int) -> None:
        nonlocal unmatched
        for index in range(start, end):
            text = after[index]
            if not text:
                unmatched += 1
                continue
            proposals.append(
                ChapterRevisionProposal(
                    block_id=f"new:{anchor_index}:{index}",
                    section_key=_anchor_section(snapshot, anchor_index),
                    kind="added",
                    current_text="",
                    proposed_text=text,
                    anchor_block_id=(
                        snapshot[anchor_index].block_id if anchor_index >= 0 else None
                    ),
                )
            )

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            add_paragraphs(j1, j2, i1 - 1)
            continue
        if tag == "delete":
            for index in range(i1, i2):
                proposals.append(
                    ChapterRevisionProposal(
                        block_id=snapshot[index].block_id,
                        section_key=snapshot[index].section_key,
                        kind="removed",
                        current_text=snapshot[index].text,
                        proposed_text="",
                    )
                )
            continue
        # replace：同类位置配对为 modified，多出的分别算 removed / added
        paired = min(i2 - i1, j2 - j1)
        for step in range(paired):
            block = snapshot[i1 + step]
            proposals.append(
                ChapterRevisionProposal(
                    block_id=block.block_id,
                    section_key=block.section_key,
                    kind="modified",
                    current_text=block.text,
                    proposed_text=after[j1 + step],
                )
            )
        for index in range(i1 + paired, i2):
            proposals.append(
                ChapterRevisionProposal(
                    block_id=snapshot[index].block_id,
                    section_key=snapshot[index].section_key,
                    kind="removed",
                    current_text=snapshot[index].text,
                    proposed_text="",
                )
            )
        add_paragraphs(j1 + paired, j2, i2 - 1)

    return ChapterRevisionReport(
        chapter_key=chapter_key,
        base_state_seq=base_state_seq,
        proposals=proposals,
        unmatched_paragraphs=unmatched,
    )

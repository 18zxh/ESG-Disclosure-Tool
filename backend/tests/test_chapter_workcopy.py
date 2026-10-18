# ABOUTME: 章工作稿的投影与回读对齐测试——Markdown 切片、块清单、modified/added/removed 提议。
# ABOUTME: 含一条真实 genoffice 往返（本机无 CLI 时跳过）：验证「导出→回读」不产生假变更。
from __future__ import annotations

import shutil
from uuid import uuid4

import pytest

from sustainability_desk.contract.chapter_generation import ChapterWorkcopyBlock
from sustainability_desk.contract.chapter_plan import derive_chapters
from sustainability_desk.contract.models import (
    Block,
    ExplicitGenerationEvidenceSelector,
    GenerationInputs,
    GenerationSpec,
    GenerationTask,
    Inline,
    Report,
    Section,
)
from sustainability_desk.export import chapter_workcopy


def _generative(block_id: str, text: str, *, state: str = "ready") -> Block:
    return Block(
        id=block_id,
        type="paragraph",
        blockType="generative",
        source="ai",
        state=state,
        content=[Inline(kind="text", text=text)],
        generation=GenerationSpec(
            task=GenerationTask(focus="说明当前内容单元的管理实践。"),
            inputs=GenerationInputs(
                evidence=ExplicitGenerationEvidenceSelector(kind="explicit")
            ),
        ),
    )


def _report() -> Report:
    return Report(
        title="测试报告",
        sections=[
            Section(
                key="about_report",
                title="关于本报告",
                headingLevel=1,
                children=[
                    Section(
                        key="about_report.scope",
                        title="报告范围",
                        headingLevel=2,
                        blocks=[
                            _generative("about.scope", "本报告覆盖公司本部。"),
                            _generative("about.boundary", "数据边界与财务报表一致。"),
                        ],
                    ),
                    Section(
                        key="about_report.feedback",
                        title="读者反馈",
                        headingLevel=2,
                        blocks=[_generative("about.feedback", "欢迎读者提出意见。")],
                    ),
                ],
            )
        ],
    )


def _bundle(report: Report):
    return chapter_workcopy.build_workcopy(
        report,
        report_id=uuid4(),
        chapter_key="about_report",
        contract_version="sustainability_desk.report.v4",
        state_seq=7,
    )


def test_build_workcopy_projects_headings_and_ready_prose_only() -> None:
    report = _report()
    report.sections[0].children[0].blocks[1].state = "omitted"

    bundle = _bundle(report)

    assert "# 关于本报告" in bundle.markdown
    assert "## 报告范围" in bundle.markdown
    assert "本报告覆盖公司本部。" in bundle.markdown
    # 受控省略的块不出现，也不进清单：写作稿里不该有「系统占位」。
    assert "数据边界" not in bundle.markdown
    assert [block.block_id for block in bundle.manifest.blocks] == [
        "about.scope",
        "about.feedback",
    ]
    assert [item.text for item in bundle.snapshot] == [
        "本报告覆盖公司本部。",
        "欢迎读者提出意见。",
    ]
    assert bundle.manifest.state_seq == 7
    assert all(len(block.text_fingerprint) == 64 for block in bundle.manifest.blocks)


def test_workcopy_covers_every_generatable_block_of_the_chapter() -> None:
    report = _report()
    chapter = derive_chapters(report)[0]
    bundle = _bundle(report)

    assert tuple(block.block_id for block in bundle.manifest.blocks) == chapter.block_ids


def _readback(*paragraphs: str) -> chapter_workcopy.WorkcopyReadback:
    return chapter_workcopy.WorkcopyReadback(
        paragraphs=paragraphs, comments=(), revisions=()
    )


def test_diff_detects_modification_without_cascading() -> None:
    bundle = _bundle(_report())
    readback = _readback(
        "本报告覆盖公司本部及其两家子公司。",
        "数据边界与财务报表一致。",
        "欢迎读者提出意见。",
    )

    report = chapter_workcopy.diff_workcopy(
        bundle.snapshot,
        readback,
        chapter_key="about_report",
        base_state_seq=7,
    )

    assert [(item.block_id, item.kind) for item in report.proposals] == [
        ("about.scope", "modified")
    ]
    assert report.proposals[0].proposed_text == "本报告覆盖公司本部及其两家子公司。"
    assert report.proposals[0].current_text == "本报告覆盖公司本部。"
    assert report.unmatched_paragraphs == 0


def test_diff_treats_middle_insertion_as_addition_anchored_to_previous_block() -> None:
    bundle = _bundle(_report())
    readback = _readback(
        "本报告覆盖公司本部。",
        "报告期内公司未发生重大重组。",
        "数据边界与财务报表一致。",
        "欢迎读者提出意见。",
    )

    report = chapter_workcopy.diff_workcopy(
        bundle.snapshot,
        readback,
        chapter_key="about_report",
        base_state_seq=7,
    )

    kinds = [(item.block_id.split(":")[0] if item.kind == "added" else item.block_id, item.kind) for item in report.proposals]
    assert kinds == [("new", "added")]
    added = report.proposals[0]
    assert added.proposed_text == "报告期内公司未发生重大重组。"
    assert added.anchor_block_id == "about.scope"
    assert added.section_key == "about_report.scope"


def test_diff_detects_removal_and_keeps_section_attribution() -> None:
    bundle = _bundle(_report())
    readback = _readback("本报告覆盖公司本部。", "欢迎读者提出意见。")

    report = chapter_workcopy.diff_workcopy(
        bundle.snapshot,
        readback,
        chapter_key="about_report",
        base_state_seq=7,
    )

    assert [(item.block_id, item.kind) for item in report.proposals] == [
        ("about.boundary", "removed")
    ]
    assert report.proposals[0].section_key == "about_report.scope"
    assert report.proposals[0].current_text == "数据边界与财务报表一致。"


def test_diff_reports_no_proposal_when_nothing_changed() -> None:
    bundle = _bundle(_report())

    report = chapter_workcopy.diff_workcopy(
        bundle.snapshot,
        _readback(*(item.text for item in bundle.snapshot)),
        chapter_key="about_report",
        base_state_seq=7,
    )

    assert report.proposals == []
    assert report.chapter_key == "about_report"
    assert report.base_state_seq == 7


@pytest.mark.skipif(
    shutil.which(chapter_workcopy.GENOFFICE_CLI_ENV) is None
    and shutil.which("genoffice") is None,
    reason="本机未安装 genoffice CLI",
)
def test_genoffice_round_trip_produces_no_phantom_revisions(tmp_path) -> None:
    """导出 → genoffice 写 .docx → 回读：未改动的章不应产生任何提议。"""

    bundle = _bundle(_report())
    target = chapter_workcopy.render_workcopy_docx(bundle, tmp_path / "about.docx")

    readback = chapter_workcopy.read_workcopy(target)
    report = chapter_workcopy.diff_workcopy(
        bundle.snapshot,
        readback,
        chapter_key="about_report",
        base_state_seq=7,
    )

    assert report.proposals == []
    assert report.unmatched_paragraphs == 0
    assert len(readback.paragraphs) == len(bundle.snapshot)


def test_manifest_blocks_are_strictly_typed() -> None:
    bundle = _bundle(_report())

    for block in bundle.manifest.blocks:
        assert isinstance(block, ChapterWorkcopyBlock)
        assert block.kind == "paragraph"


def test_workcopy_renders_table_cells_from_controlled_value() -> None:
    """表格走受控值（value），不是 Plate 文本骨架——骨架是裸 dict，交给行内解析会崩。"""

    from sustainability_desk.contract.models import GsTable, GsTableCell, GsTableRow

    report = _report()
    report.sections[0].children[0].blocks.append(
        Block(
            id="about.metrics",
            type="table",
            blockType="fixed",
            source="template",
            state="ready",
            table=GsTable(
                children=[
                    GsTableRow(
                        headerRow=True,
                        children=[
                            GsTableCell(type="th", value="指标"),
                            GsTableCell(type="th", value="2025 年"),
                        ],
                    ),
                    GsTableRow(
                        children=[
                            GsTableCell(value="温室气体排放"),
                            GsTableCell(value=["范围一", "范围二"]),
                        ],
                    ),
                ]
            ),
        )
    )
    report.knowledgePackageId = "sse_zh_hans"

    bundle = _bundle(report)

    assert "| 指标 | 2025 年 |" in bundle.markdown
    assert "| --- | --- |" in bundle.markdown
    assert "| 温室气体排放 | 范围一、范围二 |" in bundle.markdown
    # 表格不进块清单（v1 不做表格级回读），不进正文快照。
    assert "about.metrics" not in [block.block_id for block in bundle.manifest.blocks]


def test_snapshot_path_cannot_escape_the_artifact_root(tmp_path) -> None:
    """章 key 来自 URL：`../` 不得把快照写出产物根目录。"""

    root = tmp_path / "artifacts"
    escaped = chapter_workcopy.snapshot_path(
        root,
        report_id=uuid4(),
        chapter_key="../../etc/passwd",
        state_seq=7,
    )

    assert escaped.resolve().is_relative_to(root.resolve())
    assert "../" not in str(escaped)
    assert chapter_workcopy.chapter_slug("env") == "env"

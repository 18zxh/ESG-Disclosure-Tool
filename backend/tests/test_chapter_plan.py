# ABOUTME: 章节目录与完成度派生的纯函数测试——章边界、节清单、状态、续写口径与指纹聚合。
# ABOUTME: 不连库、不调模型：章节层若在这些判定上出错，批次编排会按错的范围跑。
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sustainability_desk.contract.chapter_plan import (
    chapter_fingerprint,
    chapter_for_section,
    chapter_sections,
    derive_chapters,
    draft_facts,
    following_action,
    is_generatable_block,
    pending_section_keys,
)
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


def _generative(block_id: str, *, state: str | None = None, text: str | None = None) -> Block:
    return Block(
        id=block_id,
        type="paragraph",
        blockType="generative",
        source="ai",
        state=state,
        content=[Inline(kind="text", text=text)] if text else None,
        generation=GenerationSpec(
            task=GenerationTask(focus="说明当前内容单元的管理实践。"),
            inputs=GenerationInputs(
                evidence=ExplicitGenerationEvidenceSelector(kind="explicit")
            ),
        ),
    )


def _fixed(block_id: str, text: str) -> Block:
    return Block(
        id=block_id,
        type="paragraph",
        blockType="fixed",
        source="template",
        content=[Inline(kind="text", text=text)],
    )


def _report() -> Report:
    """两章：第一章两节（一节可生成、一节纯固定），附录全固定。"""

    return Report(
        title="测试报告",
        sections=[
            Section(
                key="about_report",
                title="关于本报告",
                headingLevel=1,
                blocks=[_fixed("about.opening", "本公司秉持可持续发展理念。")],
                children=[
                    Section(
                        key="about_report.scope",
                        title="报告范围",
                        headingLevel=2,
                        blocks=[_generative("about.scope", text="范围覆盖公司本部。")],
                    ),
                    Section(
                        key="about_report.notes",
                        title="编制说明",
                        headingLevel=2,
                        blocks=[_fixed("about.notes", "本报告以简体中文编制。")],
                    ),
                ],
            ),
            Section(
                key="report_appendix",
                title="附录",
                headingLevel=1,
                blocks=[_fixed("appendix.note", "附录固定说明。")],
            ),
        ],
    )


def test_derive_chapters_uses_top_level_sections_as_chapters() -> None:
    chapters = derive_chapters(_report())

    assert [chapter.key for chapter in chapters] == ["about_report", "report_appendix"]
    assert [chapter.ordinal for chapter in chapters] == [1, 2]
    about, appendix = chapters
    # 只有含可生成块的节进入生成清单；纯固定节只出现在 all_section_keys 里。
    assert about.section_keys == ("about_report.scope",)
    assert about.all_section_keys == (
        "about_report",
        "about_report.scope",
        "about_report.notes",
    )
    assert about.block_ids == ("about.scope",)
    assert appendix.has_generation is False
    assert appendix.is_appendix is True


def test_fixed_only_chapter_reports_as_fixed_not_empty() -> None:
    facts = draft_facts(_report(), "report_appendix")

    assert facts.generatable_blocks == 0
    assert facts.status == "fixed"
    assert following_action(facts.status, "not_generated") == "review"


def test_draft_status_progresses_from_empty_to_partial_to_drafted() -> None:
    report = _report()
    scope_section = report.sections[0].children[0]

    scope_section.blocks = [_generative("about.scope"), _generative("about.detail")]
    assert draft_facts(report, "about_report").status == "empty"

    scope_section.blocks[0].state = "ready"
    scope_section.blocks[0].content = [Inline(kind="text", text="已生成正文。")]
    facts = draft_facts(report, "about_report")
    assert (facts.ready_blocks, facts.pending_blocks) == (1, 1)
    assert facts.status == "partial"
    assert following_action(facts.status, "not_generated") == "continue"

    scope_section.blocks[1].state = "omitted"
    assert draft_facts(report, "about_report").status == "drafted"


def test_controlled_omission_counts_as_settled_but_never_as_ready() -> None:
    report = _report()
    report.sections[0].children[0].blocks = [_generative("about.scope", state="omitted")]

    facts = draft_facts(report, "about_report")
    assert (facts.ready_blocks, facts.omitted_blocks, facts.pending_blocks) == (0, 1, 0)
    assert facts.status == "drafted"


def test_pending_section_keys_only_lists_unsettled_sections() -> None:
    report = _report()
    scope_section = report.sections[0].children[0]
    scope_section.blocks = [
        _generative("about.scope", state="ready", text="已生成。"),
        _generative("about.detail"),
    ]

    assert pending_section_keys(report, "about_report") == ("about_report.scope",)

    scope_section.blocks[1].state = "ready"
    scope_section.blocks[1].content = [Inline(kind="text", text="第二段。")]
    assert pending_section_keys(report, "about_report") == ()


def test_chapter_for_section_resolves_nested_sections() -> None:
    chapter = chapter_for_section(_report(), "about_report.scope")

    assert chapter is not None
    assert chapter.key == "about_report"
    assert chapter_sections(_report(), "about_report")[0].key == "about_report"


def test_chapter_fingerprint_is_order_independent_and_input_sensitive() -> None:
    first = chapter_fingerprint("ch", {"a": "1" * 64, "b": "2" * 64})
    second = chapter_fingerprint("ch", {"b": "2" * 64, "a": "1" * 64})
    changed = chapter_fingerprint("ch", {"a": "1" * 64, "b": "3" * 64})

    assert first == second
    assert first != changed
    assert len(first) == 64


def test_generation_predicate_excludes_package_assets() -> None:
    assert is_generatable_block(_generative("x")) is True
    assert is_generatable_block(_fixed("y", "固定正文")) is False
    # 有生成规格但类型是 slot 时仍不进生成队列：slot 的值来自 fields，不由模型写。
    slot = _generative("z")
    slot.blockType = "slot"
    assert is_generatable_block(slot) is False


def test_select_section_keys_separates_continue_from_full_rewrite() -> None:
    from sustainability_desk.chapter_generation import (
        ChapterEmptyError,
        ChapterUnknownSectionError,
        select_section_keys,
    )

    report = _report()
    scope_section = report.sections[0].children[0]
    scope_section.blocks = [
        _generative("about.scope", state="ready", text="已生成。"),
    ]
    chapter = derive_chapters(report)[0]

    # 续写：没有待写小节时返回空，由调用方转成「已写完」而不是内容缺失。
    assert select_section_keys(report, chapter, mode="missing", requested=None) == ()
    # 整章重写：仍然包含已写好的节。
    assert select_section_keys(report, chapter, mode="all", requested=None) == (
        "about_report.scope",
    )

    with pytest.raises(ChapterUnknownSectionError):
        select_section_keys(
            report, chapter, mode="missing", requested=["report_appendix"]
        )
    with pytest.raises(ChapterEmptyError):
        select_section_keys(
            report, chapter, mode="all", requested=["about_report.notes"]
        )


def test_ready_blocks_count_as_settled_even_when_not_renderable() -> None:
    """complete_coverage 下 assessment 来源的块不进入正文，但「写完了」仍是事实。

    若把可渲染性算进完成度，这类块永远 pending：章永久 partial、mode=missing 每次重跑
    同一节，用户看到「继续写」却永远写不完。
    """

    from sustainability_desk.contract.models import ReportMeta

    report = _report()
    report.meta = ReportMeta(materialityStrategy="complete_coverage")
    scope_section = report.sections[0].children[0]
    assessment_block = _generative("about.coverage", state="ready", text="覆盖率投影正文。")
    assessment_block.source = "assessment"
    scope_section.blocks = [assessment_block]

    facts = draft_facts(report, "about_report")

    assert (facts.ready_blocks, facts.pending_blocks) == (1, 0)
    assert facts.status == "drafted"
    assert pending_section_keys(report, "about_report") == ()


@pytest.mark.parametrize("chapter_key", ["about_report", "report_appendix"])
async def test_enqueue_without_pending_sections_returns_the_domain_reason(
    chapter_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completed and fixed-only chapters must not fail with an undefined variable."""
    import sustainability_desk.chapter_generation as generation

    report = _report()
    report.sections[0].children[0].blocks[0].state = "ready"
    scope = SimpleNamespace(knowledge_package=None, project_report=lambda value: value)
    snapshot = SimpleNamespace(state_seq=1, state={})
    monkeypatch.setattr(generation, "current_generation_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr(
        generation.reports_dal, "get_lightweight_v4_snapshot", AsyncMock(return_value=snapshot)
    )
    monkeypatch.setattr(generation, "build_report_revision", lambda *args, **kwargs: report)
    enqueue = AsyncMock()
    monkeypatch.setattr(generation.chapter_dal, "enqueue_run", enqueue)
    expected = (
        generation.ChapterUpToDateError
        if chapter_key == "about_report"
        else generation.ChapterEmptyError
    )

    with pytest.raises(expected):
        await generation.enqueue_chapter_generation(
            None,
            account_id=uuid4(),
            report_id=uuid4(),
            chapter_key=chapter_key,
            mode="missing",
            section_keys=None,
            base_state_seq=1,
            idempotency_key=uuid4(),
            model_id="luna",
        )

    enqueue.assert_not_awaited()

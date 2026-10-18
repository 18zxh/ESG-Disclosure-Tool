# ABOUTME: 章节（Chapter）作为生成/编辑/管理的一等工作单位——从 Report 骨架派生，只读不改 Report。
# ABOUTME: 章节只是编排单位：写回仍是「整节原子提交」，章不复制正文、不持第二份真相。
# ABOUTME(en): Chapters are the first-class unit of batch generation, editing and management, derived from the
# ABOUTME(en): Report skeleton. Orchestration only: write-back stays whole-section atomic; a chapter owns no prose.
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Mapping

from sustainability_desk.contract.models import Block, Report, Section
from sustainability_desk.contract.visibility import visible

# 可生成块的判据只有一处：这里。generate_all 与本模块共用，避免「章认为可生成、
# 编排认为不可生成」两套口径在某次合同升级后给出相反答案。
GENERATABLE_BLOCK_TYPES: frozenset[str] = frozenset({"constrained", "generative"})

# fixed=本章无可生成块（前四章的固定正文、附录固定段）；empty=一个都没写；partial=写了一部分；
# drafted=章内可生成块均已落定（ready 或受控省略）。
ChapterDraftStatus = Literal["fixed", "empty", "partial", "drafted"]
# 章级下一步动作由事实决定，不由界面猜测：用户看到的是「该做什么」，不是一串状态码。
ChapterNextAction = Literal["generate", "continue", "refresh", "review"]
# 生命周期是人工管理的字段（审阅/锁定），与正文完成度正交；生成不推进它。
ChapterLifecycle = Literal["draft", "reviewing", "locked"]
# 章级新鲜度：need 最近一次成功批次的指纹与当前输入比较；未生成过时是 not_generated。
ChapterFreshness = Literal["not_generated", "fresh", "stale"]


def is_generatable_block(block: Block) -> bool:
    """本块是否需要（且允许）由模型生成正文/表格。

    blockType 为 constrained/generative 且带生成规格。fixed/slot 是包资产：前者整段由包写定，
    后者是包写定的文字加字段占位，都不进入生成队列。
    """

    return block.blockType in GENERATABLE_BLOCK_TYPES and block.generation is not None


@dataclass(frozen=True)
class ChapterRef:
    """报告骨架中的一章：稳定 key、序位、参与生成的节与块（均按文档预序）。

    ``section_keys`` 只含**有可生成块的可见节**——这是批次编排实际要跑的清单；
    ``all_section_keys`` 含章内全部可见节，供界面与导出切片使用。两者分开是为了让
    「这一章要生成几次」与「这一章有多少节」不再混为同一个数字。
    """

    key: str
    ordinal: int
    title: str
    module_id: str | None
    is_appendix: bool
    all_section_keys: tuple[str, ...]
    section_keys: tuple[str, ...]
    block_ids: tuple[str, ...]

    @property
    def has_generation(self) -> bool:
        return bool(self.section_keys)


@dataclass(frozen=True)
class ChapterDraftFacts:
    """一章的完成度事实；全部由块状态投影，不做启发式推断。"""

    key: str
    generatable_blocks: int
    ready_blocks: int
    omitted_blocks: int

    @property
    def pending_blocks(self) -> int:
        return max(0, self.generatable_blocks - self.ready_blocks - self.omitted_blocks)

    @property
    def status(self) -> ChapterDraftStatus:
        if self.generatable_blocks == 0:
            return "fixed"
        if self.ready_blocks == 0 and self.omitted_blocks == 0:
            return "empty"
        if self.pending_blocks == 0:
            return "drafted"
        return "partial"


def following_action(
    status: ChapterDraftStatus,
    freshness: ChapterFreshness,
) -> ChapterNextAction:
    """章级下一步动作的唯一判定；界面按钮与接口提示共用本结论。"""

    if status == "fixed":
        return "review"
    if status == "empty":
        return "generate"
    if status == "partial":
        return "continue"
    return "refresh" if freshness == "stale" else "review"


def _visible_sections(sections: list[Section], report: Report) -> list[Section]:
    """按文档预序展平可见节；隐藏的父节连同子树一并跳过（与生成侧同口径）。"""

    out: list[Section] = []
    for section in sections:
        if not visible(section, report):
            continue
        out.append(section)
        if section.children:
            out.extend(_visible_sections(section.children, report))
    return out


def _section_generatable_blocks(section: Section, report: Report) -> list[Block]:
    return [
        block
        for block in section.blocks
        if is_generatable_block(block) and visible(block, report)
    ]


def derive_chapters(report: Report) -> tuple[ChapterRef, ...]:
    """按报告骨架派生章节清单：顶层可见节即一章，其子树为章内各节。

    不新增契约字段、不改 Report：章节身份沿用 Section.key 这一既有稳定标识，
    因此存量报告、导出与前端锚点不需要迁移。附录按 key 前缀识别（``report_appendix``），
    仅用于界面排序与「正文/附录」分组，不参与生成语义。
    """

    chapters: list[ChapterRef] = []
    for ordinal, top in enumerate(
        [section for section in report.sections if visible(section, report)], start=1
    ):
        subtree = _visible_sections([top], report)
        all_keys = tuple(section.key for section in subtree)
        generating_keys: list[str] = []
        block_ids: list[str] = []
        for section in subtree:
            blocks = _section_generatable_blocks(section, report)
            if not blocks:
                continue
            generating_keys.append(section.key)
            block_ids.extend(block.id for block in blocks)
        chapters.append(
            ChapterRef(
                key=top.key,
                ordinal=ordinal,
                title=top.title,
                module_id=top.reportModuleId,
                is_appendix=top.key == "report_appendix"
                or top.key.startswith("report_appendix."),
                all_section_keys=all_keys,
                section_keys=tuple(generating_keys),
                block_ids=tuple(block_ids),
            )
        )
    return tuple(chapters)


def chapter_for_section(report: Report, section_key: str) -> ChapterRef | None:
    """定位某节所属的章；服务端校验「请求的节确实属于该章」时用。"""

    for chapter in derive_chapters(report):
        if section_key == chapter.key or section_key in chapter.all_section_keys:
            return chapter
    return None


def chapter_sections(report: Report, chapter_key: str) -> tuple[Section, ...]:
    """一章的可见节（预序）；章工作区与章级工作稿导出共用本切片。"""

    for top in report.sections:
        if top.key != chapter_key:
            continue
        return tuple(_visible_sections([top], report))
    return ()


def chapter_blocks(report: Report, chapter_key: str) -> tuple[Block, ...]:
    """一章的可生成块（预序）；未知章返回空。"""

    out: list[Block] = []
    for section in chapter_sections(report, chapter_key):
        out.extend(_section_generatable_blocks(section, report))
    return tuple(out)


def draft_facts(report: Report, chapter_key: str) -> ChapterDraftFacts:
    """从块状态投影完成度：「已落定」只看块状态（ready / 受控省略）。

    可渲染性不参与完成度。它回答的是「这段会不会出现在交付物里」，与「这一节写完了没有」
    是两个问题：`complete_coverage` 报告里 assessment 来源的块会被判不可渲染，若把它们
    排除在 ready 之外，章会永久停在 partial，`mode=missing` 每次都重跑同一节——用户看到
    「继续写」却永远写不完。呈现与否由 `block_is_renderable` 在渲染/覆盖判定处决定。
    """

    blocks = chapter_blocks(report, chapter_key)
    ready = 0
    omitted = 0
    for block in blocks:
        if block.state == "omitted":
            omitted += 1
        elif block.state == "ready":
            ready += 1
    return ChapterDraftFacts(
        key=chapter_key,
        generatable_blocks=len(blocks),
        ready_blocks=ready,
        omitted_blocks=omitted,
    )


def pending_section_keys(report: Report, chapter_key: str) -> tuple[str, ...]:
    """章内仍有未落定可生成块的节；批次 ``mode=missing`` 只跑这些节。

    节的判定复用 ``draft_facts`` 的同一套块事实，但按节聚合：一节内只要还有 pending 块，
    该节就要重跑（整节原子提交的语义决定了不能只补块）。
    """

    pending: list[str] = []
    for section in chapter_sections(report, chapter_key):
        blocks = _section_generatable_blocks(section, report)
        if not blocks:
            continue
        settled = all(
            block.state == "omitted" or block.state == "ready" for block in blocks
        )
        if not settled:
            pending.append(section.key)
    return tuple(pending)


def chapter_fingerprint(
    chapter_key: str,
    section_fingerprints: Mapping[str, str],
) -> str:
    """把章内各节的输入指纹聚合成章级指纹（稳定排序，与调用顺序无关）。

    章级新鲜度=「最近一次成功批次冻结的章指纹」与当前章指纹比较。各节的指纹仍由
    ``section_generation_input_fingerprint`` 单独计算——章不重新定义「输入」，只汇总。
    """

    encoded = json.dumps(
        {
            "chapter": chapter_key,
            "sections": sorted(section_fingerprints.items()),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()

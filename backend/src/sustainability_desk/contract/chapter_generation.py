# ABOUTME: 章节批次生成、章级台账与章工作稿回读的严格公共合同；前端类型由此派生，不手写平行形状。
# ABOUTME: 章节层只做编排与聚合：写回仍是整节原子提交，正文真相仍只有 Report。
# ABOUTME(en): Strict public contracts for chapter-batch generation, the chapter ledger and work-copy round-trip.
# ABOUTME(en): The chapter layer orchestrates and aggregates only; whole-section atomic write-back and Report SSOT stay.
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ChapterRunStatus = Literal[
    "queued", "running", "succeeded", "partial", "failed", "cancelled"
]
ChapterStepStatus = Literal[
    "pending", "running", "ready", "failed", "skipped", "cancelled"
]
ChapterLifecycleValue = Literal["draft", "reviewing", "locked"]
ChapterFreshnessValue = Literal["not_generated", "fresh", "stale"]
ChapterDraftStatusValue = Literal["fixed", "empty", "partial", "drafted"]
ChapterNextActionValue = Literal["generate", "continue", "refresh", "review"]
ChapterGenerationMode = Literal["missing", "all"]


class ChapterContractModel(BaseModel):
    """章级响应及嵌套 DTO 的严格解析基类。"""

    model_config = ConfigDict(extra="forbid")


class ChapterStepProjection(ChapterContractModel):
    """章批次中的一个节；status 与 reason_code 都是稳定值，不含异常消息。"""

    section_key: str
    title: str
    status: ChapterStepStatus
    block_count: int = Field(ge=0)
    state_seq: int | None = Field(default=None, ge=1)
    reason_code: str | None = None


class ChapterRunProgress(ChapterContractModel):
    """一次章批次的进度投影；轮询端点与章台账共用。"""

    run_id: UUID
    chapter_key: str
    mode: ChapterGenerationMode
    status: ChapterRunStatus
    steps: list[ChapterStepProjection]
    current_section_key: str | None
    state_seq: int | None = Field(default=None, ge=1)
    cancel_requested: bool
    failure_code: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def ready_sections(self) -> int:
        return sum(1 for step in self.steps if step.status == "ready")


class ChapterProjection(ChapterContractModel):
    """章台账的一行：完成度 + 新鲜度 + 生命周期 + 下一步动作。"""

    key: str
    ordinal: int = Field(ge=1)
    title: str
    module_id: str | None
    is_appendix: bool
    section_count: int = Field(ge=0)
    generatable_block_count: int = Field(ge=0)
    ready_block_count: int = Field(ge=0)
    omitted_block_count: int = Field(ge=0)
    pending_block_count: int = Field(ge=0)
    status: ChapterDraftStatusValue
    freshness: ChapterFreshnessValue
    lifecycle: ChapterLifecycleValue
    next_action: ChapterNextActionValue
    last_generated_at: str | None


class ChapterBoardTotals(ChapterContractModel):
    """整本报告的章级汇总；界面的「还剩几章」不自行累加。"""

    chapters: int = Field(ge=0)
    drafted: int = Field(ge=0)
    partial: int = Field(ge=0)
    empty: int = Field(ge=0)
    fixed: int = Field(ge=0)
    stale: int = Field(ge=0)
    pending_blocks: int = Field(ge=0)


class ChapterBoardResponse(ChapterContractModel):
    """章节台账：一章一行 + 汇总 + 当前在跑的批次（如有）。"""

    chapters: list[ChapterProjection]
    totals: ChapterBoardTotals
    active_run: ChapterRunProgress | None


class ChapterGenerationRequest(ChapterContractModel):
    """章批次入队请求；节的清单、块清单与额度全部由服务端派生。"""

    mode: ChapterGenerationMode = "missing"
    section_keys: list[str] | None = None
    base_state_seq: int = Field(ge=1)
    idempotency_key: UUID


class ChapterLifecycleRequest(ChapterContractModel):
    """人工推进章生命周期；锁定后拒绝新批次，已生成的正文不动。"""

    lifecycle: ChapterLifecycleValue


class ChapterWorkcopyBlock(ChapterContractModel):
    """工作稿中一个块的锚点：块 id + 导出时正文指纹，回读时据此对齐。"""

    block_id: str
    section_key: str
    kind: Literal["paragraph", "table"]
    text_fingerprint: str = Field(min_length=64, max_length=64)


class ChapterWorkcopyManifest(ChapterContractModel):
    """章工作稿清单（提交给编辑器/协作方的随附事实，不含内部字段与路径）。"""

    schema_version: Literal["sustainability_desk.chapter_workcopy.v1"]
    report_id: UUID
    chapter_key: str
    contract_version: str
    state_seq: int = Field(ge=1)
    exported_at: str
    blocks: list[ChapterWorkcopyBlock]


class ChapterRevisionProposal(ChapterContractModel):
    """回读工作稿后的一条块级变更提议；采纳与否由人决定，系统不自动写回。"""

    block_id: str
    section_key: str
    kind: Literal["modified", "added", "removed"]
    current_text: str
    proposed_text: str
    # added 时指向「插在哪一块之后」；None 表示章首。removed 时为空。
    anchor_block_id: str | None = None


class ChapterRevisionReport(ChapterContractModel):
    """一次回读的结果：提议清单 + 未能对齐的段落数（不静默丢弃）。"""

    chapter_key: str
    base_state_seq: int = Field(ge=1)
    proposals: list[ChapterRevisionProposal]
    unmatched_paragraphs: int = Field(ge=0)

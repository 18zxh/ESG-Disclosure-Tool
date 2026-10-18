# ABOUTME: 章批次编排——按章顺序逐节生成，每节仍走「整节原子提交」，单节失败不吞掉整章成果。
# ABOUTME: 章节层只做顺序、进度、取消与额度；正文与标题的唯一写回口仍是 section_generations。
# ABOUTME(en): Chapter orchestration — sections run in document order, each committing whole-section atomically;
# ABOUTME(en): a failed section no longer discards the chapter. Ordering/progress/cancel only; write-back stays in one place.
from __future__ import annotations

import logging
from uuid import UUID, uuid5

import asyncpg

from sustainability_desk.accounts.service import (
    AccountContext,
    get_account_context_for_account,
)
from sustainability_desk.contract.chapter_plan import (
    ChapterRef,
    chapter_fingerprint,
    derive_chapters,
    pending_section_keys,
)
from sustainability_desk.contract.knowledge_packages import KnowledgePackage
from sustainability_desk.contract.models import Report
from sustainability_desk.contract.report_revision import build_report_revision
from sustainability_desk.lightweight_report_generation import (
    current_generation_scope,
    resolve_section_mapped_evidence,
)
from sustainability_desk.llm.ai_observability import (
    ObservationRun,
    create_observation_run,
    observe_generation,
)
from sustainability_desk.llm.derive import (
    COMPANY_BUSINESS_SUMMARY_KEY,
    company_profile_text,
    derive_company_business_summary,
    needs_company_business_summary,
)
from sustainability_desk.llm.generate_all import blocks_for_section, generate_all
from sustainability_desk.observability.registry import Stage, register_stage
from sustainability_desk.observability.stages import open_stage
from sustainability_desk.persistence import chapter_generations as chapter_dal
from sustainability_desk.persistence import material_agent_pipeline as pipeline_dal
from sustainability_desk.persistence import reports as reports_dal
from sustainability_desk.persistence.section_generations import (
    GenerationAlreadyRunningError,
    GenerationBatch,
    GenerationBatchStatusConflictError,
    GenerationQuotaExceededError,
    GenerationStateConflictError,
    complete_batch,
    fail_batch,
    material_gated_block_ids,
    reserve_batch,
    section_generation_input_fingerprint,
    unsuccessful_generation_results,
)

logger = logging.getLogger(__name__)

# 批次级工作单元根：一次章批次一条 trace，章内每节一个子阶段。
CHAPTER_ORCHESTRATION_STAGE = register_stage(
    Stage(id="generation.chapter", kind="orchestration", unit_root=True)
)
CHAPTER_SECTION_STAGE = register_stage(
    Stage(id="generation.chapter.section", kind="orchestration")
)


class ChapterGenerationError(Exception):
    """章批次入队/编排的业务错误基类。"""


class ChapterEmptyError(ChapterGenerationError):
    """该章（或指定节）没有可生成内容，例如附录全为固定正文。"""


class ChapterUpToDateError(ChapterGenerationError):
    """续写模式下该章已无待写小节：这是正常的「写完了」，不是内容缺失。"""


class ChapterUnknownSectionError(ChapterGenerationError):
    """请求的节不属于该章。"""


def _section_titles(report: Report, section_keys: tuple[str, ...]) -> list[tuple[str, str]]:
    titles: dict[str, str] = {}

    def walk(sections) -> None:
        for section in sections:
            titles[section.key] = section.title
            walk(section.children or [])

    walk(report.sections)
    return [(key, titles.get(key, key)) for key in section_keys]


def resolve_chapter(report: Report, chapter_key: str) -> ChapterRef:
    for chapter in derive_chapters(report):
        if chapter.key == chapter_key:
            return chapter
    raise ChapterUnknownSectionError(chapter_key)


def select_section_keys(
    report: Report,
    chapter: ChapterRef,
    *,
    mode: str,
    requested: list[str] | None,
) -> tuple[str, ...]:
    """把「这次跑哪几节」解析成唯一清单；三个入口共用本判定。

    - ``mode=missing``：只跑仍有未落定块的节（按章续写）。
    - ``mode=all``：整章重写全部有生成块的节。
    - ``requested``：显式清单，必须是本章的节且必须含可生成块。
    """

    if requested:
        unknown = [key for key in requested if key not in chapter.all_section_keys]
        if unknown:
            raise ChapterUnknownSectionError(",".join(sorted(unknown)))
        generatable = set(chapter.section_keys)
        blocked = [key for key in requested if key not in generatable]
        if blocked:
            raise ChapterEmptyError(",".join(sorted(blocked)))
        return tuple(dict.fromkeys(requested))
    if mode == "all":
        return chapter.section_keys
    return pending_section_keys(report, chapter.key)


async def enqueue_chapter_generation(
    pool: asyncpg.Pool,
    *,
    account_id: UUID,
    report_id: UUID,
    chapter_key: str,
    mode: str,
    section_keys: list[str] | None,
    base_state_seq: int,
    idempotency_key: UUID,
    model_id: str,
) -> tuple[chapter_dal.ChapterRun, bool]:
    """按**服务端状态**派生章节与节清单后入队，不信任客户端送来的 Report。

    与整节重写端点的差别：整节重写是「用户刚在页面上改了什么，就地重写这一页」，
    客户端手里的 Report 就是当次输入；章批次是「按当前报告状态推进一章」，
    必须以库内 state 为准，否则一份过期页面会把整章写成旧值。
    """

    scope = await current_generation_scope(
        pool, account_id=account_id, report_id=report_id
    )
    snapshot = await reports_dal.get_lightweight_v4_snapshot(pool, account_id, report_id)
    if snapshot.state_seq != base_state_seq:
        raise GenerationStateConflictError(chapter_key)
    report = scope.project_report(
        build_report_revision(snapshot.state, package=scope.knowledge_package)
    )
    chapter = resolve_chapter(report, chapter_key)
    keys = select_section_keys(report, chapter, mode=mode, requested=section_keys)
    if not keys:
        # 区分「写完了」与「这一章本来就没有可写内容」：前者是正常终态，
        # 用错误码 chapter_empty 提示会让用户以为系统找不到章节。
        if mode != "all" and not section_keys and chapter.section_keys:
            raise ChapterUpToDateError(chapter_key)
        raise ChapterEmptyError(chapter_key)
    await chapter_dal.ensure_chapter_ledger(
        pool,
        account_id=account_id,
        report_id=report_id,
        chapters=[
            (item.key, item.ordinal, item.title, item.module_id)
            for item in derive_chapters(report)
        ],
    )
    return await chapter_dal.enqueue_run(
        pool,
        account_id=account_id,
        report_id=report_id,
        chapter_key=chapter_key,
        mode="all" if mode == "all" else "missing",
        expected_section_keys=keys,
        steps=chapter_dal.initial_steps(_section_titles(report, keys)),
        base_state_seq=base_state_seq,
        idempotency_key=idempotency_key,
        model_id=model_id,
    )


async def _account_context(
    pool: asyncpg.Pool, run: chapter_dal.ChapterRun
) -> AccountContext:
    return await get_account_context_for_account(pool, run.account_id)


def _failure_code(error: Exception) -> str:
    """把异常归到稳定 code；异常消息只进日志，不进进度响应。"""

    if isinstance(error, GenerationQuotaExceededError):
        return "quota_exceeded"
    if isinstance(error, GenerationAlreadyRunningError):
        return "section_busy"
    if isinstance(
        error, (GenerationStateConflictError, GenerationBatchStatusConflictError)
    ):
        return "state_conflict"
    if isinstance(error, ValueError):
        return "incomplete"
    return "generation_error"


async def _current_report(
    pool: asyncpg.Pool, *, run: chapter_dal.ChapterRun
) -> tuple[Report, int]:
    """按库内 state 重建 Report，并**同一快照**返回它的 state_seq。

    两者必须同源：若先用旧快照建 Report、再另行读取「当前」state_seq 去预留，
    生成结果会以新序号写回、内容却基于旧正文——一次静默的丢失更新。
    """

    scope = await current_generation_scope(
        pool, account_id=run.account_id, report_id=run.report_id
    )
    snapshot = await reports_dal.get_lightweight_v4_snapshot(
        pool, run.account_id, run.report_id
    )
    report = scope.project_report(
        build_report_revision(snapshot.state, package=scope.knowledge_package)
    )
    return report, snapshot.state_seq


async def _generate_section(
    pool: asyncpg.Pool,
    *,
    run: chapter_dal.ChapterRun,
    report: Report,
    section_key: str,
    package: KnowledgePackage,
    quota: int,
    model_id: str,
    base_state_seq: int,
    material_set_fingerprint: str | None,
    company_business_summary: str | None,
    observation: ObservationRun,
    trace_id: str,
) -> tuple[str, int | None, str | None, int]:
    """生成并提交一节；返回 (status, state_seq, reason_code, block_count)。

    所有失败在此收敛成稳定 code：章批次不得因一节失败而中断整条编排，
    也不得把失败伪装成成功——status 与 reason_code 一并进进度账本。
    """

    blocks = blocks_for_section(report, section_key)
    expected_block_ids = tuple(block.id for block in blocks)
    if not expected_block_ids:
        return "skipped", None, "no_generatable_block", 0

    gated = frozenset(expected_block_ids) & material_gated_block_ids(package)
    mapped_evidence = await resolve_section_mapped_evidence(
        pool,
        account_id=run.account_id,
        report_id=run.report_id,
        expected_block_ids=expected_block_ids,
    )
    if gated and mapped_evidence is None:
        # 缺冻结映射时证据门控会关闭、块被判受控省略，而受控省略在账本里算成功——
        # 既有正文会被静默抹平。故整节拒绝，而不是带着空决定生成。
        return "failed", None, "material_not_ready", len(expected_block_ids)

    fingerprint = section_generation_input_fingerprint(
        report,
        section_key,
        material_set_fingerprint=material_set_fingerprint,
    )
    try:
        batch: GenerationBatch = await reserve_batch(
            pool,
            account_id=run.account_id,
            report_id=run.report_id,
            section_key=section_key,
            expected_block_ids=expected_block_ids,
            # 每节一个确定性幂等键：同一批次内重试命中同一行，跨节互不冲突。
            idempotency_key=uuid5(run.id, section_key),
            base_state_seq=base_state_seq,
            regeneration_quota=quota,
            input_fingerprint=fingerprint,
        )
    except Exception as error:  # noqa: BLE001 — 单节失败隔离，收敛为稳定 code
        reason = _failure_code(error)
        logger.warning("章批次 %s 的节 %s 未能预留：%s", run.id, section_key, reason)
        return "failed", None, reason, len(expected_block_ids)

    if batch.replayed:
        return "ready", batch.base_state_seq, None, len(expected_block_ids)

    try:
        with open_stage(
            CHAPTER_SECTION_STAGE,
            trace_id=trace_id,
            report_id=str(run.report_id),
            **{
                "sustainability_desk.chapter_key": run.chapter_key,
                "sustainability_desk.section_key": section_key,
            },
        ):
            with observe_generation(observation):
                results = await generate_all(
                    report,
                    section_key=section_key,
                    n=1,
                    model_id=model_id,
                    observation=observation,
                    empty_table_policy="omit",
                    mapped_evidence_by_block=mapped_evidence,
                )
            # 先看块级结果再提交：让失败原因落到「契约阻断 / 生成失败」这类稳定 code 上，
            # 而不是一律由 apply 的 ValueError 归成含义模糊的 incomplete。
            unsuccessful = unsuccessful_generation_results(
                results, allow_contract_omissions=False, package=package
            )
            if unsuccessful:
                await fail_batch(pool, batch.id, "incomplete_section")
                # 「blocked」是中性表述：底层可能因守卫、可见性或模型不可用被拦，
                # 这里不替它下结论（准确归因在块级 trace 与 reason 里）。
                return (
                    "failed",
                    None,
                    "blocked" if all(
                        item.get("status") == "blocked" for item in unsuccessful
                    ) else "generation_failed",
                    len(expected_block_ids),
                )
            new_seq = await complete_batch(
                pool,
                batch=batch,
                results=results,
                package=package,
                company_business_summary=company_business_summary,
                input_fingerprint=fingerprint,
            )
    except Exception as error:  # noqa: BLE001 — 单节失败隔离
        reason = _failure_code(error)
        await fail_batch(pool, batch.id, reason)
        logger.warning(
            "章批次 %s 的节 %s 提交失败：%s（%s）",
            run.id,
            section_key,
            reason,
            type(error).__name__,
        )
        return "failed", None, reason, len(expected_block_ids)

    return "ready", new_seq, None, len(expected_block_ids)


def _with_company_summary(report: Report, summary: str | None) -> Report:
    """把本次派生的公司业务摘要并入在途 Report（不改持久化状态）。

    摘要只在第一次成功提交时才落库；在那之前每一节都得自己带上，
    否则首节之后的正文会退回「未提供公司概况」的通用表述。
    """

    if not summary:
        return report
    field = report.fields.get(COMPANY_BUSINESS_SUMMARY_KEY)
    if field is None or field.value:
        return report
    return report.model_copy(
        update={
            "fields": {
                **report.fields,
                COMPANY_BUSINESS_SUMMARY_KEY: field.model_copy(
                    update={"value": summary}
                ),
            }
        }
    )


async def _ensure_company_summary(
    report: Report,
    *,
    package: KnowledgePackage,
    model_id: str,
    observation: ObservationRun,
) -> str | None:
    """批次开始前派生一次公司业务摘要；与整节/报告级生成同源判定。

    摘要出现在每个块的 <report_subject> 里，若不派生，整章正文会退化成
    「未提供公司概况」的通用表述——这是写作质量差异，不是安全边界差异。
    """

    field = report.fields.get(COMPANY_BUSINESS_SUMMARY_KEY)
    if field is None or field.value:
        return None
    if not needs_company_business_summary(company_profile_text(report), package=package):
        return None
    return await derive_company_business_summary(
        report, model_id=model_id, observation=observation
    )


async def run_chapter_generation(
    pool: asyncpg.Pool,
    *,
    run: chapter_dal.ChapterRun,
    worker_id: str,
    model_id: str,
) -> None:
    """执行一整章：逐节生成、逐节提交、逐节记账；结束给出章级裁决。"""

    scope = await current_generation_scope(
        pool, account_id=run.account_id, report_id=run.report_id
    )
    package = scope.knowledge_package
    context = await _account_context(pool, run)
    quota = context.profile.section_regeneration_limit
    # 观测运行要求报告实际钉住的契约版本；这里读库而不是用包的当前版本，
    # 否则契约升级后旧报告的轨迹会标成新版本，归因反过来被污染。
    summary = await reports_dal.get_report_summary(
        pool, run.account_id, run.report_id
    )
    snapshot = await pipeline_dal.latest_material_set_snapshot(
        pool, account_id=run.account_id, report_id=run.report_id
    )
    material_set_fingerprint = (
        snapshot.snapshot.input_fingerprint if snapshot is not None else None
    )
    observation = create_observation_run(
        CHAPTER_ORCHESTRATION_STAGE,
        model_id=model_id,
        workload_kind="product_generation",
        report_id=str(run.report_id),
        block_id=f"chapter:{run.chapter_key}",
        run_id=str(run.id),
        user_id=str(run.account_id),
        contract_version=summary.contract_version,
    )

    ready: list[str] = []
    failed: list[str] = []
    cancelled = False

    with open_stage(
        CHAPTER_ORCHESTRATION_STAGE,
        trace_id=observation.runId,
        report_id=str(run.report_id),
        **{
            "sustainability_desk.chapter_key": run.chapter_key,
            "sustainability_desk.section_count": len(run.expected_section_keys),
        },
    ), observe_generation(observation):
        opening_report, _opening_state_seq = await _current_report(pool, run=run)
        try:
            company_summary = await _ensure_company_summary(
                opening_report,
                package=package,
                model_id=model_id,
                observation=observation,
            )
        except Exception:  # noqa: BLE001 — 摘要是增强而非前置条件
            # 派生失败（典型是模型密钥/端点不可用）不应把整批判成编排故障：
            # 逐节生成会各自失败并留下稳定 reason code，用户能看到「哪一节、为什么」。
            logger.warning(
                "章批次 %s 未能派生公司业务摘要，按无摘要继续", run.id
            )
            company_summary = None
        if company_summary:
            opening_report = _with_company_summary(opening_report, company_summary)

        for section_key in run.expected_section_keys:
            if await chapter_dal.is_cancel_requested(pool, run_id=run.id):
                await chapter_dal.record_step(
                    pool,
                    run_id=run.id,
                    section_key=section_key,
                    status="cancelled",
                    reason_code="cancel_requested",
                )
                cancelled = True
                # 其余未开始的节保持 pending：取消不是「全部失败」，重发时用户应看到真实进度。
                break
            if not await chapter_dal.renew_lease(pool, run_id=run.id, worker_id=worker_id):
                logger.warning("章批次 %s 的租约已被接管，停止本进程编排", run.id)
                return
            await chapter_dal.record_step(
                pool, run_id=run.id, section_key=section_key, status="running"
            )
            report, base_state_seq = await _current_report(pool, run=run)
            report = _with_company_summary(report, company_summary)
            status, state_seq, reason_code, block_count = await _generate_section(
                pool,
                run=run,
                report=report,
                section_key=section_key,
                package=package,
                quota=quota,
                model_id=model_id,
                base_state_seq=base_state_seq,
                material_set_fingerprint=material_set_fingerprint,
                company_business_summary=company_summary,
                observation=observation,
                trace_id=observation.runId,
            )
            if reason_code == "state_conflict":
                # 同一报告的另一章可能刚提交，把 state_seq 推高了。这不是内容错误，
                # 重建一次 Report 与基准序号再试一次；再冲突才算失败，避免并行批次互相踩踏。
                report, base_state_seq = await _current_report(pool, run=run)
                report = _with_company_summary(report, company_summary)
                status, state_seq, reason_code, block_count = await _generate_section(
                    pool,
                    run=run,
                    report=report,
                    section_key=section_key,
                    package=package,
                    quota=quota,
                    model_id=model_id,
                    base_state_seq=base_state_seq,
                    material_set_fingerprint=material_set_fingerprint,
                    company_business_summary=company_summary,
                    observation=observation,
                    trace_id=observation.runId,
                )
            await chapter_dal.record_step(
                pool,
                run_id=run.id,
                section_key=section_key,
                status=status,  # type: ignore[arg-type]
                block_count=block_count,
                state_seq=state_seq,
                reason_code=reason_code,
            )
            if status == "ready":
                ready.append(section_key)
            elif status == "failed":
                failed.append(section_key)

        if ready:
            # 章快照用**成功之后**重算的指纹：生成期间正文已变，指纹必须反映变化，
            # 否则章台账会把刚写完的章标成 fresh，而它实际已与输入不一致。
            # state_seq 取同一快照的序号，避免「指纹新、序号旧」的错配。
            settled, settled_seq = await _current_report(pool, run=run)
            await chapter_dal.record_chapter_snapshot(
                pool,
                report_id=run.report_id,
                chapter_key=run.chapter_key,
                state_seq=settled_seq,
                input_fingerprint=chapter_fingerprint(
                    run.chapter_key,
                    {
                        key: section_generation_input_fingerprint(
                            settled,
                            key,
                            material_set_fingerprint=material_set_fingerprint,
                        )
                        for key in run.expected_section_keys
                    },
                ),
            )

    if cancelled:
        final, failure_code = "cancelled", "cancel_requested"
    elif failed and ready:
        final, failure_code = "partial", None
    elif failed and not ready:
        final, failure_code = "failed", "no_section_succeeded"
    else:
        final, failure_code = "succeeded", (
            "no_generatable_section" if not ready else None
        )

    await chapter_dal.finish_run(
        pool, run_id=run.id, status=final, failure_code=failure_code
    )
    await pool.execute(
        """
        insert into report_events (report_id, actor, event_type, payload)
        values ($1, 'llm', 'chapter_generation_finished', $2)
        """,
        run.report_id,
        {
            "runId": str(run.id),
            "chapterKey": run.chapter_key,
            "status": final,
            "readySections": ready,
            "failedSections": failed,
        },
    )


async def process_one_chapter_generation(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    model_id: str | None = None,
    report_id: UUID | None = None,
) -> bool:
    """worker 节点入口：领取一个章批次并执行；无活可干返回 False。

    模型以**入队时冻结**的 `run.model_id` 为准：用户选定模型后，排队期间改默认值
    不该让这一章换模型；worker 的 `model_id` 参数只在批次未记录时兜底。
    """

    run = await chapter_dal.claim_run(pool, worker_id=worker_id, report_id=report_id)
    if run is None:
        return False
    try:
        await run_chapter_generation(
            pool,
            run=run,
            worker_id=worker_id,
            model_id=model_id or run.model_id,
        )
    except Exception:  # noqa: BLE001 — 单批次失败不得终结常驻 worker
        logger.exception("章批次 %s 执行失败", run.id)
        await chapter_dal.finish_run(
            pool, run_id=run.id, status="failed", failure_code="orchestration_error"
        )
    return True

# ABOUTME: 章节台账与章批次账本——章状态、单活动批次租约、逐节进度与取消；不保存任何正文。
# ABOUTME: Report（report_states）仍是正文 SSOT；本模块只记录「哪一章、哪一节、什么时候、成了没有」。
# ABOUTME(en): Chapter ledger and batch ledger — lifecycle, one active batch per chapter, per-section progress, cancel.
# ABOUTME(en): No prose here: report_states stays the SSOT; this module only records which chapter/section/when/outcome.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal
from uuid import UUID

import asyncpg

ChapterRunStatus = Literal[
    "queued", "running", "succeeded", "partial", "failed", "cancelled"
]
ChapterRunMode = Literal["missing", "all"]

# 章批次租约比整节批次长：一章可能含十余节，每节都是数次模型调用。
# 过期不是错误语义而是「另一 worker 可重新领取」；租约在每节开始时续一次。
CHAPTER_LEASE = timedelta(minutes=30)


class ChapterLedgerError(Exception):
    """章台账与批次账本的业务错误基类。"""


class ChapterRunNotFoundError(ChapterLedgerError):
    """批次不存在或不属于该报告。"""


class ChapterRunConflictError(ChapterLedgerError):
    """同一章已有活动批次，或幂等键已用于其他请求。"""


class ChapterLockedError(ChapterLedgerError):
    """章已锁定，拒绝新的生成批次。"""


class ChapterStateConflictError(ChapterLedgerError):
    """客户端持有的 state_seq 已过期。"""


@dataclass(frozen=True)
class ChapterLedgerRow:
    """章台账一行；last_generated_* 由成功批次回写，不含正文。"""

    chapter_key: str
    ordinal: int
    title: str
    module_id: str | None
    lifecycle: Literal["draft", "reviewing", "locked"]
    last_generated_at: datetime | None
    last_generated_state_seq: int | None
    input_fingerprint: str | None


@dataclass(frozen=True)
class ChapterRun:
    """一次章批次的领取视图。"""

    id: UUID
    account_id: UUID
    report_id: UUID
    chapter_key: str
    mode: ChapterRunMode
    status: ChapterRunStatus
    expected_section_keys: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]
    current_section_key: str | None
    base_state_seq: int
    idempotency_key: UUID
    cancel_requested: bool
    failure_code: str | None
    model_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _run(row: asyncpg.Record) -> ChapterRun:
    return ChapterRun(
        id=row["id"],
        account_id=row["account_id"],
        report_id=row["report_id"],
        chapter_key=row["chapter_key"],
        mode=row["mode"],
        status=row["status"],
        expected_section_keys=tuple(row["expected_section_keys"]),
        steps=tuple(row["steps"] or ()),
        current_section_key=row["current_section_key"],
        base_state_seq=row["base_state_seq"],
        idempotency_key=row["idempotency_key"],
        cancel_requested=row["cancel_requested"],
        failure_code=row["failure_code"],
        model_id=row["model_id"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def initial_steps(
    sections: Iterable[tuple[str, str]],
) -> list[dict[str, Any]]:
    """章批次的初始进度：每节一条 pending，节标题在建单时冻结。"""

    return [
        {
            "section_key": section_key,
            "title": title,
            "status": "pending",
            "block_count": 0,
            "state_seq": None,
            "reason_code": None,
        }
        for section_key, title in sections
    ]


async def ensure_chapter_ledger(
    pool: asyncpg.Pool,
    *,
    account_id: UUID,
    report_id: UUID,
    chapters: Iterable[tuple[str, int, str, str | None]],
) -> None:
    """把当前骨架同步进台账：新增章插入，标题与序位随骨架更新。

    lifecycle 与生成快照是**人工/批次拥有**的字段，骨架同步不得覆盖它们——
    否则改一次章节标题就把「已锁定」重置成草稿，用户为自己的成果买单。
    """

    rows = [
        (account_id, report_id, chapter_key, ordinal, title, module_id)
        for chapter_key, ordinal, title, module_id in chapters
    ]
    if not rows:
        return
    await pool.executemany(
        """
        insert into report_chapters
          (account_id, report_id, chapter_key, ordinal, title, module_id)
        values ($1, $2, $3, $4, $5, $6)
        on conflict (report_id, chapter_key) do update
        set title = excluded.title,
            ordinal = excluded.ordinal,
            module_id = excluded.module_id,
            updated_at = now()
        """,
        rows,
    )


async def chapter_ledger(
    pool: asyncpg.Pool,
    *,
    report_id: UUID,
) -> dict[str, ChapterLedgerRow]:
    """读取该报告全部章台账行；无行的章由调用方按骨架默认值投影。"""

    rows = await pool.fetch(
        """
        select chapter_key, ordinal, title, module_id, lifecycle,
               last_generated_at, last_generated_state_seq, input_fingerprint
        from report_chapters
        where report_id = $1
        """,
        report_id,
    )
    return {
        row["chapter_key"]: ChapterLedgerRow(
            chapter_key=row["chapter_key"],
            ordinal=row["ordinal"],
            title=row["title"],
            module_id=row["module_id"],
            lifecycle=row["lifecycle"],
            last_generated_at=row["last_generated_at"],
            last_generated_state_seq=row["last_generated_state_seq"],
            input_fingerprint=row["input_fingerprint"],
        )
        for row in rows
    }


async def set_lifecycle(
    pool: asyncpg.Pool,
    *,
    account_id: UUID,
    report_id: UUID,
    chapter_key: str,
    lifecycle: Literal["draft", "reviewing", "locked"],
) -> None:
    """人工推进/回退章生命周期；目标章不存在时视为冲突（由 API 转 404）。"""

    changed = await pool.fetchval(
        """
        update report_chapters
        set lifecycle = $4, updated_at = now()
        where account_id = $1 and report_id = $2 and chapter_key = $3
        returning chapter_key
        """,
        account_id,
        report_id,
        chapter_key,
        lifecycle,
    )
    if changed is None:
        raise ChapterRunNotFoundError(chapter_key)


async def enqueue_run(
    pool: asyncpg.Pool,
    *,
    account_id: UUID,
    report_id: UUID,
    chapter_key: str,
    mode: ChapterRunMode,
    expected_section_keys: tuple[str, ...],
    steps: list[dict[str, Any]],
    base_state_seq: int,
    idempotency_key: UUID,
    model_id: str,
) -> tuple[ChapterRun, bool]:
    """建/(幂等重放)一次章批次；同一章同时只允许一个活动批次。

    返回 ``(run, replayed)``。重放以 ``(account_id, idempotency_key)`` 为准：
    同键同范围返回原批次，同键异范围抛冲突——与整节批次同一语义。
    """

    async with pool.acquire() as conn, conn.transaction():
        locked = await conn.fetchval(
            """
            select lifecycle from report_chapters
            where account_id = $1 and report_id = $2 and chapter_key = $3
            for update
            """,
            account_id,
            report_id,
            chapter_key,
        )
        if locked == "locked":
            raise ChapterLockedError(chapter_key)

        known = await conn.fetchrow(
            """
            select * from chapter_generation_runs
            where account_id = $1 and idempotency_key = $2
            """,
            account_id,
            idempotency_key,
        )
        if known is not None:
            same_scope = (
                known["report_id"] == report_id
                and known["chapter_key"] == chapter_key
                and tuple(known["expected_section_keys"]) == expected_section_keys
            )
            if not same_scope:
                raise ChapterRunConflictError("idempotency_key")
            if known["status"] in {"queued", "running", "succeeded", "partial"}:
                return _run(known), True
            raise ChapterRunConflictError("idempotency_key")

        current_seq = await conn.fetchval(
            "select state_seq from report_states where report_id = $1", report_id
        )
        if current_seq != base_state_seq:
            raise ChapterStateConflictError(chapter_key)

        active = await conn.fetchval(
            """
            select 1 from chapter_generation_runs
            where report_id = $1 and chapter_key = $2 and status in ('queued', 'running')
            """,
            report_id,
            chapter_key,
        )
        if active is not None:
            raise ChapterRunConflictError(chapter_key)

        row = await conn.fetchrow(
            """
            insert into chapter_generation_runs
              (account_id, report_id, chapter_key, mode, status, expected_section_keys,
               steps, base_state_seq, idempotency_key, model_id)
            values ($1, $2, $3, $4, 'queued', $5, $6, $7, $8, $9)
            returning *
            """,
            account_id,
            report_id,
            chapter_key,
            mode,
            list(expected_section_keys),
            steps,
            base_state_seq,
            idempotency_key,
            model_id,
        )
        await conn.execute(
            """
            insert into report_events (report_id, actor, event_type, payload)
            values ($1, 'user', 'chapter_generation_queued', $2)
            """,
            report_id,
            {
                "chapterKey": chapter_key,
                "mode": mode,
                "sectionCount": len(expected_section_keys),
            },
        )
    return _run(row), False


async def claim_run(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    lease: timedelta = CHAPTER_LEASE,
    report_id: UUID | None = None,
) -> ChapterRun | None:
    """领取一个排队中的章批次（for update skip locked），并续上租约。"""

    row = await pool.fetchrow(
        """
        with candidate as (
          select id from chapter_generation_runs
          where status = 'queued' and ($3::uuid is null or report_id = $3::uuid)
          order by created_at
          limit 1
          for update skip locked
        )
        update chapter_generation_runs run
        set status = 'running',
            attempt = run.attempt + 1,
            leased_by = $1,
            lease_token = gen_random_uuid(),
            lease_expires_at = now() + $2::interval,
            started_at = coalesce(run.started_at, now()),
            current_section_key = coalesce(
              run.current_section_key,
              (select step->>'section_key' from jsonb_array_elements(run.steps) step limit 1)
            ),
            updated_at = now()
        from candidate
        where run.id = candidate.id
        returning run.*
        """,
        worker_id,
        lease,
        report_id,
    )
    return _run(row) if row is not None else None


async def renew_lease(
    pool: asyncpg.Pool,
    *,
    run_id: UUID,
    worker_id: str,
    lease: timedelta = CHAPTER_LEASE,
) -> bool:
    """每节开始前续租；租约已被他人接管时返回 False，调用方必须停止。"""

    renewed = await pool.fetchval(
        """
        update chapter_generation_runs
        set lease_expires_at = now() + $3::interval, updated_at = now()
        where id = $1 and leased_by = $2 and status = 'running'
        returning id
        """,
        run_id,
        worker_id,
        lease,
    )
    return renewed is not None


async def record_step(
    pool: asyncpg.Pool,
    *,
    run_id: UUID,
    section_key: str,
    status: Literal["running", "ready", "failed", "skipped", "cancelled"],
    block_count: int = 0,
    state_seq: int | None = None,
    reason_code: str | None = None,
) -> None:
    """更新一节的状态；整段 steps 在同一事务内读改写，避免并发半更新。

    单活动批次保证同一 run 只有本 worker 写 steps，故此处以行锁为前提而非局部 jsonb_set：
    进度是「一组节的有序事实」，逐字段拼接会让状态出现不可能的组合。
    """

    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "select steps from chapter_generation_runs where id = $1 for update", run_id
        )
        if row is None:
            raise ChapterRunNotFoundError(str(run_id))
        steps = list(row["steps"] or [])
        found = False
        for step in steps:
            if step.get("section_key") != section_key:
                continue
            step["status"] = status
            step["block_count"] = block_count
            step["state_seq"] = state_seq
            step["reason_code"] = reason_code
            found = True
            break
        if not found:
            raise ChapterRunNotFoundError(f"{run_id}:{section_key}")
        await conn.execute(
            """
            update chapter_generation_runs
            set steps = $2, current_section_key = $3, updated_at = now()
            where id = $1
            """,
            run_id,
            steps,
            section_key,
        )


async def request_cancel(
    pool: asyncpg.Pool,
    *,
    account_id: UUID,
    report_id: UUID,
    run_id: UUID,
) -> ChapterRun:
    """置取消位；worker 在**下一节开始前**收手，正在生成的那一节照常落库。

    不做硬中断：一次整节生成中途放弃会在 provider 侧留下半程调用，且已花掉的额度
    无法回收；「生成完当前节再停」是用户可理解、账本也可核对的语义。
    """

    row = await pool.fetchrow(
        """
        update chapter_generation_runs
        set cancel_requested = true,
            -- 尚未被领取的批次直接终结：留在 queued 会永久占住「单活动批次」的唯一索引，
            -- 之后任何重发都被判为 chapter_busy，用户无自助恢复路径。
            status = case when status = 'queued' then 'cancelled' else status end,
            failure_code = case
                when status = 'queued' then 'cancel_requested' else failure_code end,
            finished_at = case when status = 'queued' then now() else finished_at end,
            updated_at = now()
        where id = $1 and account_id = $2 and report_id = $3
          and status in ('queued', 'running')
        returning *
        """,
        run_id,
        account_id,
        report_id,
    )
    if row is not None:
        return _run(row)
    existing = await pool.fetchrow(
        """
        select * from chapter_generation_runs
        where id = $1 and account_id = $2 and report_id = $3
        """,
        run_id,
        account_id,
        report_id,
    )
    if existing is None:
        raise ChapterRunNotFoundError(str(run_id))
    return _run(existing)


async def is_cancel_requested(pool: asyncpg.Pool, *, run_id: UUID) -> bool:
    value = await pool.fetchval(
        "select cancel_requested from chapter_generation_runs where id = $1", run_id
    )
    return bool(value)


async def finish_run(
    pool: asyncpg.Pool,
    *,
    run_id: UUID,
    status: Literal["succeeded", "partial", "failed", "cancelled"],
    failure_code: str | None = None,
) -> None:
    """终结批次并清空租约；已终结的批次不会被二次改写。"""

    await pool.execute(
        """
        update chapter_generation_runs
        set status = $2,
            failure_code = $3,
            leased_by = null,
            lease_token = null,
            lease_expires_at = null,
            finished_at = now(),
            updated_at = now()
        where id = $1 and status = 'running'
        """,
        run_id,
        status,
        failure_code,
    )


async def record_chapter_snapshot(
    pool: asyncpg.Pool,
    *,
    report_id: UUID,
    chapter_key: str,
    state_seq: int,
    input_fingerprint: str,
) -> None:
    """成功批次回写章级快照：最近一次生成的输入指纹与状态序号。"""

    await pool.execute(
        """
        update report_chapters
        set last_generated_at = now(),
            last_generated_state_seq = $3,
            input_fingerprint = $4,
            updated_at = now()
        where report_id = $1 and chapter_key = $2
        """,
        report_id,
        chapter_key,
        state_seq,
        input_fingerprint,
    )


async def get_run(
    pool: asyncpg.Pool,
    *,
    account_id: UUID,
    report_id: UUID,
    run_id: UUID,
) -> ChapterRun:
    row = await pool.fetchrow(
        """
        select * from chapter_generation_runs
        where id = $1 and account_id = $2 and report_id = $3
        """,
        run_id,
        account_id,
        report_id,
    )
    if row is None:
        raise ChapterRunNotFoundError(str(run_id))
    return _run(row)


async def latest_run(
    pool: asyncpg.Pool,
    *,
    report_id: UUID,
    chapter_key: str | None = None,
) -> ChapterRun | None:
    """章台账用：该章（或全报告）最近一次批次，含活动中的。"""

    row = await pool.fetchrow(
        """
        select * from chapter_generation_runs
        where report_id = $1 and ($2::text is null or chapter_key = $2::text)
        order by created_at desc
        limit 1
        """,
        report_id,
        chapter_key,
    )
    return _run(row) if row is not None else None


async def recover_expired_runs(pool: asyncpg.Pool) -> int:
    """把过期租约的批次判为失败，交还队列语义；不自动重排队。

    自动重排队会让一个总是超时的章无限烧额度；失败态可由用户显式重发，
    且幂等键仍在，重发不会产生重复账目。
    """

    result = await pool.execute(
        """
        update chapter_generation_runs
        set status = 'failed',
            failure_code = 'lease_expired',
            leased_by = null,
            lease_token = null,
            lease_expires_at = null,
            finished_at = now(),
            updated_at = now()
        where status = 'running' and lease_expires_at <= $1
        """,
        datetime.now(timezone.utc),
    )
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0

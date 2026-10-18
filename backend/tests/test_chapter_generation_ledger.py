# ABOUTME: 章台账与章批次账本的数据库集成测试——单活动批次、幂等重放、取消防卡死、租约恢复。
# ABOUTME: 直连本机 Supabase；栈未启动时整组跳过（与 section 批次测试同口径）。
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from sustainability_desk.contract.contract_version import contract_version
from sustainability_desk.persistence import chapter_generations as chapter_dal
from sustainability_desk.persistence import reports as reports_dal
from sustainability_desk.persistence.db import _init_connection
from knowledge_package_fixtures import SSE_PACKAGE

LOCAL_DB = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
STATE = {
    "version": 4,
    "fields": {},
    "intakeItems": {},
    "generatedBlocks": {},
    "tableBlocks": {},
}
SECTIONS = [("env.governance", "环境治理"), ("env.strategy", "环境战略")]


@pytest.fixture
async def pool():
    try:
        value = await asyncpg.create_pool(
            LOCAL_DB, min_size=1, max_size=3, init=_init_connection, timeout=3
        )
    except OSError:
        pytest.skip("本地 Supabase 栈未启动")
    yield value
    await value.close()


@pytest.fixture
async def account(pool):
    account_id = uuid4()
    await pool.execute(
        "insert into accounts (id, email) values ($1, $2)",
        account_id,
        f"{account_id}@example.edu.cn",
    )
    yield account_id
    # 先删批次与台账再删报告：这两张表没有级联，残留的 queued 行会被
    # claim_run 全局领走，让下一个用例的断言指向别人的批次。
    await pool.execute(
        "delete from chapter_generation_runs where account_id = $1", account_id
    )
    await pool.execute("delete from report_chapters where account_id = $1", account_id)
    await pool.execute(
        "delete from report_events where report_id in "
        "(select id from reports where account_id = $1)",
        account_id,
    )
    await pool.execute(
        "delete from report_states where report_id in "
        "(select id from reports where account_id = $1)",
        account_id,
    )
    await pool.execute("delete from reports where account_id = $1", account_id)
    await pool.execute("delete from accounts where id = $1", account_id)


async def _report(pool, account_id):
    return await reports_dal.create_report(
        pool,
        account_id=account_id,
        title="章批次测试",
        report_type="lightweight",
        report_profile_id="sse_zh_hans@1",
        data_classification="customer",
        contract_version=contract_version(SSE_PACKAGE),
        initial_state=STATE,
        active_scope_limit=5,
        profile_id="local_single_user@1",
    )


async def _ledger(pool, account_id, report_id, *, title="环境"):
    await chapter_dal.ensure_chapter_ledger(
        pool,
        account_id=account_id,
        report_id=report_id,
        chapters=[("env", 1, title, None)],
    )


async def _enqueue(pool, account_id, report_id, *, key=None, base_seq=1):
    return await chapter_dal.enqueue_run(
        pool,
        account_id=account_id,
        report_id=report_id,
        chapter_key="env",
        mode="missing",
        expected_section_keys=tuple(key for key, _ in SECTIONS),
        steps=chapter_dal.initial_steps(SECTIONS),
        base_state_seq=base_seq,
        idempotency_key=key or uuid4(),
        model_id="luna",
    )


async def test_ledger_sync_keeps_manual_lifecycle_and_snapshot(pool, account):
    report = await _report(pool, account)
    await _ledger(pool, account, report.id)
    await chapter_dal.set_lifecycle(
        pool,
        account_id=account,
        report_id=report.id,
        chapter_key="env",
        lifecycle="locked",
    )
    await chapter_dal.record_chapter_snapshot(
        pool,
        report_id=report.id,
        chapter_key="env",
        state_seq=4,
        input_fingerprint="b" * 64,
    )

    # 骨架同步只覆盖标题/序位，不得把「已锁定」和成功批次快照冲掉。
    await _ledger(pool, account, report.id, title="环境（改标题）")
    row = (await chapter_dal.chapter_ledger(pool, report_id=report.id))["env"]

    assert row.title == "环境（改标题）"
    assert row.lifecycle == "locked"
    assert row.last_generated_state_seq == 4
    assert row.input_fingerprint == "b" * 64


async def test_enqueue_is_single_active_and_idempotent(pool, account):
    report = await _report(pool, account)
    await _ledger(pool, account, report.id)
    key = uuid4()

    first, replayed = await _enqueue(pool, account, report.id, key=key)
    assert (first.status, replayed) == ("queued", False)

    again, replayed = await _enqueue(pool, account, report.id, key=key)
    assert replayed is True
    assert again.id == first.id

    with pytest.raises(chapter_dal.ChapterRunConflictError):
        await _enqueue(pool, account, report.id)


async def test_cancelling_queued_run_releases_the_active_slot(pool, account):
    """排队中被取消必须直接终结：留在 queued 会永久占住单活动批次索引。"""

    report = await _report(pool, account)
    await _ledger(pool, account, report.id)
    run, _ = await _enqueue(pool, account, report.id)

    cancelled = await chapter_dal.request_cancel(
        pool, account_id=account, report_id=report.id, run_id=run.id
    )
    assert cancelled.status == "cancelled"
    assert cancelled.finished_at is not None

    # 同一章可以立刻重发：这正是取消后用户的恢复路径。
    fresh, replayed = await _enqueue(pool, account, report.id)
    assert (fresh.status, replayed) == ("queued", False)


async def test_claim_progress_and_finish_round_trip(pool, account):
    report = await _report(pool, account)
    await _ledger(pool, account, report.id)
    run, _ = await _enqueue(pool, account, report.id)

    claimed = await chapter_dal.claim_run(pool, worker_id="w-1", report_id=report.id)
    assert claimed is not None
    assert claimed.id == run.id
    assert claimed.status == "running"
    assert claimed.current_section_key == SECTIONS[0][0]

    await chapter_dal.record_step(
        pool,
        run_id=run.id,
        section_key=SECTIONS[0][0],
        status="ready",
        block_count=3,
        state_seq=2,
    )
    await chapter_dal.finish_run(pool, run_id=run.id, status="partial")

    final = await chapter_dal.get_run(
        pool, account_id=account, report_id=report.id, run_id=run.id
    )
    assert final.status == "partial"
    assert final.finished_at is not None
    steps = {step["section_key"]: step for step in final.steps}
    assert steps[SECTIONS[0][0]]["status"] == "ready"
    assert steps[SECTIONS[0][0]]["state_seq"] == 2
    assert steps[SECTIONS[1][0]]["status"] == "pending"


async def test_locked_chapter_rejects_new_batch(pool, account):
    report = await _report(pool, account)
    await _ledger(pool, account, report.id)
    await chapter_dal.set_lifecycle(
        pool,
        account_id=account,
        report_id=report.id,
        chapter_key="env",
        lifecycle="locked",
    )

    with pytest.raises(chapter_dal.ChapterLockedError):
        await _enqueue(pool, account, report.id)


async def test_expired_lease_is_recovered_as_failed(pool, account):
    report = await _report(pool, account)
    await _ledger(pool, account, report.id)
    run, _ = await _enqueue(pool, account, report.id)
    await chapter_dal.claim_run(pool, worker_id="w-1", report_id=report.id)
    await pool.execute(
        "update chapter_generation_runs set lease_expires_at = $2 where id = $1",
        run.id,
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert await chapter_dal.recover_expired_runs(pool) >= 1

    recovered = await chapter_dal.get_run(
        pool, account_id=account, report_id=report.id, run_id=run.id
    )
    assert recovered.status == "failed"
    assert recovered.failure_code == "lease_expired"
    # 恢复必须释放租约，否则同一行会一直满足「running 需持租约」的检查反向约束。
    row = await pool.fetchrow(
        "select leased_by, lease_token, lease_expires_at from chapter_generation_runs where id = $1",
        run.id,
    )
    assert (row["leased_by"], row["lease_token"], row["lease_expires_at"]) == (
        None,
        None,
        None,
    )

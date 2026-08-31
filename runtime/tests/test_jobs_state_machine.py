# F018 §4.2 状态机确定性测试（纯逻辑，无数据库依赖）
import datetime as _dt

import pytest

from runtime.core import jobs


def _iso(seconds_ago: int = 0) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds_ago)).isoformat()


def _job(**overrides) -> jobs.AnalysisJob:
    base = dict(job_id="j1", kind="parse", input_ref="obj://x/1/hash")
    base.update(overrides)
    return jobs.AnalysisJob(**base)


def test_initial_state_pending():
    job = _job()
    assert job.status == jobs.PENDING
    assert job.attempts == 0


def test_claim_pending_job():
    job = _job()
    claimed = jobs.claim_next_jobs([job], now_iso=_iso())
    assert claimed == [job]
    assert job.status == jobs.RUNNING
    assert job.attempts == 1
    assert job.heartbeat_at is not None


def test_claim_skip_running_fresh_job():
    fresh = _job(status=jobs.RUNNING, heartbeat_at=_iso(seconds_ago=10), attempts=1)
    claimed = jobs.claim_next_jobs([fresh], now_iso=_iso(), heartbeat_stale_seconds=60)
    assert claimed == []
    assert fresh.status == jobs.RUNNING  # 未超时不可抢占


def test_claim_recover_stale_running_to_retryable_then_pending():
    stale = _job(status=jobs.RUNNING, heartbeat_at=_iso(seconds_ago=120), attempts=1, max_attempts=3)
    claimed = jobs.claim_next_jobs([stale], now_iso=_iso(), heartbeat_stale_seconds=60)
    assert claimed == []  # 第一轮只回收，不直接重跑
    assert stale.status == jobs.PENDING  # 回收为 retryable 且 attempts 未耗尽 -> 回到 pending
    assert stale.error_code == "heartbeat_timeout"
    # 第二轮领取
    claimed2 = jobs.claim_next_jobs([stale], now_iso=_iso())
    assert len(claimed2) == 1
    assert stale.status == jobs.RUNNING
    assert stale.attempts == 2


def test_fail_exhausts_attempts_goes_failed():
    job = _job(status=jobs.RUNNING, attempts=3, max_attempts=3)
    jobs.fail(job, "boom")
    assert job.status == jobs.FAILED
    assert job.error_code == "boom"


def test_fail_with_remaining_attempts_goes_retryable():
    job = _job(status=jobs.RUNNING, attempts=1, max_attempts=3)
    jobs.fail(job, "boom", "细节")
    assert job.status == jobs.RETRYABLE
    assert job.attempts == 1
    assert job.error_message == "细节"


def test_terminal_states_immutable():
    done = _job(status=jobs.COMPLETED)
    with pytest.raises(ValueError):
        jobs.complete(done)
    with pytest.raises(ValueError):
        jobs.cancel(done)
    failed = _job(status=jobs.FAILED)
    with pytest.raises(ValueError):
        jobs.fail(failed, "again")


def test_complete_transition():
    job = _job(status=jobs.RUNNING)
    jobs.complete(job)
    assert job.status == jobs.COMPLETED


def test_cancel_pending():
    job = _job()
    jobs.cancel(job)
    assert job.status == jobs.CANCELLED


def test_heartbeat_requires_running():
    job = _job()
    with pytest.raises(ValueError):
        jobs.heartbeat(job, _iso())


def test_retry_resets_error():
    job = _job(status=jobs.RETRYABLE, attempts=1, max_attempts=3, error_code="x")
    jobs.retry(job)
    assert job.status == jobs.PENDING
    assert job.error_code is None


def test_idempotency_key_components():
    assert jobs.build_idempotency_key("parse", "obj://x", "p1") == "parse:obj://x:p1"
    assert jobs.build_idempotency_key("parse", "obj://x") == "parse:obj://x:"


def test_summarize():
    jobs_list = [
        _job(job_id="a"),
        _job(job_id="b", status=jobs.RUNNING),
        _job(job_id="c", status=jobs.COMPLETED),
    ]
    s = jobs.summarize(jobs_list)
    assert s == {"pending": 1, "running": 1, "completed": 1, "retryable": 0,
                 "failed": 0, "cancelled": 0, "total": 3}
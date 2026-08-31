"""E2.1 P1-C: single-owner process fence 纯单元验证（PURE_UNIT，不连库）。

覆盖：
- _shutdown gating：fence 后（_shutdown=True）主循环不再 claim 新任务（fence-before-claim=0）。
- claim-before-fence：循环已进入 poll（claim 已提交）后收到 fence，job 自然 drain，
  循环退出，**不**二次 claim（无 claim-vs-fence 数据库锁竞争）。
- inline claim 回归：移除 is_pickup_admitted 后，_after_close_poll_once 仍走
  FOR UPDATE SKIP LOCKED 内联 claim（queued → running → execute_after_close_run）。
- 共享 _shutdown 的 co-process drain（_drain_co_process）不 Task.cancel，自然退出。
- 反向回归：worker 模块不再暴露 is_pickup_admitted（admission 子系统已删除）。
"""
import asyncio

import pytest

import app.worker as worker


class _FakeJob:
    def __init__(self):
        self.status = "queued"
        self.id = "j1"
        self.job_name = "after_close_orchestrator"
        self.business_date = None
        self.worker_instance_id = None
        self.started_at = None
        self.heartbeat_at = None
        self.lease_expires_at = None
        self.lease_epoch = 0
        self.metadata_json = None
        self.attempt_no = 0


class FakeSession:
    """PURE_UNIT 用 fake session：不连库，仅记录 commit 并回放预设 job。"""

    def __init__(self):
        self.committed = False
        self.job = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return self

    def scalars(self, *a, **k):
        return self

    def one_or_none(self, *a, **k):
        return self.job

    def scalar_one_or_none(self, *a, **k):
        return self.job

    def all(self, *a, **k):
        return []

    async def commit(self, *a, **k):
        self.committed = True

    async def rollback(self, *a, **k):
        pass

    async def get(self, *a, **k):
        return None

    def add(self, *a, **k):
        pass

    def merge(self, *a, **k):
        pass

    def refresh(self, *a, **k):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    """将 worker 的 DB / 启动恢复 / co-process 全部替换为 pure-unit 友好 stub。"""
    sess = FakeSession()
    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: sess)

    async def _noop_recover(*a, **k):
        return 0

    monkeypatch.setattr(worker, "recover_stale_scheduler_job_runs", _noop_recover)
    monkeypatch.setattr(worker, "recover_replaced_incarnation_runs", _noop_recover)
    monkeypatch.setattr(worker, "auto_resume_interrupted_after_close_runs", _noop_recover)

    async def _noop_coproc(*a, **k):
        return None

    monkeypatch.setattr(worker, "_run_auction_scheduler_co_process", _noop_coproc)
    monkeypatch.setattr(worker, "run_chip_consensus_worker", _noop_coproc)
    monkeypatch.setattr(worker, "_heartbeat_loop", _noop_coproc)
    monkeypatch.setattr(worker, "WORKER_INTERVAL", 0.01)
    return sess


def test_no_admission_import():
    # admission 子系统已删除：worker 不得再暴露 is_pickup_admitted
    assert not hasattr(worker, "is_pickup_admitted")


async def test_fence_before_claim_no_new_pickup(fake_db, monkeypatch):
    # fence 之后（_shutdown=True）：主循环在领取新任务前检查 _shutdown，
    # 不再调用 _after_close_poll_once → 不会发生新的 pickup。
    calls = {"n": 0}

    async def _fake_poll():
        calls["n"] += 1
        return False

    monkeypatch.setattr(worker, "_after_close_poll_once", _fake_poll)
    worker._shutdown = True
    await worker.run_after_close_orchestrator_worker()
    assert calls["n"] == 0
    worker._shutdown = False


async def test_claim_before_fence_drains_and_exits(fake_db, monkeypatch):
    # claim-before-fence：循环已进入 poll（claim 已提交，job 正在执行）后收到 fence
    # （_shutdown=True）。正在执行的 job 自然 drain 到 terminal 后循环退出，
    # 不再发生第二次 claim —— 这就是“线性化点 = 容器 EXITED + running==0”的语义来源。
    calls = {"n": 0}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _fake_poll():
        calls["n"] += 1
        entered.set()
        await release.wait()  # 模拟正在执行的 running job，等待 drain
        return True

    monkeypatch.setattr(worker, "_after_close_poll_once", _fake_poll)
    worker._shutdown = False
    task = asyncio.create_task(worker.run_after_close_orchestrator_worker())
    await asyncio.wait_for(entered.wait(), timeout=5)

    # fence 在 claim 已提交、job 正在运行时到达：合法，不应中断当前 job
    worker._shutdown = True
    release.set()
    await asyncio.wait_for(task, timeout=5)

    assert calls["n"] == 1  # 仅一次 claim，fence 后无第二次
    worker._shutdown = False


async def test_inline_claim_regression(fake_db, monkeypatch):
    # 移除 is_pickup_admitted 后，_after_close_poll_once 仍内联 claim：
    # queued job → status=running → commit → execute_after_close_run。
    # 因 execute_after_close_run 在函数内局部 import（非模块属性），改用
    # job.status + session.commit 来断言 claim 路径（无 admission 拦截）已执行。
    sess = fake_db
    job = _FakeJob()
    sess.job = job
    result = await worker._after_close_poll_once()
    assert result is True
    assert job.status == "running"  # 内联 FOR UPDATE SKIP LOCKED claim 生效
    assert sess.committed is True


async def test_inline_claim_no_job(fake_db, monkeypatch):
    sess = fake_db
    sess.job = None
    result = await worker._after_close_poll_once()
    assert result is False
    assert sess.committed is False


async def test_drain_co_process_no_cancel():
    # SIGTERM drain：await 当前 item 自然完成，绝不 Task.cancel。
    async def _work():
        await asyncio.sleep(0.01)

    t = asyncio.create_task(_work())
    await worker._drain_co_process(t, "x")
    assert t.done()
    assert not t.cancelled()


async def test_drain_co_process_already_done():
    async def _work():
        return None

    t = asyncio.create_task(_work())
    await t
    # 已完成的 task 应立即返回，不报错
    await worker._drain_co_process(t, "x")


async def test_drain_co_process_exception_isolated():
    async def _boom():
        raise RuntimeError("kaboom")

    t = asyncio.create_task(_boom())
    # 异常被隔离（仅记录），不向上抛出
    await worker._drain_co_process(t, "x")
    assert t.done()

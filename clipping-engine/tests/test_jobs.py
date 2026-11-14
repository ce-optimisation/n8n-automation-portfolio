import time

from clipengine.db import Database
from clipengine.jobs import JobQueue, PermanentError, TransientError


def make_q(tmp_path):
    return JobQueue(Database(tmp_path / "q.sqlite3"), max_attempts=3)


def test_enqueue_dedupes_active_jobs(tmp_path):
    q = make_q(tmp_path)
    a = q.enqueue("x", dedupe_key="k1")
    b = q.enqueue("x", dedupe_key="k1")
    assert a == b
    assert q.db.one("SELECT COUNT(*) n FROM jobs")["n"] == 1


def test_success_and_dedupe_after_completion(tmp_path):
    q = make_q(tmp_path)
    ran = []
    q.register("x", lambda ctx: ran.append(ctx.payload["v"]))
    q.enqueue("x", payload={"v": 1}, dedupe_key="k")
    q.drain(10)
    assert ran == [1]
    assert q.db.one("SELECT status FROM jobs")["status"] == "done"
    # A finished job does not block a new one with the same key.
    assert q.enqueue("x", payload={"v": 2}, dedupe_key="k") != 1


def test_transient_errors_retry_then_fail(tmp_path):
    q = make_q(tmp_path)
    calls = []

    def flaky(ctx):
        calls.append(1)
        raise TransientError("network blip")
    q.register("x", flaky)
    jid = q.enqueue("x")
    for _ in range(3):
        q.db.execute("UPDATE jobs SET run_after = 0")
        q.run_one()
    job = q.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert len(calls) == 3 and job["status"] == "failed" and "network blip" in job["error"]


def test_transient_error_then_success(tmp_path):
    q = make_q(tmp_path)
    state = {"n": 0}

    def h(ctx):
        state["n"] += 1
        if state["n"] == 1:
            raise TransientError("once")
    q.register("x", h)
    jid = q.enqueue("x")
    q.run_one()
    assert q.db.one("SELECT status, run_after FROM jobs WHERE id=?", (jid,))["run_after"] > time.time()
    q.db.execute("UPDATE jobs SET run_after = 0")
    q.run_one()
    assert q.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "done"


def test_permanent_error_fails_immediately_and_calls_hook(tmp_path):
    q = make_q(tmp_path)
    hook = []
    q.register("x", lambda ctx: (_ for _ in ()).throw(PermanentError("bad file")))
    q.register("x:failed", lambda ctx: hook.append(ctx.id))
    jid = q.enqueue("x")
    q.run_one()
    assert q.db.one("SELECT status, attempts FROM jobs WHERE id=?", (jid,))["attempts"] == 1
    assert hook == [jid]


def test_recover_requeues_running_jobs_after_crash(tmp_path):
    q = make_q(tmp_path)
    jid = q.enqueue("x")
    q.claim()  # simulate a crash mid-job
    assert q.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "running"
    q2 = JobQueue(Database(tmp_path / "q.sqlite3"))  # "restart"
    assert q2.recover() == 1
    ran = []
    q2.register("x", lambda ctx: ran.append(ctx.id))
    q2.drain(10)
    assert ran == [jid]


def test_manual_retry_and_cancel(tmp_path):
    q = make_q(tmp_path)
    q.register("x", lambda ctx: (_ for _ in ()).throw(PermanentError("nope")))
    jid = q.enqueue("x")
    q.run_one()
    assert q.retry(jid)
    assert q.db.one("SELECT status, attempts FROM jobs WHERE id=?", (jid,))["status"] == "queued"
    assert q.cancel(jid)
    assert q.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "cancelled"

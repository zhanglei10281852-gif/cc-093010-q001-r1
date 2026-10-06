from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from app.pilots.service import PilotOperationsService
from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import get_connection, transaction


PROTOCOL = {
    "code": "gait-assist",
    "name": "外骨骼步态体验方案",
    "capability": "gait-assist",
    "parameter_schema": {
        "minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30},
        "assist_level": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "scene": {"type": "string", "required": True, "choices": ["stairs", "flat"]},
    },
    "default_parameters": {"assist_level": 0.4},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}


def submit_payload(key: str, *, user: str = "pilot-operator-1", priority: int = 50) -> dict:
    return {
        "protocol_code": "gait-assist",
        "project_code": "expo-health-a",
        "requested_by": user,
        "parameters": {"minutes": 8, "scene": "stairs"},
        "priority": priority,
        "idempotency_key": key,
    }


def create_protocol(client) -> None:
    response = client.post("/api/pilots/protocols?actor=administrator", json=PROTOCOL)
    assert response.status_code == 201, response.text


def test_protocol_submission_idempotency_and_parameter_validation(client):
    create_protocol(client)
    first = client.post("/api/pilots/sessions", json=submit_payload("request-000001"))
    second = client.post("/api/pilots/sessions", json=submit_payload("request-000001"))
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    invalid = submit_payload("request-000002")
    invalid["parameters"]["minutes"] = 50
    rejected = client.post("/api/pilots/sessions", json=invalid)
    assert rejected.status_code == 422


def test_priority_capability_claim_and_observation_version(client):
    create_protocol(client)
    low = client.post("/api/pilots/sessions", json=submit_payload("priority-low", priority=10)).json()
    high = client.post("/api/pilots/sessions", json=submit_payload("priority-high", priority=90)).json()
    no_match = client.post("/api/pilots/sessions/claim", json={"site_code": "w0", "capabilities": ["other"], "lease_seconds": 60})
    assert no_match.status_code == 200 and no_match.json()["session"] is None
    claimed = client.post("/api/pilots/sessions/claim", json={"site_code": "w1", "capabilities": ["gait-assist"], "lease_seconds": 60})
    assert claimed.status_code == 200
    assert claimed.json()["session"]["id"] == high["id"]
    completed = client.post(
        f"/api/pilots/sessions/{high['id']}/complete",
        json={"site_code": "w1", "observation": {"value": 3.14}, "metrics": {"seconds": 2}, "lease_generation": claimed.json()["session"]["lease_generation"]},
    )
    assert completed.status_code == 200
    details = client.get(f"/api/pilots/session-details/{high['id']}").json()
    assert details["status"] == "succeeded"
    assert details["current_observation_version"] == 1
    assert len(details["observations"]) == 1
    assert low["status"] == "queued"


def test_quota_cancel_retry_priority_and_batch_interventions(client):
    create_protocol(client)
    quota = client.put(
        "/api/pilots/quotas?actor=administrator",
        json={"subject_type": "user", "subject_key": "limited", "max_queued": 1, "max_running": 1, "daily_submissions": 2},
    )
    assert quota.status_code == 200
    one = client.post("/api/pilots/sessions", json=submit_payload("quota-one", user="limited")).json()
    blocked = client.post("/api/pilots/sessions", json=submit_payload("quota-two", user="limited"))
    assert blocked.status_code == 409
    cancelled = client.post(f"/api/pilots/sessions/{one['id']}/cancel", json={"actor": "administrator", "reason": "项目暂停"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    retried = client.post(f"/api/pilots/sessions/{one['id']}/retry", json={"actor": "administrator", "reason": "项目恢复", "priority": 95})
    assert retried.status_code == 200 and retried.json()["priority"] == 95
    other = client.post("/api/pilots/sessions", json=submit_payload("batch-other", user="other-user")).json()
    batch = client.post(
        "/api/pilots/sessions/batch",
        json={"session_ids": [one["id"], other["id"]], "operation": "priority", "actor": "administrator", "reason": "临床合作方临时到场", "priority": 99},
    )
    assert batch.status_code == 200
    assert len(batch.json()["succeeded"]) == 2
    details = client.get(f"/api/pilots/session-details/{one['id']}").json()
    assert [item["action"] for item in details["interventions"]] == ["cancel", "retry", "priority"]


def test_failure_backoff_and_expired_lease_recovery(client):
    from app.database import init_db

    init_db()
    clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=UTC))
    service = PilotOperationsService(get_connection(), clock)
    service.create_protocol(PROTOCOL, "administrator")
    first = service.submit(submit_payload("failure-000001"))
    claimed = service.claim("site-a", ["gait-assist"], 10)
    assert claimed and claimed["id"] == first["id"]
    failed = service.fail(first["id"], "site-a", "sensor_unstable", "步态传感器读数不稳定", True, claimed["lease_generation"])
    assert failed["status"] == "queued"
    assert failed["available_at"] > failed["updated_at"]
    clock.advance(seconds=2)
    claimed_again = service.claim("site-a", ["gait-assist"], 10)
    assert claimed_again and claimed_again["attempt_count"] == 2
    clock.advance(seconds=11)
    recovered = service.recover_expired()
    assert recovered["exhausted"] == [first["id"]]
    details = service.get_session(first["id"])
    assert details["status"] == "failed"
    assert details["interventions"][-1]["action"] == "lease_recovery"


def _start_service(tmp_path, *, start: datetime | None = None):
    import os

    from app.database import close_connection, init_db

    os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = str(tmp_path / "pilot.db")
    close_connection()
    init_db()
    clock = FrozenClock(start or datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
    service = PilotOperationsService(get_connection(), clock)
    service.create_protocol(PROTOCOL, "administrator")
    return service, clock


def test_heartbeat_succeeds_at_boundary_but_old_lease_cannot_extend_after_expiry(tmp_path):
    service, clock = _start_service(tmp_path)
    session = service.submit(submit_payload("lease-boundary-001"))
    claimed = service.claim("site-a", ["gait-assist"], 30)
    generation = claimed["lease_generation"]
    assert generation == 1

    # 恰好推进到租约边界：租约仍有效（>=now），续租成功。
    clock.advance(seconds=30)
    renewed = service.heartbeat(session["id"], "site-a", 30, generation)
    assert renewed["lease_expires_at"] > renewed["updated_at"]

    # 越过边界 1 秒：同一旧请求（无论重放多少次）都不能再延长租约。
    clock.advance(seconds=31)
    for attempt in range(3):
        with pytest.raises(ConflictError) as exc_info:
            service.heartbeat(session["id"], "site-a", 30, generation)
        context = exc_info.value.context
        assert context["current_status"] == "running"
        assert context["lease_owner"] == "site-a"
        assert context["lease_expired"] is True
        assert context["requested_lease_generation"] == generation
    # 重放没有改动任何状态：租约到期时间保持为上次成功续租的值。
    stored = service.get_session(session["id"])
    assert stored["lease_expires_at"] == renewed["lease_expires_at"]
    assert stored["version"] == renewed["version"]


def test_expired_lease_is_requeued_and_old_site_is_fully_rejected(tmp_path):
    service, clock = _start_service(tmp_path)
    session = service.submit(submit_payload("lease-requeue-001"))
    old = service.claim("site-a", ["gait-assist"], 30)
    old_generation = old["lease_generation"]

    clock.advance(seconds=31)
    result = service.recover_expired()
    # 仍有剩余尝试次数：场次重新排队。
    assert result["recovered"] == [session["id"]]
    assert result["exhausted"] == []

    details = service.get_session(session["id"])
    assert details["status"] == "queued"
    assert details["lease_owner"] == ""
    assert details["lease_expires_at"] == ""
    recovery = details["interventions"][-1]
    assert recovery["action"] == "lease_recovery"
    assert recovery["actor"] == "recovery-site"
    # 干预记录保留了接管前后的完整快照。
    import json

    before = json.loads(recovery["before_json"])
    after = json.loads(recovery["after_json"])
    assert before["status"] == "running" and before["lease_owner"] == "site-a"
    assert after["status"] == "queued" and after["lease_owner"] == ""

    # 旧站点的迟到心跳与步态数据全部被拒绝，且能说明当前状态。
    with pytest.raises(ConflictError) as heartbeat_error:
        service.heartbeat(session["id"], "site-a", 30, old_generation)
    assert heartbeat_error.value.context["current_status"] == "queued"
    with pytest.raises(ConflictError) as complete_error:
        service.complete(session["id"], "site-a", {"step": 99}, {}, old_generation)
    assert complete_error.value.context["current_status"] == "queued"
    with pytest.raises(ConflictError) as fail_error:
        service.fail(session["id"], "site-a", "late", "迟到回执", True, old_generation)
    assert fail_error.value.context["current_status"] == "queued"

    # 恢复后由新站点重新领取（即便站点编码相同，也是新一代租约）。
    new = service.claim("site-a", ["gait-assist"], 30)
    assert new["attempt_count"] == 2
    assert new["lease_generation"] == old_generation + 1
    # 旧代次请求不能续到新租约上。
    with pytest.raises(ConflictError) as stale_error:
        service.heartbeat(session["id"], "site-a", 30, old_generation)
    assert stale_error.value.context["stale_lease_generation"] is True
    assert stale_error.value.context["current_lease_generation"] == new["lease_generation"]
    # 新代次可以正常续租与完成。
    assert service.heartbeat(session["id"], "site-a", 30, new["lease_generation"])["status"] == "running"
    completed = service.complete(session["id"], "site-a", {"step": 120}, {"seconds": 28}, new["lease_generation"])
    assert completed["status"] == "succeeded"
    versions = service.get_session(session["id"])["observations"]
    assert len(versions) == 1 and versions[0]["created_by"] == "site-a"


def test_recovery_ends_session_when_attempts_exhausted(tmp_path):
    service, clock = _start_service(tmp_path)
    session = service.submit(submit_payload("lease-exhaust-001"))
    first = service.claim("site-a", ["gait-assist"], 30)
    service.fail(session["id"], "site-a", "sensor", "首次失败", True, first["lease_generation"])
    clock.advance(seconds=31)
    second = service.claim("site-b", ["gait-assist"], 30)
    assert second["attempt_count"] == 2
    clock.advance(seconds=31)
    result = service.recover_expired()
    assert result["recovered"] == []
    assert result["exhausted"] == [session["id"]]
    details = service.get_session(session["id"])
    assert details["status"] == "failed"
    assert details["last_error_code"] == "lease_expired"
    # 旧站点任何请求都无法让场次复活。
    with pytest.raises(ConflictError):
        service.heartbeat(session["id"], "site-b", 30, second["lease_generation"])
    assert service.get_session(session["id"])["status"] == "failed"


def test_observations_and_interventions_survive_recovery_cycle(tmp_path):
    import json

    service, clock = _start_service(tmp_path)
    session = service.submit(submit_payload("lease-preserve-001"))
    first = service.claim("site-a", ["gait-assist"], 30)
    # 第一次尝试在租约有效期内失败退避。
    service.fail(session["id"], "site-a", "sensor", "首次失败", True, first["lease_generation"])
    clock.advance(seconds=2)
    second = service.claim("site-a", ["gait-assist"], 30)
    assert second["lease_generation"] == first["lease_generation"] + 1

    # 既有观察版本在恢复/人工操作后不得丢失。
    connection = get_connection()
    now = connection.execute("SELECT updated_at FROM pilot_sessions WHERE id=?", (session["id"],)).fetchone()[0]
    connection.execute(
        "INSERT INTO pilot_observations(session_id,version,observation_json,metrics_json,observation_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
        (session["id"], 1, '{"step": 1}', '{}', "legacy-digest", "site-a", now),
    )
    connection.execute(
        "UPDATE pilot_sessions SET current_observation_version=1 WHERE id=?", (session["id"],)
    )

    # 第二次尝试租约过期且尝试次数用尽 -> failed，随后人工重试重新排队。
    clock.advance(seconds=31)
    service.recover_expired()
    service.retry(session["id"], "operator-1", "人工安排复测", priority=80)

    details = service.get_session(session["id"])
    assert len(details["observations"]) == 1
    assert details["observations"][0]["observation_digest"] == "legacy-digest"
    actions = [item["action"] for item in details["interventions"]]
    assert actions == ["lease_recovery", "retry"]
    # 干预快照应完整保留持有者与代次信息。
    recovered_after = json.loads(details["interventions"][0]["after_json"])
    assert recovered_after["status"] == "failed"
    assert recovered_after["lease_generation"] == second["lease_generation"]


def test_concurrent_heartbeat_and_recovery_have_a_single_decision(tmp_path):
    service, clock = _start_service(tmp_path)
    session = service.submit(submit_payload("lease-race-001"))
    claimed = service.claim("site-a", ["gait-assist"], 30)
    generation = claimed["lease_generation"]
    clock.advance(seconds=31)

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def renew() -> None:
        barrier.wait()
        try:
            service.heartbeat(session["id"], "site-a", 30, generation)
            outcomes.append("renewed")
        except ConflictError:
            outcomes.append("rejected")

    def recover() -> None:
        barrier.wait()
        result = service.recover_expired()
        outcomes.append("recovered" if result["recovered"] or result["exhausted"] else "noop")

    threads = [threading.Thread(target=renew), threading.Thread(target=recover)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    details = service.get_session(session["id"])
    # 续租必败（租约已过期）；恢复恰好执行一次，只有一个决定生效。
    assert outcomes.count("rejected") == 1
    assert sum(1 for value in outcomes if value in {"recovered", "noop"}) == 1
    assert details["status"] == "queued"
    recovery_records = [item for item in details["interventions"] if item["action"] == "lease_recovery"]
    assert len(recovery_records) == 1


def test_heartbeat_endpoint_requires_generation_and_rejects_expired_lease(client):
    create_protocol(client)
    submitted = client.post("/api/pilots/sessions", json=submit_payload("http-lease-001")).json()
    claimed = client.post("/api/pilots/sessions/claim", json={"site_code": "w1", "capabilities": ["gait-assist"], "lease_seconds": 30})
    assert claimed.status_code == 200
    generation = claimed.json()["session"]["lease_generation"]

    missing = client.post(f"/api/pilots/sessions/{submitted['id']}/heartbeat", json={"site_code": "w1", "lease_seconds": 30})
    assert missing.status_code == 422

    ok = client.post(
        f"/api/pilots/sessions/{submitted['id']}/heartbeat",
        json={"site_code": "w1", "lease_seconds": 30, "lease_generation": generation},
    )
    assert ok.status_code == 200 and ok.json()["lease_owner"] == "w1"


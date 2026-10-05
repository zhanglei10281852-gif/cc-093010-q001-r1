from __future__ import annotations

import os
import threading
from datetime import UTC, datetime

import pytest

from app.pilots.service import PilotOperationsService
from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import close_connection, get_connection, transaction


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
        json={"site_code": "w1", "observation": {"value": 3.14}, "metrics": {"seconds": 2}},
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
    failed = service.fail(first["id"], "site-a", "sensor_unstable", "步态传感器读数不稳定", True)
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


def _expired_service(tmp_path, start: datetime = datetime(2026, 9, 26, 2, 0, tzinfo=UTC), *, db_name: str = "lease-test.db"):
    from app.database import init_db

    os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = str(tmp_path / db_name)
    close_connection()
    init_db()
    clock = FrozenClock(start)
    service = PilotOperationsService(get_connection(), clock)
    service.create_protocol(PROTOCOL, "administrator")
    session = service.submit(submit_payload("lease-boundary-0001"))
    claimed = service.claim("site-a", ["gait-assist"], 10)
    assert claimed and claimed["id"] == session["id"]
    return service, clock, session["id"]


def test_heartbeat_renews_inside_boundary_but_is_rejected_at_and_after_expiry(tmp_path):
    service, clock, session_id = _expired_service(tmp_path)

    clock.advance(seconds=9)
    renewed = service.heartbeat(session_id, "site-a", 10)
    assert renewed["status"] == "running"
    assert renewed["lease_owner"] == "site-a"
    assert renewed["lease_expires_at"] > renewed["updated_at"]

    # 正好推进到租约边界：旧站点续租必须被稳定拒绝。
    clock.advance(seconds=11)
    for _ in range(3):  # 旧请求重放任意次数都不能重新取得控制权
        with pytest.raises(ConflictError) as excinfo:
            service.heartbeat(session_id, "site-a", 10)
        context = excinfo.value.context
        assert context["reason"] == "lease_expired"
        assert context["status"] == "running"
        assert context["lease_owner"] == "site-a"
        assert context["lease_expires_at"] <= context["current_time"]

    details = service.get_session(session_id)
    assert details["lease_expires_at"] != ""  # 被拒续租没有改动任何字段
    assert details["interventions"] == []


def test_late_data_from_old_site_is_rejected_after_recovery_and_new_claim(tmp_path):
    service, clock, session_id = _expired_service(tmp_path)

    clock.advance(seconds=10)
    recovered = service.recover_expired()
    assert recovered["recovered"] == [session_id]

    # 恢复流程接管后，旧站点迟到的失败上报不能再改变场次。
    with pytest.raises(ConflictError) as excinfo:
        service.fail(session_id, "site-a", "late", "迟到的失败上报", True)
    assert excinfo.value.context["reason"] == "not_running"

    claimed_again = service.claim("site-b", ["gait-assist"], 10)
    assert claimed_again and claimed_again["attempt_count"] == 2
    assert claimed_again["lease_owner"] == "site-b"

    # 新站点持约期间，旧站点的续租/观察数据一律拒绝，拒绝结果说明当前持有者。
    with pytest.raises(ConflictError) as heartbeat_exc:
        service.heartbeat(session_id, "site-a", 10)
    assert heartbeat_exc.value.context["reason"] == "owner_changed"
    assert heartbeat_exc.value.context["lease_owner"] == "site-b"

    with pytest.raises(ConflictError) as complete_exc:
        service.complete(session_id, "site-a", {"gait": "late"}, {"seconds": 9})
    assert complete_exc.value.context["reason"] == "owner_changed"
    assert complete_exc.value.context["status"] == "running"

    completed = service.complete(session_id, "site-b", {"gait": "ok"}, {"seconds": 5})
    assert completed["status"] == "succeeded"
    assert completed["current_observation_version"] == 1

    # 场次详情可还原完整处置经过：只有新站点的观察版本，恢复记录完整保留。
    details = service.get_session(session_id)
    assert [item["created_by"] for item in details["observations"]] == ["site-b"]
    recovery = details["interventions"][-1]
    assert recovery["action"] == "lease_recovery"
    assert recovery["before"]["lease_owner"] == "site-a"
    assert recovery["after"]["status"] == "queued"


def test_exhausted_session_recovery_ends_session_and_old_site_cannot_touch_it(tmp_path):
    service, clock, session_id = _expired_service(tmp_path)
    # 第一次尝试失败后重试，第二次持约时耗尽次数。
    service.fail(session_id, "site-a", "sensor_unstable", "传感器不稳定", True)
    clock.advance(seconds=2)
    assert service.claim("site-a", ["gait-assist"], 10)["attempt_count"] == 2
    clock.advance(seconds=10)

    assert service.recover_expired()["exhausted"] == [session_id]
    with pytest.raises(ConflictError) as excinfo:
        service.heartbeat(session_id, "site-a", 10)
    assert excinfo.value.context["reason"] == "not_running"
    details = service.get_session(session_id)
    assert details["status"] == "failed"
    assert details["interventions"][-1]["action"] == "lease_recovery"


def test_recovery_and_heartbeat_concurrency_has_single_decision(tmp_path):
    from app.database import close_connection as close_thread_connection

    service, clock, session_id = _expired_service(tmp_path)
    clock.advance(seconds=10)  # 恰好租约边界：续租已不合格，恢复可接管

    barrier = threading.Barrier(8)
    outcomes: list[tuple[str, str]] = []

    def worker(kind: str) -> None:
        local_service = PilotOperationsService(get_connection(), clock)
        barrier.wait()
        try:
            if kind == "heartbeat":
                local_service.heartbeat(session_id, "site-a", 10)
                outcomes.append((kind, "renewed"))
            else:
                result = local_service.recover_expired()
                outcomes.append((kind, "recovered" if result["recovered"] else "noop"))
        except ConflictError as exc:
            outcomes.append((kind, exc.context["reason"]))
        finally:
            close_thread_connection()

    threads = [threading.Thread(target=worker, args=("heartbeat",)) for _ in range(4)]
    threads += [threading.Thread(target=worker, args=("recovery",)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    recover_wins = [outcome for kind, outcome in outcomes if kind == "recovery" and outcome == "recovered"]
    assert len(recover_wins) == 1  # 恢复最多生效一次
    heartbeat_outcomes = {outcome for kind, outcome in outcomes if kind == "heartbeat"}
    assert heartbeat_outcomes  # 有并发续租参与
    assert heartbeat_outcomes <= {"lease_expired", "not_running"}  # 没有任何一次续租成功

    details = service.get_session(session_id)
    assert details["status"] == "queued"
    assert details["lease_owner"] == ""
    assert len([i for i in details["interventions"] if i["action"] == "lease_recovery"]) == 1

    # 边界前一秒：续租全部成立，恢复一律空转，控制权仍属原站点。
    service2, clock2, session_id2 = _expired_service(tmp_path, datetime(2026, 9, 27, 2, 0, tzinfo=UTC), db_name="lease-test-before-boundary.db")
    clock2.advance(seconds=9)
    barrier2 = threading.Barrier(4)
    outcomes2: list[tuple[str, str]] = []

    def worker2(kind: str) -> None:
        local_service = PilotOperationsService(get_connection(), clock2)
        barrier2.wait()
        try:
            if kind == "heartbeat":
                local_service.heartbeat(session_id2, "site-a", 10)
                outcomes2.append((kind, "renewed"))
            else:
                result = local_service.recover_expired()
                outcomes2.append((kind, "recovered" if result["recovered"] else "noop"))
        finally:
            close_thread_connection()

    threads2 = [threading.Thread(target=worker2, args=("heartbeat",)) for _ in range(2)]
    threads2 += [threading.Thread(target=worker2, args=("recovery",)) for _ in range(2)]
    for thread in threads2:
        thread.start()
    for thread in threads2:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert all(outcome == "noop" for kind, outcome in outcomes2 if kind == "recovery")
    assert all(outcome == "renewed" for kind, outcome in outcomes2 if kind == "heartbeat")
    details2 = service2.get_session(session_id2)
    assert details2["status"] == "running"
    assert details2["lease_owner"] == "site-a"
    assert details2["interventions"] == []


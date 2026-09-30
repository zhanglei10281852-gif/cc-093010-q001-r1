from __future__ import annotations


PRODUCT = {
    "code": "diagnostic-ai",
    "name": "染色体辅助诊断软件",
    "organization": "示例医学科技",
    "origin_country": "中国",
    "category": "辅助诊断",
    "intended_use": "辅助临床人员完成染色体图像切割、排列和异常提示",
    "risk_level": "high",
    "regulatory_status": "研究",
}


SITE = {
    "code": "clinical-a",
    "name": "联合临床观察点",
    "site_type": "医院",
    "region": "浙江",
    "capabilities": ["cytogenetics", "diagnostic-ai"],
    "max_concurrent": 3,
}


def test_product_site_evidence_and_feedback_flow(client):
    product = client.post("/api/catalog/products", json=PRODUCT)
    assert product.status_code == 201, product.text
    site = client.post("/api/catalog/sites", json=SITE)
    assert site.status_code == 201, site.text
    evidence = client.post("/api/catalog/evidence", json={
        "product_code": "diagnostic-ai",
        "evidence_type": "性能",
        "title": "多中心性能摘要",
        "source_name": "联合验证组",
        "source_region": "中国",
        "version": "2026.09",
        "content_digest": "a" * 64,
        "summary": {"samples": 320, "metric": "sensitivity"},
        "submitted_by": "evidence-owner",
    })
    assert evidence.status_code == 201, evidence.text
    reviewed = client.post(f"/api/catalog/evidence/{evidence.json()['id']}/review", json={"reviewer": "reviewer-a", "decision": "accepted", "note": "来源和版本可追溯"})
    assert reviewed.status_code == 200
    feedback = client.post("/api/catalog/feedback", json={
        "product_code": "diagnostic-ai",
        "site_code": "clinical-a",
        "session_reference": "session-001",
        "audience_type": "临床人员",
        "rating": 4,
        "tags": ["效率", "可解释性"],
        "comment": "异常提示需要保留原始图像位置",
        "contact_digest": "contact-a",
        "consent_to_follow_up": True,
    })
    assert feedback.status_code == 201, feedback.text
    summary = client.get("/api/catalog/feedback/summary?product_code=diagnostic-ai")
    assert summary.status_code == 200
    assert summary.json()["items"][0]["feedback_count"] == 1
    readiness = client.get("/api/catalog/insights/products/diagnostic-ai/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["accepted_evidence"] == 1
    tags = client.get("/api/catalog/insights/products/diagnostic-ai/feedback-tags")
    assert tags.status_code == 200
    assert tags.json()["tags"][0]["tag"] in {"效率", "可解释性"}
    utilization = client.get("/api/catalog/insights/sites/clinical-a/utilization")
    assert utilization.status_code == 200
    assert utilization.json()["available_slots"] == 3


def test_evidence_and_feedback_are_idempotent(client):
    client.post("/api/catalog/products", json=PRODUCT)
    client.post("/api/catalog/sites", json=SITE)
    evidence_payload = {
        "product_code": "diagnostic-ai",
        "evidence_type": "安全",
        "title": "安全观察摘要",
        "source_name": "临床观察组",
        "source_region": "浙江",
        "version": "v1",
        "content_digest": "b" * 64,
        "summary": {},
        "submitted_by": "owner-a",
    }
    first = client.post("/api/catalog/evidence", json=evidence_payload).json()
    second = client.post("/api/catalog/evidence", json=evidence_payload).json()
    assert first["id"] == second["id"]
    feedback_payload = {
        "product_code": "diagnostic-ai",
        "site_code": "clinical-a",
        "session_reference": "same-session",
        "audience_type": "公众",
        "rating": 5,
        "tags": ["易用"],
        "comment": "体验顺畅",
        "contact_digest": "anon-1",
        "consent_to_follow_up": False,
    }
    one = client.post("/api/catalog/feedback", json=feedback_payload).json()
    two = client.post("/api/catalog/feedback", json=feedback_payload).json()
    assert one["id"] == two["id"]

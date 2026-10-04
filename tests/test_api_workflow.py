"""端到端 API 流程测试。"""

import threading

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app import models


SLOT_PAYLOAD = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "B02", "stack": "B", "tier": 2, "capacity": 100, "arm": 3.0},
    ],
    "adjacency": [["A01", "B01"], ["A02", "B02"]],
    "incompatible_classes": [["1", "5"]],
    "min_moment": -100000,
    "max_moment": 100000,
}

MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "C002", "weight": 20, "hazmat": None, "discharge_order": 1},
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}


def _seed(client) -> tuple[int, int]:
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    return sr, mr


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_full_compute_confirm_flow(client):
    sr, mr = _seed(client)
    r = client.post("/api/plans/compute", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 201
    plan = r.json()
    assert plan["revision_no"] == 1
    assert plan["status"] == "draft"
    assert plan["max_occupied_tier"] == 1
    assert plan["total_moment"] == 60  # 10*2 + 20*2
    pairs = [(a["container_no"], a["slot_code"]) for a in plan["assignments"]]
    assert pairs == [("C001", "A01"), ("C002", "B01")]

    # 错误资料修订号确认 -> 409，方案仍为 draft
    bad = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": 999, "manifest_revision": mr
    })
    assert bad.status_code == 409
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "draft"

    # 正确确认
    ok = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert ok.status_code == 200
    assert ok.json()["status"] == "confirmed"
    assert ok.json()["confirmed_at"] is not None

    # 幂等重复确认
    again = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert again.status_code == 200
    assert again.json()["status"] == "confirmed"


def test_infeasible_plan_not_saved(client):
    sr, mr = _seed(client)
    # 加入更多箱使无解（槽位不足）
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": f"X{i}", "weight": 50, "discharge_order": 1} for i in range(5)
        ]
    })
    r = client.post("/api/plans/compute", json={
        "slots_revision": sr, "manifest_revision": mr + 1
    })
    assert r.status_code == 422
    assert "无解" in r.json()["detail"]
    plans = client.get("/api/plans").json()
    assert plans == []


def test_stale_revision_rejected_on_confirm(client):
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()

    # 槽位资料产生新修订，旧方案过期
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 409
    assert "已过期" in r.json()["detail"]
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "draft"


def test_amend_chain_and_slot_release(client):
    sr, mr = _seed(client)
    p1 = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{p1['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })

    # 新箱清单（同槽资料仍可配载），基于已确认方案修订
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1},
        ]
    })
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
    assert p2["revision_no"] == 2
    assert p2["status"] == "draft"
    assert p2["parent_id"] == p1["plan_id"]
    assert p2["root_id"] == p1["root_id"] or p2["root_id"] == p1["plan_id"]

    # 确认新修订：旧确认被取代，占用转移；无槽位冲突
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr + 1
    })
    assert ok.status_code == 200
    old = client.get(f"/api/plans/{p1['plan_id']}").json()
    assert old["status"] == "superseded"
    new = client.get(f"/api/plans/{p2['plan_id']}").json()
    assert new["status"] == "confirmed"

    db = SessionLocal()
    try:
        slots = {r.slot_code for r in db.query(models.ConfirmedSlot).all()}
        assert slots == {"A01"}
    finally:
        db.close()


def test_concurrent_confirm_disjoint_plans(client):
    """两个独立方案占用不同槽位：并发确认均应成功。"""
    sr, mr = _seed(client)
    p1 = client.post("/api/plans/compute", json={}).json()
    p2 = client.post("/api/plans/compute", json={}).json()

    results = {}

    def confirm(pid, key):
        local = TestClient(app)
        results[key] = local.post(f"/api/plans/{pid}/confirm", json={
            "slots_revision": sr, "manifest_revision": mr
        }).status_code

    t1 = threading.Thread(target=confirm, args=(p1["plan_id"], 1))
    t2 = threading.Thread(target=confirm, args=(p2["plan_id"], 2))
    t1.start(); t2.start(); t1.join(); t2.join()

    # 同槽位唯一约束 -> 恰好一个成功，另一个 409
    assert sorted(results.values()) == [200, 409]

    db = SessionLocal()
    try:
        confirmed = db.query(models.ConfirmedSlot).count()
        assert confirmed == 2  # 成功方案占用两个槽
    finally:
        db.close()


def test_get_nonexistent_plan_404(client):
    assert client.get("/api/plans/nope").status_code == 404


def _commit_slot_revision(revision: int) -> None:
    """在独立会话上直接提交一条新槽位资料修订（模拟资料更新的实际提交）。"""
    s = SessionLocal()
    try:
        s.add(models.SlotRevision(
            revision=revision, min_moment=-100000, max_moment=100000,
        ))
        s.commit()
    finally:
        s.close()


def _commit_manifest_revision(revision: int) -> None:
    """在独立会话上直接提交一条新箱清单修订（模拟资料更新的实际提交）。"""
    s = SessionLocal()
    try:
        s.add(models.ManifestRevision(revision=revision))
        s.commit()
    finally:
        s.close()


def test_confirm_race_slot_revision_committed_before_lock(client, monkeypatch):
    """读检查与拿写锁之间提交了新槽位资料：确认按过期拒绝（409），草稿不变。"""
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()

    from app import services

    original = services._latest_slot_revision
    bumped = False

    def racy_latest(db):
        nonlocal bumped
        latest = original(db)
        if not bumped:
            bumped = True
            # 模拟竞态：确认读取最新修订之后、取得写锁之前，新槽位资料先提交
            _commit_slot_revision(latest + 1)
        return latest

    monkeypatch.setattr(services, "_latest_slot_revision", racy_latest)

    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 409
    assert "已过期" in r.json()["detail"]
    # 草稿与槽位占用均不变
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "draft"
    db = SessionLocal()
    try:
        assert db.query(models.ConfirmedSlot).count() == 0
    finally:
        db.close()


def test_confirm_race_manifest_revision_committed_before_lock(client, monkeypatch):
    """读检查与拿写锁之间提交了新箱清单：确认按过期拒绝（409），草稿不变。"""
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()

    from app import services

    original = services._latest_manifest_revision
    bumped = False

    def racy_latest(db):
        nonlocal bumped
        latest = original(db)
        if not bumped:
            bumped = True
            # 模拟竞态：确认读取最新修订之后、取得写锁之前，新箱清单先提交
            _commit_manifest_revision(latest + 1)
        return latest

    monkeypatch.setattr(services, "_latest_manifest_revision", racy_latest)

    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 409
    assert "已过期" in r.json()["detail"]
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "draft"
    db = SessionLocal()
    try:
        assert db.query(models.ConfirmedSlot).count() == 0
    finally:
        db.close()


def test_confirm_race_keeps_chain_and_slot_occupancy(client, monkeypatch):
    """竞态拒绝时：同链旧已确认方案、草稿与槽位占用均不变。"""
    sr, mr = _seed(client)
    p1 = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{p1['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })

    # 新箱清单（rev2），基于已确认 p1 修订出草稿 p2
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1},
        ]
    })
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
    assert p2["status"] == "draft"

    from app import services

    original = services._latest_slot_revision
    bumped = False

    def racy_latest(db):
        nonlocal bumped
        latest = original(db)
        if not bumped:
            bumped = True
            # p2 确认读检查之后、拿写锁之前，新槽位资料先提交
            _commit_slot_revision(latest + 1)
        return latest

    monkeypatch.setattr(services, "_latest_slot_revision", racy_latest)

    r = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr + 1
    })
    assert r.status_code == 409
    assert "已过期" in r.json()["detail"]

    # 旧已确认方案不被取代、草稿保持草稿、槽位占用仍属旧方案
    assert client.get(f"/api/plans/{p1['plan_id']}").json()["status"] == "confirmed"
    assert client.get(f"/api/plans/{p2['plan_id']}").json()["status"] == "draft"
    db = SessionLocal()
    try:
        rows = db.query(models.ConfirmedSlot).all()
        assert {row.slot_code for row in rows} == {"A01", "B01"}
        assert {row.plan_id for row in rows} == {p1["plan_id"]}
    finally:
        db.close()


def test_confirm_committed_before_data_update_not_revoked(client):
    """确认先提交则成功；其后的资料更新不追溯撤销，原样重复确认仍幂等。"""
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()
    ok = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert ok.status_code == 200

    # 确认提交之后才产生新资料修订：不追溯撤销已确认方案
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "confirmed"

    # 同一已确认方案的原样重复确认保持幂等
    again = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert again.status_code == 200
    assert again.json()["status"] == "confirmed"


def test_concurrent_confirm_and_data_update_commit_order(client):
    """确认与资料更新并发：按实际提交先后定论，且结果状态自洽。"""
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()

    results = {}

    def confirm():
        local = TestClient(app)
        results["confirm"] = local.post(
            f"/api/plans/{plan['plan_id']}/confirm",
            json={"slots_revision": sr, "manifest_revision": mr},
        ).status_code

    def bump():
        local = TestClient(app)
        results["bump"] = local.post(
            "/api/slot-revisions", json=SLOT_PAYLOAD
        ).status_code

    t1 = threading.Thread(target=confirm)
    t2 = threading.Thread(target=bump)
    t1.start(); t2.start(); t1.join(); t2.join()

    # 资料更新本身总是成功（要么先于确认提交，要么在确认提交后生效）
    assert results["bump"] == 201
    status = client.get(f"/api/plans/{plan['plan_id']}").json()["status"]
    db = SessionLocal()
    try:
        occupied = db.query(models.ConfirmedSlot).count()
    finally:
        db.close()
    if results["confirm"] == 200:
        # 确认先提交：成功，随后的资料更新不追溯撤销
        assert status == "confirmed"
        assert occupied == 2
    else:
        # 资料更新先提交：确认按过期拒绝，草稿与槽位占用不变
        assert results["confirm"] == 409
        assert status == "draft"
        assert occupied == 0

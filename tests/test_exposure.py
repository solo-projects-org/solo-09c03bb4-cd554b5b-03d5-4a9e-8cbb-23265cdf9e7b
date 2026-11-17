"""超温暴露核算自测：剂量数学、越界区间溯源、无法判定分支、版本一致性与旧版回看。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.dose import compute_dose

CSV_HEADER = "segment,local_seconds,temperature\n"


def make_client(tmp_path, monkeypatch, worker="0", delay="0") -> TestClient:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("WORKER_ENABLED", worker)
    monkeypatch.setenv("RECALC_DELAY", delay)
    from app.main import create_app
    return TestClient(create_app())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as c:
        yield c


def upload(client: TestClient, rows, filename="data.csv"):
    content = CSV_HEADER + "".join(f"{s},{sec},{t}\n" for s, sec, t in rows)
    return client.post("/api/uploads", files={"file": (filename, content, "text/csv")})


def set_anchors(client: TestClient, seg, l1, u1, l2, u2, version=None):
    payload = {"segment": seg, "local1": l1, "utc1": u1, "local2": l2, "utc2": u2}
    if version is not None:
        payload["version"] = version
    return client.post("/api/anchors", json=payload)


def exposure(client: TestClient, start, end, limit="5", allowed="5", max_gap="10"):
    return client.get("/api/exposure", params={
        "start": start, "end": end, "limit": limit,
        "allowed": allowed, "max_gap": max_gap,
    })


# 常用锚点：段 1，本地秒 = UTC 秒，2026-01-01T00:00:00Z 起
def anchor_seg1(client, day="2026-01-01"):
    return set_anchors(client, 1, 0, f"{day}T00:00:00Z", 3600, f"{day}T01:00:00Z")


# ---------------------------------------------------------------- 剂量数学

def test_dose_basic_and_interval_refs(client):
    upload(client, [(1, 0, 2.0), (1, 60, 8.0), (1, 120, 8.0), (1, 180, 2.0)])
    anchor_seg1(client)
    body = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:03:00", allowed="5").json()
    assert body["status"] == "ok" and body["current"] is True
    # 梯形+三角：45+180+45 = 270 °C·s = 4.5 °C·min
    assert body["dose"] == 4.5
    assert body["decision"] == "release" and body["conclusion"] == "release"
    assert body["calc_version"] >= 1
    iv = body["intervals"][0]
    assert iv["start_utc"] == "2026-01-01T00:00:30.000Z"
    assert iv["end_utc"] == "2026-01-01T00:02:30.000Z"
    assert iv["peak_temperature"] == 8.0
    # 越界区间两端读数的批次号、原行号
    assert (iv["start_reading"]["batch_id"], iv["start_reading"]["row_no"]) == (1, 1)
    assert (iv["end_reading"]["batch_id"], iv["end_reading"]["row_no"]) == (1, 4)
    assert [p["row_no"] for p in iv["exceed_readings"]] == [2, 3]
    # 夹住时段首尾的读数
    assert body["window"]["start"]["before"]["row_no"] == 1
    assert body["window"]["start"]["after"]["row_no"] == 1  # 起点恰为记录点
    assert body["window"]["end"]["after"]["row_no"] == 4
    # 允许剂量收紧 → 建议隔离
    body = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:03:00", allowed="4").json()
    assert body["decision"] == "quarantine" and body["conclusion"] == "quarantine"


def test_window_boundary_interpolation(client):
    upload(client, [(1, 0, 2.0), (1, 60, 8.0)])
    anchor_seg1(client)
    # 窗口首端落在两点之间：T(00:00:30)=5°C，剂量为三角形 3×30/2=45 °C·s
    body = exposure(client, "2026-01-01 00:00:30", "2026-01-01 00:01:00",
                    allowed="0.5").json()
    assert body["status"] == "ok"
    assert body["dose"] == 0.75
    assert body["decision"] == "quarantine"
    iv = body["intervals"][0]
    assert iv["start_utc"] == "2026-01-01T00:00:30.000Z"  # 自窗口起点即越界
    assert iv["start_reading"]["row_no"] == 1            # 首端夹持读数
    assert iv["end_reading"]["row_no"] == 2
    # 允许剂量恰等于剂量 → 可放行（边界）
    body = exposure(client, "2026-01-01 00:00:30", "2026-01-01 00:01:00",
                    allowed="0.75").json()
    assert body["decision"] == "release"


def test_cross_day_continuous(client):
    upload(client, [(1, 0, 8.0), (1, 60, 8.0), (1, 120, 8.0), (1, 180, 8.0)])
    set_anchors(client, 1, 0, "2026-01-01T23:58:00Z", 240, "2026-01-02T00:02:00Z")
    body = exposure(client, "2026-01-01 23:58:00", "2026-01-02 00:01:00",
                    allowed="100").json()
    assert body["status"] == "ok"
    assert body["dose"] == 9.0  # 3°C × 180s，跨日连续累计，不按日截断
    assert len(body["intervals"]) == 1
    assert body["intervals"][0]["start_utc"].startswith("2026-01-01T23")
    assert body["intervals"][0]["end_utc"].startswith("2026-01-02T00")


def test_zero_dose_when_all_below_limit(client):
    upload(client, [(1, 0, 2.0), (1, 600, 3.0)])
    anchor_seg1(client)
    body = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:10:00",
                    allowed="0").json()
    assert body["status"] == "ok" and body["dose"] == 0.0
    assert body["decision"] == "release" and body["intervals"] == []


def test_single_spike_unit():
    """纯函数：单点尖峰的越界区间与两端读数。"""
    pts = [
        {"reading_id": i, "batch_id": 1, "row_no": i, "segment": 1,
         "local_seconds": t, "utc_time": f"2026-01-01T00:0{i - 1}:00.000Z",
         "temperature": temp, "ts": float(t)}
        for i, (t, temp) in enumerate([(0, 2.0), (60, 8.0), (120, 2.0)], start=1)
    ]
    res = compute_dose(pts, 0.0, 120.0, 5.0, 600.0, 10.0)
    assert res["status"] == "ok" and res["dose"] == 1.5  # 两个三角形 45+45 °C·s
    iv = res["intervals"][0]
    assert iv["start_reading"]["row_no"] == 1 and iv["end_reading"]["row_no"] == 3
    assert [p["row_no"] for p in iv["exceed_readings"]] == [2]


# ---------------------------------------------------------------- 无法判定

def test_missing_edge_readings_undetermined(client):
    upload(client, [(1, 60, 8.0), (1, 120, 8.0)])
    anchor_seg1(client)
    # 窗口开始早于首个读数
    body = exposure(client, "2025-12-31 23:59:00", "2026-01-01 00:02:00").json()
    assert body["status"] == "undetermined"
    assert body["reasons"][0]["code"] == "NO_START_READING"
    assert "dose" not in body and "decision" not in body  # 不得以零剂量放行
    assert body["conclusion"] == "undetermined"
    # 窗口结束晚于末个读数
    body = exposure(client, "2026-01-01 00:01:00", "2026-01-01 02:00:00").json()
    assert body["status"] == "undetermined"
    assert body["reasons"][0]["code"] == "NO_END_READING"


def test_no_data_undetermined(client):
    body = exposure(client, "2026-01-01 00:00:00", "2026-01-01 01:00:00").json()
    assert body["status"] == "undetermined"
    assert body["conclusion"] != "release"


def test_gap_too_large_undetermined(client):
    upload(client, [(1, 0, 8.0), (1, 600, 8.0), (1, 1200, 8.0)])
    anchor_seg1(client)
    body = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:20:00",
                    max_gap="5").json()
    assert body["status"] == "undetermined"
    reason = body["reasons"][0]
    assert reason["code"] == "GAP_TOO_LARGE"
    assert reason["gaps"][0]["from"]["row_no"] == 1
    assert reason["gaps"][0]["to"]["row_no"] == 2
    assert reason["gaps"][0]["gap_seconds"] == 600
    assert "dose" not in body
    # 间隔上限放宽 → 可判定（3°C × 1200s = 60 °C·min）
    body = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:20:00",
                    max_gap="10").json()
    assert body["status"] == "ok" and body["dose"] == 60.0


def test_conflicting_same_utc_undetermined(client):
    upload(client, [(1, 0, 3.0), (1, 600, 4.0)])
    upload(client, [(2, 0, 9.0), (2, 300, 5.0)])
    set_anchors(client, 1, 0, "2026-01-01T00:00:00Z", 600, "2026-01-01T00:10:00Z")
    set_anchors(client, 2, 0, "2026-01-01T00:00:00Z", 600, "2026-01-01T00:10:00Z")
    body = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:10:00").json()
    assert body["status"] == "undetermined"
    reason = body["reasons"][0]
    assert reason["code"] == "CONFLICTING_READINGS"
    groups = reason["conflicts"]
    assert len(groups) == 1
    assert {p["batch_id"] for p in groups[0]} == {1, 2}
    assert {p["temperature"] for p in groups[0]} == {3.0, 9.0}
    assert "dose" not in body


def test_same_utc_same_temperature_deduped(client):
    upload(client, [(1, 0, 3.0), (1, 600, 4.0)])
    upload(client, [(2, 0, 3.0), (2, 300, 5.0)])  # 与段 1 同刻同温 → 去重
    set_anchors(client, 1, 0, "2026-01-01T00:00:00Z", 600, "2026-01-01T00:10:00Z")
    set_anchors(client, 2, 0, "2026-01-01T00:00:00Z", 600, "2026-01-01T00:10:00Z")
    body = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:10:00",
                    limit="4", allowed="10").json()
    assert body["status"] == "ok"
    assert body["points_used"] == 3
    assert body["dose"] == 3.75  # 75 + 150 °C·s


# ---------------------------------------------------------------- 版本一致性

def test_stale_result_only_for_review(tmp_path, monkeypatch):
    from app import calibration
    from app.main import create_app

    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WORKER_ENABLED", "0")
    with TestClient(create_app()) as c:
        upload(c, [(1, 0, 8.0), (1, 600, 8.0)])
        anchor_seg1(c)
        good = exposure(c, "2026-01-01 00:00:00", "2026-01-01 00:10:00",
                        allowed="100").json()
        assert good["conclusion"] == "release" and good["current"] is True
        v1 = good["calc_version"]
        # 校时未追平：屏蔽重算后上传新点（变更已落库但未重算）
        monkeypatch.setattr(calibration.RecalcWorker, "run_until_quiet",
                            lambda self: None)
        upload(c, [(1, 900, 8.0)])
        body = exposure(c, "2026-01-01 00:00:00", "2026-01-01 00:10:00",
                        allowed="100").json()
        assert body["current"] is False
        assert body["conclusion"] == "stale"      # 旧版剂量仅供回看
        assert body["calc_version"] == v1         # 版号与旧结果一致
        assert body["dose"] == good["dose"]       # 剂量仍对应旧版依据


def test_failed_recalc_not_reported_release(tmp_path, monkeypatch):
    from app import calibration
    from app.main import create_app

    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WORKER_ENABLED", "0")
    with TestClient(create_app()) as c:
        upload(c, [(1, 0, 8.0), (1, 600, 8.0)])
        anchor_seg1(c)
        good = exposure(c, "2026-01-01 00:00:00", "2026-01-01 00:10:00",
                        allowed="100").json()
        assert good["conclusion"] == "release"

        def boom(readings, anchors):
            raise RuntimeError("模拟计算失败")

        monkeypatch.setattr(calibration, "compute_calibration", boom)
        upload(c, [(1, 900, 8.0)])  # 新点被接纳，但重算失败
        assert c.get("/api/status").json()["calc"]["status"] == "failed"
        body = exposure(c, "2026-01-01 00:00:00", "2026-01-01 00:10:00",
                        allowed="100").json()
        assert body["current"] is False
        assert body["conclusion"] == "stale"      # 重算失败不得误报可放行
        assert body["calc_status"] == "failed"
        assert body["dose"] == good["dose"]       # 旧版完整结果仍可回看


def test_version_follows_recalc(client):
    upload(client, [(1, 0, 8.0), (1, 600, 8.0)])
    anchor_seg1(client)
    b1 = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:10:00",
                  allowed="100").json()
    # 更正锚点：整体平移一天，换版后剂量/依据/版号同属新完整结果
    set_anchors(client, 1, 0, "2026-01-02T00:00:00Z", 3600, "2026-01-02T01:00:00Z",
                version=1)
    b2 = exposure(client, "2026-01-02 00:00:00", "2026-01-02 00:10:00",
                  allowed="100").json()
    assert b2["calc_version"] > b1["calc_version"]
    assert b2["status"] == "ok" and b2["current"] is True
    assert b2["dose"] == b1["dose"]  # 平移后同形曲线，剂量一致
    # 旧窗口在新版下无读数 → 无法判定，且版号已是新版
    old = exposure(client, "2026-01-01 00:00:00", "2026-01-01 00:10:00",
                   allowed="100").json()
    assert old["status"] == "undetermined"
    assert old["calc_version"] == b2["calc_version"]


# ---------------------------------------------------------------- 输入校验 / 页面

def test_input_validation(client):
    assert exposure(client, "2026-01-02 00:00:00", "2026-01-01 00:00:00").status_code == 400
    assert exposure(client, "2026-01-01 00:00:00", "2026-01-01 01:00:00",
                    limit="abc").status_code == 400
    assert exposure(client, "2026-01-01 00:00:00", "2026-01-01 01:00:00",
                    allowed="-1").status_code == 400
    assert exposure(client, "2026-01-01 00:00:00", "2026-01-01 01:00:00",
                    max_gap="0").status_code == 400
    assert exposure(client, "", "2026-01-01 01:00:00").status_code == 400
    assert exposure(client, "not-a-time", "2026-01-01 01:00:00").status_code == 400


def test_exposure_page_html(client):
    upload(client, [(1, 0, 8.0), (1, 600, 8.0)])
    anchor_seg1(client)
    r = client.get("/exposure", params={
        "start": "2026-01-01 00:00:00", "end": "2026-01-01 00:10:00",
        "limit": "5", "allowed": "100", "max_gap": "10",
    })
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "可放行" in r.text
    assert "批次号" in r.text and "原行号" in r.text
    assert "越界区间" in r.text
    # 空表单与导航入口
    assert client.get("/exposure").status_code == 200
    assert "/exposure" in client.get("/").text
    # 无法判定页面
    r = client.get("/exposure", params={
        "start": "2025-12-31 00:00:00", "end": "2026-01-01 00:10:00",
        "limit": "5", "allowed": "100", "max_gap": "10",
    })
    assert "无法判定" in r.text

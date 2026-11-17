"""领域服务：上传入库、锚点维护、查询、超温核算、状态汇总。

所有写操作在单事务内完成并递增 state_generation（触发后续重算）。
"""
from __future__ import annotations

import math
from datetime import datetime

from .config import Settings
from .db import Database
from .dose import compute_dose
from .timeutil import fmt_utc, parse_utc, utcnow_iso


class ServiceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: str = "BAD_REQUEST",
        errors: list | None = None,
        current_version: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.errors = errors or []
        self.current_version = current_version


def _bump_generation(cur) -> None:
    cur.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        " WHERE key = 'state_generation'"
    )


# ---------------------------------------------------------------- 上传

def add_batch(db: Database, cfg: Settings, filename: str, rows: list[dict]) -> int:
    """整批入库（原子）：任一点冲突或超上限则整批拒绝。返回批次号。"""
    if not rows:
        raise ServiceError("没有可入库的数据行", code="EMPTY_BATCH")
    with db.tx() as cur:
        total = cur.execute("SELECT COUNT(*) AS c FROM readings").fetchone()["c"]
        if total + len(rows) > cfg.max_points:
            raise ServiceError(
                f"累计点数将超过上限 {cfg.max_points}（现有 {total} 点，本批 {len(rows)} 行），整批已拒绝",
                code="POINT_LIMIT_EXCEEDED",
            )
        conflicts = []
        for r in rows:
            hit = cur.execute(
                "SELECT 1 FROM readings WHERE segment = ? AND local_seconds = ?",
                (r["segment"], r["local_seconds"]),
            ).fetchone()
            if hit:
                conflicts.append({
                    "row": r["row_no"],
                    "message": f"与已有数据重复：段 {r['segment']} 秒数 {r['local_seconds']}",
                })
        if conflicts:
            raise ServiceError(
                f"发现 {len(conflicts)} 条同段秒数重复，整批已拒绝",
                code="DUPLICATE_SECONDS",
                errors=conflicts,
            )
        now = utcnow_iso()
        batch_id = cur.execute(
            "INSERT INTO batches(filename, created_at, rows_accepted) VALUES (?, ?, ?)",
            (filename[:200], now, len(rows)),
        ).lastrowid
        cur.executemany(
            "INSERT INTO readings(batch_id, row_no, segment, local_seconds, temperature, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (batch_id, r["row_no"], r["segment"], r["local_seconds"], r["temperature"], now)
                for r in rows
            ],
        )
        _bump_generation(cur)
    return batch_id


# ---------------------------------------------------------------- 锚点

def set_anchors(
    db: Database,
    segment: int,
    local1: int,
    utc1,                      # datetime
    local2: int,
    utc2,                      # datetime
    version: int | None,       # 所见版本；新建时须为 None
) -> int:
    """录入或更正某段的两枚锚点，返回新版本号。

    约束：0 ≤ local1 < local2 且 utc1 < utc2（UTC 须递增）。
    更正时必须提交所见版本，否则 409。
    """
    if segment < 0:
        raise ServiceError("段号须为非负整数", code="BAD_SEGMENT")
    if not (0 <= local1 < local2):
        raise ServiceError("本地秒数须满足 0 ≤ 秒数1 < 秒数2", code="BAD_LOCAL_RANGE")
    if not (utc1 < utc2):
        raise ServiceError("UTC 必须递增：UTC2 须晚于 UTC1", code="UTC_NOT_INCREASING")

    with db.tx() as cur:
        row = cur.execute(
            "SELECT version FROM anchors WHERE segment = ?", (segment,)
        ).fetchone()
        now = utcnow_iso()
        if row is None:
            if version is not None:
                raise ServiceError(
                    "该段尚无锚点，无法按版本更正（请清空版本字段按新建提交）",
                    status=409, code="VERSION_CONFLICT", current_version=None,
                )
            new_version = 1
            cur.execute(
                "INSERT INTO anchors(segment, local1, utc1, local2, utc2, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (segment, local1, fmt_utc(utc1), local2, fmt_utc(utc2), new_version, now),
            )
        else:
            current = row["version"]
            if version is None or version != current:
                raise ServiceError(
                    f"锚点版本不一致：提交版本 {version}，当前版本 {current}，请刷新后重试",
                    status=409, code="VERSION_CONFLICT", current_version=current,
                )
            new_version = current + 1
            cur.execute(
                "UPDATE anchors SET local1 = ?, utc1 = ?, local2 = ?, utc2 = ?,"
                " version = ?, updated_at = ? WHERE segment = ?",
                (local1, fmt_utc(utc1), local2, fmt_utc(utc2), new_version, now, segment),
            )
        _bump_generation(cur)
    return new_version


def list_anchors(db: Database) -> list[dict]:
    anchors = [dict(a) for a in db.query("SELECT * FROM anchors ORDER BY segment")]
    secs_by_seg: dict[int, list[int]] = {}
    for r in db.query("SELECT segment, local_seconds FROM readings"):
        secs_by_seg.setdefault(r["segment"], []).append(r["local_seconds"])
    for a in anchors:
        secs = secs_by_seg.get(a["segment"], [])
        a["point_count"] = len(secs)
        a["in_range_count"] = sum(1 for s in secs if a["local1"] <= s <= a["local2"])
    return anchors


# ---------------------------------------------------------------- 查询

def calc_info(db: Database) -> dict:
    row = db.query_one("SELECT * FROM calc_state WHERE id = 1")
    meta = {r["key"]: r["value"] for r in db.query("SELECT key, value FROM meta")}
    state_gen = int(meta.get("state_generation", "0"))
    calibrated_gen = int(meta.get("calibrated_generation", "-1"))
    return {
        "version": row["version"],
        "status": row["status"],
        "last_error": row["last_error"],
        "updated_at": row["updated_at"],
        "state_generation": state_gen,
        "calibrated_generation": calibrated_gen,
        "dirty": state_gen != calibrated_gen,
    }


def query_by_date(db: Database, date_str: str) -> dict:
    try:
        day = datetime.strptime((date_str or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ServiceError("日期格式须为 YYYY-MM-DD", code="BAD_DATE")
    info = calc_info(db)
    rows = db.query(
        """
        SELECT c.reading_id, c.utc_time, c.anchor_version, c.calc_version,
               r.batch_id, r.row_no, r.segment, r.local_seconds, r.temperature
        FROM calibrated c JOIN readings r ON r.id = c.reading_id
        WHERE c.utc_date = ?
        ORDER BY c.utc_time, c.reading_id
        """,
        (day.isoformat(),),
    )
    return {
        "date": day.isoformat(),
        "calc_version": info["version"],
        "dirty": info["dirty"],
        "count": len(rows),
        "readings": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------- 超温核算

def _parse_finite(value, label: str) -> float:
    try:
        x = float(str(value).strip())
    except (TypeError, ValueError):
        raise ServiceError(f"{label}须为数值", code="BAD_VALUE")
    if not math.isfinite(x):
        raise ServiceError(f"{label}须为有限数值", code="BAD_VALUE")
    return x


def compute_exposure(
    db: Database,
    start_raw: str,
    end_raw: str,
    limit_raw: str,
    allowed_raw: str,
    max_gap_raw: str,
) -> dict:
    """核算运输时段的超温暴露（度·分钟剂量），决定放行 / 隔离 / 无法判定。

    一致性约定：
    - 读数、校时版本、代次在同一一致快照内读取，剂量、依据与版号始终对应
      同一完整结果；若快照内版本不一致（不应发生），返回无法判定而非拼凑结果。
    - 校时未追平（dirty）或重算失败时，结果标记 current=False、conclusion="stale"：
      旧版剂量仅供回看，不能作为当前放行结论，更不得误报可放行。
    - 无法判定时不含 dose/decision 字段，不得以零剂量放行。
    """
    try:
        start = parse_utc(start_raw)
        end = parse_utc(end_raw)
    except ValueError as exc:
        raise ServiceError(str(exc), code="BAD_TIME")
    if not start < end:
        raise ServiceError("起始时间须早于结束时间", code="BAD_WINDOW")
    limit = _parse_finite(limit_raw, "温度上限")
    allowed = _parse_finite(allowed_raw, "允许剂量")
    if allowed < 0:
        raise ServiceError("允许剂量须为非负数值", code="BAD_VALUE")
    max_gap_min = _parse_finite(max_gap_raw, "相邻读数最大间隔")
    if max_gap_min <= 0:
        raise ServiceError("相邻读数最大间隔须为正数（分钟）", code="BAD_VALUE")

    def fn(conn):
        calc = dict(conn.execute("SELECT * FROM calc_state WHERE id = 1").fetchone())
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        rows = [dict(r) for r in conn.execute(
            """
            SELECT c.reading_id, c.utc_time, c.calc_version,
                   r.batch_id, r.row_no, r.segment, r.local_seconds, r.temperature
            FROM calibrated c JOIN readings r ON r.id = c.reading_id
            ORDER BY c.utc_time, c.reading_id
            """
        )]
        return calc, meta, rows

    calc, meta, rows = db.snapshot(fn)
    state_gen = int(meta.get("state_generation", "0"))
    calibrated_gen = int(meta.get("calibrated_generation", "-1"))
    dirty = state_gen != calibrated_gen
    current = (not dirty) and calc["status"] != "failed"

    points = []
    versions = set()
    for r in rows:
        versions.add(r["calc_version"])
        points.append({
            "reading_id": r["reading_id"],
            "batch_id": r["batch_id"],
            "row_no": r["row_no"],
            "segment": r["segment"],
            "local_seconds": r["local_seconds"],
            "utc_time": r["utc_time"],
            "temperature": r["temperature"],
            "ts": parse_utc(r["utc_time"]).timestamp(),
        })

    # 防御：快照内所有读数须同属当前已发布校时版本，否则不拼凑结果
    version_consistent = versions <= {calc["version"]}
    if version_consistent:
        result = compute_dose(points, start.timestamp(), end.timestamp(),
                              limit, max_gap_min * 60.0, allowed)
    else:
        result = {
            "status": "undetermined",
            "reasons": [{
                "code": "VERSION_MIXED",
                "message": "校时结果版本不一致（换版进行中），无法判定，请稍后重试",
            }],
        }

    result.update({
        "params": {
            "start": fmt_utc(start),
            "end": fmt_utc(end),
            "limit": limit,
            "allowed": allowed,
            "max_gap_minutes": max_gap_min,
        },
        "calc_version": calc["version"],
        "calc_status": calc["status"],
        "dirty": dirty,
        "current": current,
        "computed_at": utcnow_iso(),
    })
    if not current:
        # 校时未追平或重算失败：旧版剂量仅供回看，不能作为当前放行结论
        result["conclusion"] = "stale"
    elif result["status"] != "ok":
        result["conclusion"] = "undetermined"
    else:
        result["conclusion"] = result["decision"]
    return result


def pending_readings(db: Database) -> list[dict]:
    """尚未进入当前校时版本的读数（无锚点或超出锚点范围），即“留存待校时”。"""
    anchors = {a["segment"]: a for a in db.query("SELECT * FROM anchors")}
    out = []
    for r in db.query(
        "SELECT id, batch_id, row_no, segment, local_seconds, temperature"
        " FROM readings ORDER BY id"
    ):
        a = anchors.get(r["segment"])
        if a is None:
            reason = "该段未设置校时锚点"
        elif not (a["local1"] <= r["local_seconds"] <= a["local2"]):
            reason = f"超出锚点范围 [{a['local1']}, {a['local2']}]"
        else:
            continue
        out.append({**dict(r), "reason": reason})
    return out


def get_status(db: Database, cfg: Settings) -> dict:
    accepted = db.query_one("SELECT COUNT(*) AS c FROM readings")["c"]
    batches = [
        dict(b) for b in db.query("SELECT * FROM batches ORDER BY id DESC LIMIT 100")
    ]
    return {
        "points": {"accepted": accepted, "max": cfg.max_points},
        "calc": calc_info(db),
        "anchors": list_anchors(db),
        "batches": batches,
        "pending_count": len(pending_readings(db)),
    }

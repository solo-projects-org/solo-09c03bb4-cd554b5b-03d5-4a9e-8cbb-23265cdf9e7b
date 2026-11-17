"""超温暴露（度·分钟剂量）核算：纯函数，不碰数据库。

输入为同一已发布校时版本下、含 UTC 时间戳的读数点列；输出剂量、越界区间与判定。
无法判定（首尾缺可信读数 / 相邻间隔过大 / 同一 UTC 异温）时明确给出原因，
绝不以零剂量放行。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

from .timeutil import fmt_utc

DOSE_PRECISION = 6  # 剂量（°C·min）保留 6 位小数，判定以此为准，避免浮点尾差影响结论


def _iso(ts: float) -> str:
    return fmt_utc(datetime.fromtimestamp(ts, tz=timezone.utc))


def _ref(p: dict) -> dict:
    """读数溯源信息：批次号、原行号等。"""
    return {
        "reading_id": p["reading_id"],
        "batch_id": p["batch_id"],
        "row_no": p["row_no"],
        "segment": p["segment"],
        "local_seconds": p["local_seconds"],
        "utc_time": p["utc_time"],
        "temperature": p["temperature"],
    }


def _interp(t0: float, v0: float, t1: float, v1: float, t: float) -> float:
    if t1 == t0:
        return v0
    f = (t - t0) / (t1 - t0)
    return v0 + (v1 - v0) * f


def compute_dose(points, start_ts, end_ts, limit, max_gap_s, allowed):
    """核算 [start_ts, end_ts]（UTC 秒）内温度超过 limit 的度·分钟剂量。

    points: 同一校时版本的读数，含 reading_id/batch_id/row_no/segment/
            local_seconds/utc_time/temperature/ts（UTC 秒）。
    返回 dict：
      status="ok"           → 含 dose/decision/window/intervals/points_used
      status="undetermined" → 含 reasons（绝不含 dose/decision）
    """
    pts = sorted(points, key=lambda p: (p["ts"], p["reading_id"]))

    # ---- 时段首尾须各有夹住的可信读数（首：最后一个 ≤ start；尾：第一个 ≥ end）
    t_lo = t_hi = None
    for p in pts:
        if p["ts"] <= start_ts:
            t_lo = p["ts"]  # 递增遍历，最终为 ≤ start 的最大时刻
        if p["ts"] >= end_ts and t_hi is None:
            t_hi = p["ts"]  # 第一个 ≥ end 的时刻
    reasons = []
    if t_lo is None:
        reasons.append({
            "code": "NO_START_READING",
            "message": "时段首端缺少可信读数：窗口开始之时或之前没有已校时读数，无法判定",
        })
    if t_hi is None:
        reasons.append({
            "code": "NO_END_READING",
            "message": "时段尾端缺少可信读数：窗口结束之时或之后没有已校时读数，无法判定",
        })
    if reasons:
        return {"status": "undetermined", "reasons": reasons}

    # 覆盖整个窗口的读数链（含两端夹持点；边界时刻的全部读数都纳入，以便发现异温冲突）
    chain = [p for p in pts if t_lo <= p["ts"] <= t_hi]

    # ---- 同一 UTC 异温 → 无法判定；同温同刻 → 去重为一点
    deduped: list[dict] = []
    conflicts: list[list[dict]] = []
    i = 0
    while i < len(chain):
        group = [chain[i]]
        j = i + 1
        while j < len(chain) and chain[j]["ts"] == chain[i]["ts"]:
            group.append(chain[j])
            j += 1
        if len({p["temperature"] for p in group}) > 1:
            conflicts.append(group)
        else:
            deduped.append(group[0])
        i = j
    if conflicts:
        return {
            "status": "undetermined",
            "reasons": [{
                "code": "CONFLICTING_READINGS",
                "message": f"同一 UTC 出现异温读数（{len(conflicts)} 处），无法判定",
                "conflicts": [[_ref(p) for p in g] for g in conflicts],
            }],
        }

    # ---- 相邻读数间隔超限 → 无法判定（插值不可信）
    gaps = []
    for a, b in zip(deduped, deduped[1:]):
        gap = b["ts"] - a["ts"]
        if gap > max_gap_s:
            gaps.append({"from": _ref(a), "to": _ref(b), "gap_seconds": round(gap, 3)})
    if gaps:
        return {
            "status": "undetermined",
            "reasons": [{
                "code": "GAP_TOO_LARGE",
                "message": f"相邻读数间隔超过上限（{len(gaps)} 处），无法判定",
                "gaps": gaps,
            }],
        }

    # ---- 有效点列：窗口首（按夹持点对线性插值）+ 窗口内记录点 + 窗口尾（插值）
    # deduped[0].ts <= start_ts < deduped[1].ts；deduped[-2].ts < end_ts <= deduped[-1].ts
    eff: list[tuple[float, float, dict | None]] = []  # (ts, temp, 记录点|None=插值点)
    first = deduped[0]
    if first["ts"] == start_ts:
        eff.append((start_ts, first["temperature"], first))
    else:
        nxt = deduped[1]
        eff.append((start_ts, _interp(first["ts"], first["temperature"],
                                      nxt["ts"], nxt["temperature"], start_ts), None))
    for p in deduped[1:-1]:
        eff.append((p["ts"], p["temperature"], p))
    last = deduped[-1]
    if last["ts"] == end_ts:
        eff.append((end_ts, last["temperature"], last))
    else:
        prev = deduped[-2]
        eff.append((end_ts, _interp(prev["ts"], prev["temperature"],
                                    last["ts"], last["temperature"], end_ts), None))

    # ---- 按相邻点线性插值累计超限面积（°C·秒），跨日连续（时间轴为绝对 UTC）
    dose_s = 0.0
    subs: list[list[float]] = []  # [s, e, area] 越界子区间
    for (t0, T0, _), (t1, T1, _) in zip(eff, eff[1:]):
        h0, h1 = T0 - limit, T1 - limit
        if h0 <= 0 and h1 <= 0:
            continue
        dt = t1 - t0
        if h0 > 0 and h1 > 0:                # 整段超限：梯形
            s, e, area = t0, t1, (h0 + h1) / 2 * dt
        elif h0 > 0:                         # 段内回落至上限：三角形
            f = h0 / (h0 - h1)
            s, e, area = t0, t0 + f * dt, h0 / 2 * f * dt
        else:                                # 段内升破上限：三角形
            f = h0 / (h0 - h1)
            s, e, area = t0 + f * dt, t1, h1 / 2 * (1 - f) * dt
        dose_s += area
        subs.append([s, e, area])

    # 合并相邻子区间为最大越界区间（曲线连续越限时相邻子区间首尾相接）
    merged: list[list[float]] = []
    for s, e, area in subs:
        if merged and merged[-1][1] == s:
            merged[-1][1] = e
            merged[-1][2] += area
        else:
            merged.append([s, e, area])

    chain_ts = [p["ts"] for p in deduped]
    intervals = []
    for s, e, area in merged:
        inside = [(t, T, r) for (t, T, r) in eff if s <= t <= e]
        peak_t, peak_T = max(((t, T) for t, T, _ in inside), key=lambda x: (x[1], -x[0]))
        exceed = [_ref(r) for t, T, r in inside if r is not None and T > limit]
        i0 = bisect_right(chain_ts, s) - 1   # 区间首端读数：最后一个 ≤ s 的记录点
        i1 = bisect_left(chain_ts, e)        # 区间尾端读数：第一个 ≥ e 的记录点
        intervals.append({
            "start_utc": _iso(s),
            "end_utc": _iso(e),
            "dose": round(area / 60.0, DOSE_PRECISION),
            "peak_temperature": peak_T,
            "peak_utc": _iso(peak_t),
            "start_reading": _ref(deduped[i0]),
            "end_reading": _ref(deduped[i1]),
            "exceed_readings": exceed,
        })

    dose_min = round(dose_s / 60.0, DOSE_PRECISION)
    after_start = first if first["ts"] == start_ts else deduped[1]
    before_end = last if last["ts"] == end_ts else deduped[-2]
    return {
        "status": "ok",
        "dose": dose_min,
        "decision": "release" if dose_min <= allowed else "quarantine",
        "points_used": len(deduped),
        "window": {  # 夹住时段首尾的读数（插值依据）
            "start": {"before": _ref(first), "after": _ref(after_start)},
            "end": {"before": _ref(before_end), "after": _ref(last)},
        },
        "intervals": intervals,
    }

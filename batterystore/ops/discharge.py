"""限额放电编排。

顺序固定为「并网限额钳位 → 频率窗口校验 → 爬坡 → 电量累计 → 配额记账」。三处
独立判断不能颠倒：钳位只计算本轮允许目标，频率拒收时不下发新目标，PCS 保持
上一个有效功率；只有频率在窗口内，才允许向新目标爬坡。
"""

from __future__ import annotations

from ..errors import StoreError


def discharge_rack(
    pcs,
    grid,
    quota,
    soc,
    rack,
    station: str,
    *,
    power_kw: float,
    dt: float,
    now: float,
    frequency_hz: float = 50.0,
    clusters: dict[str, float] | None = None,
) -> dict:
    """执行一次放电编排，返回带步骤与违约项的报告。"""

    flow: list[dict] = []
    violations: list[str] = []

    if pcs.cut.is_tripped():
        return {"station": station, "ok": False, "steps": flow, "violations": ["pcs_tripped"]}

    if not pcs.is_tied():
        pcs.transfer("off_grid", "discharge_request")
        pcs.transfer("grid_tie", "discharge_request")

    if clusters:
        requested_clusters = {str(name): abs(float(value_kw)) for name, value_kw in clusters.items()}
        requested_total = sum(requested_clusters.values())
        clamp_action = "clamp_clusters"
        clamped = grid.clamp_clusters(requested_clusters)
        allowed = clamped["allowed_total_kw"]
        grid_ratio = clamped["load_ratio"]
    else:
        requested_total = abs(float(power_kw))
        clamp_action = "clamp_before_ramp"
        clamped = grid.clamp_for_ramp(requested_total)
        allowed = clamped["allowed_kw"]
        grid_ratio = clamped["ratio"]
    flow.append({"component": "grid", "action": clamp_action, "detail": clamped})

    held_kw = pcs.current_power_kw()
    evidence = grid.frequency_gate(
        frequency_hz,
        requested_total,
        allowed,
        now,
        held_kw=held_kw,
    )
    flow.append({"component": "grid", "action": "frequency_gate", "detail": evidence})
    if not evidence["ok"]:
        violations.append("frequency_out_of_band")
        return {
            "station": station,
            "ok": False,
            "steps": flow,
            "violations": violations,
            "held_kw": round(held_kw, 3),
            "last_rejection_ts": evidence["last_rejection_ts"],
            "rejection_count": evidence["rejection_count"],
        }

    derate = pcs.apply_derate(grid=grid_ratio, temperature=1.0)
    point = pcs.set_point(allowed, dt, derate)
    flow.append({"component": "pcs", "action": "ramp", "detail": point})
    if point["violations"]:
        violations.extend(point["violations"])

    energy_kwh = abs(point["ramped_kw"]) * dt / 3600.0
    arc = grid.record_flow(point["ramped_kw"], dt)
    flow.append({"component": "grid", "action": "energy", "detail": arc})
    try:
        quota.record_discharge(energy_kwh, now)
    except StoreError as error:
        violations.append("quota_exceeded")
        flow.append({"component": "quota", "action": "discharge_rejected", "detail": error.as_record()})
        return {"station": station, "ok": False, "steps": flow, "violations": violations}

    delta_ah = soc.accumulate(-abs(point["ramped_kw"]) * 1000.0 / max(rack.voltage_v(), 1e-9), dt)
    estimate = soc.value(now)
    rack.set_soc(estimate.soc_pct)
    flow.append(
        {
            "component": "soc",
            "action": "estimate",
            "detail": {**estimate.as_dict(), "delta_ah": round(delta_ah, 6)},
        }
    )
    result = {"station": station, "ok": not violations, "steps": flow, "violations": violations}
    if "last_rejection_ts" in evidence:
        result["last_rejection_ts"] = evidence["last_rejection_ts"]
    return result

"""限额放电编排。

顺序固定为「并网限额钳位 → 频率窗口校验 → 爬坡 → 电量累计 → 配额记账」。
钳位、频率校验、爬坡是三处独立判断，执行顺序不能颠倒：

1. **先钳位**：带限功率参数的放电指令必须先按并网限额压住，允许功率算
   出来但不执行——如果先爬坡再钳位，并网点会在一瞬间越限。
2. **再验频**：频率不在窗口内当场拒收，不进爬坡、不下任何功率指令，PCS
   保持在上一个有效功率（拒收证据连同保持功率一起写进拒收簿），不会
   停在指令值与旧值之间的半路上。
3. **后爬坡**：两条判断都过了才按允许功率推进一步爬坡。
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
    op_id: str | None = None,
) -> dict:
    """执行一次放电编排，返回带步骤与违约项的报告。"""

    flow: list[dict] = []
    violations: list[str] = []

    if pcs.cut.is_tripped():
        return {"station": station, "ok": False, "steps": flow, "violations": ["pcs_tripped"]}

    # 判断一：并网限额钳位。只算允许功率，不动 PCS。
    requested_kw = abs(float(power_kw))
    clamped = grid.clamp_for_ramp(requested_kw)
    flow.append({"component": "grid", "action": "clamp_before_ramp", "detail": clamped})

    # 判断二：频率窗口校验。越窗即拒收，保持上一个有效功率，不进爬坡。
    admission = grid.frequency_admission(frequency_hz, now)
    flow.append({"component": "grid", "action": "frequency_gate", "detail": admission})
    if not admission["admitted"]:
        held_kw = pcs.snapshot().ramp_kw
        evidence = grid.reject_discharge(
            station=station,
            op_id=op_id,
            requested_kw=clamped["requested_kw"],
            allowed_kw=clamped["allowed_kw"],
            admission=admission,
            held_kw=held_kw,
            now=now,
        )
        flow.append({"component": "grid", "action": "discharge_rejected", "detail": evidence})
        return {
            "station": station,
            "ok": False,
            "steps": flow,
            "violations": ["frequency_out_of_band"],
            "held_kw": round(held_kw, 3),
            "last_rejection": evidence,
        }

    # 并网态在频率放行之后才建立，避免向越窗电网并接触器；它不属于三处
    # 独立判断，夹在校验与爬坡之间不改变判断顺序。
    if not pcs.is_tied():
        pcs.transfer("off_grid", "discharge_request")
        pcs.transfer("grid_tie", "discharge_request")

    # 判断三：爬坡。前两条都过了才下发并推进一步。
    derate = pcs.apply_derate(grid=clamped["ratio"], temperature=1.0)
    point = pcs.set_point(clamped["allowed_kw"], dt, derate)
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
    return {"station": station, "ok": not violations, "steps": flow, "violations": violations}

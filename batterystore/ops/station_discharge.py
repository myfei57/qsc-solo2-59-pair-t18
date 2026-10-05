"""全站簇群限额放电编排。

在单簇 :func:`~batterystore.ops.discharge.discharge_rack` 的三处独立判断之
外，多簇场景还要先回答"总功率超并网点限额时，各簇分别允许放多少"。
顺序同样写死：

1. **簇群缩功**：把各簇请求功率按选定策略（默认按比例一起缩；需要保关键
   簇时按优先级一簇一簇压）分到并网点上网限额内，缩功结果只决定各簇
   允许功率与 PCS 总目标，不产生任何输出。
2. **钳位 → 频率校验 → 爬坡**：总目标按并网限额钳位（缩功已保证不越限），
   频率越窗时整站拒收，PCS 不爬坡，所有簇保持在上一个有效功率。
3. **电量累计 → 配额记账 → SOC**：以实际爬坡后的总功率记账，按各簇
   允许份额折算簇 SOC。
"""

from __future__ import annotations

from ..errors import StoreError


def discharge_station(
    pcs,
    grid,
    quota,
    soc,
    racks,
    station: str,
    *,
    requested: dict[str, float],
    dt: float,
    now: float,
    frequency_hz: float = 50.0,
    strategy: str = "proportional",
    priorities: dict[str, int] | None = None,
    op_id: str | None = None,
) -> dict:
    """执行一次全站簇群放电编排，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []

    if pcs.cut.is_tripped():
        return {"station": station, "ok": False, "steps": steps, "violations": ["pcs_tripped"]}

    missing = sorted(rack_id for rack_id in requested if rack_id not in racks)
    if missing:
        raise KeyError(f"未知的簇: {', '.join(missing)}")

    # 第一步：簇群缩功，把各簇请求压到并网点上网限额内，不动 PCS。
    allocation = grid.curtail({rack_id: abs(float(kw)) for rack_id, kw in requested.items()}, strategy, priorities)
    steps.append({"component": "grid", "action": "cluster_curtail", "detail": allocation})

    # 第二步上半：钳位（缩功后的总额仍过一次并网限额，两处独立判断不合并）。
    clamped = grid.clamp_for_ramp(allocation["allowed_total_kw"])
    steps.append({"component": "grid", "action": "clamp_before_ramp", "detail": clamped})

    # 第二步中段：频率窗口校验。越窗即整站拒收，保持上一个有效功率。
    admission = grid.frequency_admission(frequency_hz, now)
    steps.append({"component": "grid", "action": "frequency_gate", "detail": admission})
    if not admission["admitted"]:
        held_kw = pcs.snapshot().ramp_kw
        evidence = grid.reject_discharge(
            station=station,
            op_id=op_id,
            requested_kw=allocation["requested_total_kw"],
            allowed_kw=clamped["allowed_kw"],
            admission=admission,
            held_kw=held_kw,
            now=now,
        )
        steps.append({"component": "grid", "action": "discharge_rejected", "detail": evidence})
        return {
            "station": station,
            "ok": False,
            "steps": steps,
            "violations": ["frequency_out_of_band"],
            "held_kw": round(held_kw, 3),
            "last_rejection": evidence,
            "allocation": allocation,
        }

    if not pcs.is_tied():
        pcs.transfer("off_grid", "discharge_request")
        pcs.transfer("grid_tie", "discharge_request")

    # 第二步收尾：爬坡。PCS 输出的是全站总额，按一步速率逼近允许总额。
    derate = pcs.apply_derate(grid=clamped["ratio"], temperature=1.0)
    point = pcs.set_point(clamped["allowed_kw"], dt, derate)
    steps.append({"component": "pcs", "action": "ramp", "detail": point})
    if point["violations"]:
        violations.extend(point["violations"])

    energy_kwh = abs(point["ramped_kw"]) * dt / 3600.0
    arc = grid.record_flow(point["ramped_kw"], dt)
    steps.append({"component": "grid", "action": "energy", "detail": arc})
    try:
        quota.record_discharge(energy_kwh, now)
    except StoreError as error:
        violations.append("quota_exceeded")
        steps.append({"component": "quota", "action": "discharge_rejected", "detail": error.as_record()})
        return {"station": station, "ok": False, "steps": steps, "violations": violations}

    # 实际爬坡总额按缩功份额折算到各簇 SOC。
    shares = allocation["clusters"]
    scale = (
        point["ramped_kw"] / allocation["allowed_total_kw"]
        if allocation["allowed_total_kw"] > 1e-9
        else 0.0
    )
    for rack_id in sorted(shares):
        rack = racks[rack_id]
        delivered = shares[rack_id]["allowed_kw"] * scale
        soc.accumulate(-delivered * 1000.0 / max(rack.voltage_v(), 1e-9), dt)
    estimate = soc.value(now)
    for rack_id in sorted(shares):
        racks[rack_id].set_soc(estimate.soc_pct)
    steps.append({"component": "soc", "action": "estimate", "detail": estimate.as_dict()})
    return {
        "station": station,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "allocation": allocation,
        "ramped_total_kw": round(point["ramped_kw"], 3),
    }

"""放电频率拒收、拒收留痕与重试追溯。"""

from __future__ import annotations

from support import bring_online, connect_grid


def test_discharge_rejected_when_frequency_out_of_band(platform):
    bring_online(platform)
    connect_grid(platform, power_kw=120.0)
    # 先在窗口内放一次电，建立"上一个有效功率"。
    accepted = platform.discharge(power_kw=200.0, dt=1.0, frequency_hz=50.0)
    assert accepted["ok"], accepted["violations"]
    held_before = platform.pcs_snapshot()["ramp_kw"]

    report = platform.discharge(power_kw=400.0, dt=1.0, frequency_hz=51.0)
    assert not report["ok"]
    assert report["violations"] == ["frequency_out_of_band"]
    actions = [step["action"] for step in report["steps"]]
    assert "ramp" not in actions
    assert actions[-1] == "discharge_rejected"
    # 拒收后保持在上一个有效功率，不停在半路。
    assert report["held_kw"] == held_before
    assert platform.pcs_snapshot()["ramp_kw"] == held_before


def test_rejection_evidence_is_kept_and_queryable(platform):
    bring_online(platform)
    connect_grid(platform, power_kw=120.0)
    platform.discharge(power_kw=200.0, dt=1.0, frequency_hz=50.0)
    held = platform.pcs_snapshot()["ramp_kw"]

    rejected = platform.discharge(power_kw=500.0, dt=1.0, frequency_hz=50.6)
    evidence = rejected["last_rejection"]
    assert evidence["reason"] == "frequency_out_of_band"
    assert evidence["requested_kw"] == 500.0
    # 钳位先于校验发生，证据里带的是限额压住后的允许功率。
    assert evidence["allowed_kw"] == platform.config.grid.export_limit_kw
    assert evidence["frequency_hz"] == 50.6
    assert evidence["held_kw"] == held
    assert evidence["op_id"] == rejected["op_id"]

    history = platform.discharge_rejections()
    assert history["total"] == 1
    assert history["last"]["seq"] == 1
    assert history["rejections"][0]["op_id"] == rejected["op_id"]
    snapshot = platform.grid_snapshot()
    assert snapshot["last_rejection"]["frequency_hz"] == 50.6
    assert snapshot["rejections"] >= 1


def test_retry_can_trace_previous_rejection(platform):
    bring_online(platform)
    connect_grid(platform, power_kw=120.0)
    platform.discharge(power_kw=200.0, dt=1.0, frequency_hz=50.0)

    first = platform.discharge(power_kw=200.0, dt=1.0, frequency_hz=51.0)
    first_ev = first["last_rejection"]
    assert first_ev["prev_ts"] is None
    assert first_ev["since_prev_s"] is None

    platform.clock.advance(30.0)
    second = platform.discharge(power_kw=200.0, dt=1.0, frequency_hz=51.0)
    second_ev = second["last_rejection"]
    assert second_ev["seq"] == first_ev["seq"] + 1
    assert second_ev["prev_ts"] == first_ev["ts"]
    assert second_ev["since_prev_s"] == 30.0


def test_in_band_discharge_after_rejection_resumes_from_held_power(platform):
    bring_online(platform)
    connect_grid(platform, power_kw=120.0)
    platform.discharge(power_kw=400.0, dt=1.0, frequency_hz=50.0)
    for _ in range(20):
        platform.clock.advance(1.0)
        platform.discharge(power_kw=400.0, dt=1.0, frequency_hz=50.0)
    held = platform.pcs_snapshot()["ramp_kw"]
    assert held == platform.config.grid.export_limit_kw

    rejected = platform.discharge(power_kw=400.0, dt=1.0, frequency_hz=49.5)
    assert not rejected["ok"]
    assert platform.pcs_snapshot()["ramp_kw"] == held

    platform.clock.advance(2.0)
    resumed = platform.discharge(power_kw=400.0, dt=1.0, frequency_hz=50.0)
    assert resumed["ok"], resumed["violations"]
    assert [step["action"] for step in resumed["steps"]].index("ramp") > 0


def test_clamp_frequency_ramp_order_is_fixed(platform):
    bring_online(platform)
    connect_grid(platform, power_kw=120.0)
    report = platform.discharge(power_kw=900.0, dt=1.0, frequency_hz=50.0)
    actions = [step["action"] for step in report["steps"]]
    assert actions.index("clamp_before_ramp") < actions.index("frequency_gate")
    assert actions.index("frequency_gate") < actions.index("ramp")
    # 限额先把 900kW 压到 400kW，爬坡目标不会越过并网点限额。
    clamp = report["steps"][0]["detail"]
    assert clamp["clamped"] is True
    assert clamp["allowed_kw"] == platform.config.grid.export_limit_kw
    ramp = next(step["detail"] for step in report["steps"] if step["action"] == "ramp")
    assert ramp["requested_kw"] == platform.config.grid.export_limit_kw

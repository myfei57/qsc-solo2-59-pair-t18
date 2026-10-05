"""放电指令的限额、频率门与越限收缩。"""

from __future__ import annotations

from batterystore.app import build_platform
from batterystore.config import default_config
from batterystore.grid.curtail import proportional_curtail
from support import bring_online, connect_grid


def test_proportional_curtail_scales_every_cluster_together():
    plan = proportional_curtail({"rk-01": 300.0, "rk-02": 500.0}, 400.0)
    assert plan["strategy"] == "proportional"
    assert plan["requested_total_kw"] == 800.0
    assert plan["allowed_total_kw"] == 400.0
    assert plan["ratio"] == 0.5
    assert plan["load_ratio"] == 1.0
    assert plan["clusters"] == {"rk-01": 150.0, "rk-02": 250.0}


def test_discharge_clamps_before_frequency_gate_and_ramp(platform, clock):
    bring_online(platform)
    connect_grid(platform, power_kw=0.0)
    clock.advance(1.0)

    report = platform.discharge(power_kw=900.0, dt=1.0, frequency_hz=51.0)

    assert report["ok"] is False
    assert report["violations"] == ["frequency_out_of_band"]
    actions = [step["action"] for step in report["steps"]]
    assert actions == ["clamp_before_ramp", "frequency_gate"]
    gate = report["steps"][1]["detail"]
    assert gate["allowed_kw"] == platform.config.grid.export_limit_kw
    assert gate["held_kw"] == 0.0
    assert gate["previous_rejection_ts"] is None
    assert gate["last_rejection_ts"] == clock.now()
    assert platform.pcs_snapshot()["ramp_kw"] == 0.0
    assert platform.pcs_snapshot()["ramp_steps"] == 1


def test_frequency_rejection_holds_last_valid_power_and_links_retry(platform, clock):
    bring_online(platform)
    connect_grid(platform)
    clock.advance(1.0)
    accepted = platform.discharge(power_kw=300.0, dt=1.0, frequency_hz=50.0)
    assert accepted["ok"] is True
    held_kw = platform.pcs_snapshot()["ramp_kw"]
    assert held_kw > 0.0

    clock.advance(10.0)
    rejected = platform.discharge(power_kw=900.0, dt=1.0, frequency_hz=51.0)
    assert rejected["ok"] is False
    assert rejected["held_kw"] == held_kw
    assert platform.pcs_snapshot()["ramp_kw"] == held_kw
    assert platform.grid_snapshot()["rejections"] == 1
    assert platform.grid_snapshot()["last_rejection"]["rejection_count"] == 1

    clock.advance(10.0)
    retry = platform.discharge(power_kw=300.0, dt=1.0, frequency_hz=50.0)
    gate = next(step["detail"] for step in retry["steps"] if step["action"] == "frequency_gate")
    assert retry["ok"] is True
    assert gate["ok"] is True
    assert gate["last_rejection_ts"] == rejected["last_rejection_ts"]
    evidence = next(
        item
        for item in platform.audit.query(component="grid", action="frequency_gate", limit=10)
        if item["payload"].get("rejected") is True
    )
    assert evidence["payload"]["allowed_kw"] == platform.config.grid.export_limit_kw
    assert evidence["payload"]["held_kw"] == held_kw
    assert evidence["severity"] == "alarm"
    assert platform.grid_snapshot()["rejections"] == 1


def test_discharge_cluster_map_uses_proportional_curtailment(platform, clock):
    bring_online(platform)
    connect_grid(platform)
    clock.advance(1.0)

    report = platform.discharge(
        clusters={"rk-01": 300.0, "rk-02": 500.0},
        dt=1.0,
        frequency_hz=50.0,
    )

    clamp = next(step["detail"] for step in report["steps"] if step["action"] == "clamp_clusters")
    assert clamp["strategy"] == "proportional"
    assert clamp["clusters"] == {"rk-01": 150.0, "rk-02": 250.0}
    assert [step["action"] for step in report["steps"][:3]] == ["clamp_clusters", "frequency_gate", "ramp"]


def test_frequency_rejection_evidence_survives_restart(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage, rack_count=1)
    bring_online(first)
    connect_grid(first)
    rejected = first.discharge(power_kw=300.0, dt=1.0, frequency_hz=51.0)
    assert rejected["ok"] is False

    clock.advance(5.0)
    second = build_platform(default_config(), clock=clock, storage_root=storage, rack_count=1)
    evidence = second.grid.last_frequency_rejection()
    assert evidence is not None
    assert evidence["sample_ts"] == rejected["last_rejection_ts"]
    assert second.grid_snapshot()["rejections"] == 1

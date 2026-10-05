"""簇群越限缩功：按比例一起缩 vs 按优先级一簇一簇压。"""

from __future__ import annotations

import pytest

from batterystore.grid.curtail import PRIORITY, PROPORTIONAL, CurtailPlanner
from support import bring_online, connect_grid


def test_proportional_scales_every_cluster_by_same_ratio():
    plan = CurtailPlanner(PROPORTIONAL).plan({"rk-01": 300.0, "rk-02": 300.0}, 400.0)
    assert plan["curtailed"] is True
    assert plan["strategy"] == PROPORTIONAL
    assert plan["allowed_total_kw"] == 400.0
    assert plan["clusters"]["rk-01"]["allowed_kw"] == 200.0
    assert plan["clusters"]["rk-02"]["allowed_kw"] == 200.0
    # 公平：两簇同比例，没有整簇退出。
    assert plan["clusters"]["rk-01"]["ratio"] == plan["clusters"]["rk-02"]["ratio"]
    assert not any(item["zeroed"] for item in plan["clusters"].values())


def test_priority_drops_low_priority_clusters_and_keeps_critical():
    priorities = {"rk-01": 0, "rk-02": 5}
    plan = CurtailPlanner(PRIORITY).plan({"rk-01": 300.0, "rk-02": 300.0}, 400.0, priorities)
    assert plan["curtailed"] is True
    # 关键簇 rk-01 不被动，低优先级 rk-02 先压。
    assert plan["clusters"]["rk-01"]["allowed_kw"] == 300.0
    assert plan["clusters"]["rk-02"]["allowed_kw"] == 100.0
    assert plan["clusters"]["rk-02"]["cut_kw"] == 200.0

    deeper = CurtailPlanner(PRIORITY).plan({"rk-01": 300.0, "rk-02": 300.0}, 200.0, priorities)
    assert deeper["clusters"]["rk-01"]["allowed_kw"] == 200.0
    assert deeper["clusters"]["rk-02"]["allowed_kw"] == 0.0
    assert deeper["clusters"]["rk-02"]["zeroed"] is True


def test_priority_tie_breaks_deterministically_by_rack_id():
    first = CurtailPlanner(PRIORITY).plan({"rk-01": 300.0, "rk-02": 300.0}, 400.0)
    second = CurtailPlanner(PRIORITY).plan({"rk-02": 300.0, "rk-01": 300.0}, 400.0)
    assert first["clusters"] == second["clusters"]
    # 优先级相同按簇编号，rk-01 先被压。
    assert first["clusters"]["rk-01"]["allowed_kw"] == 100.0
    assert first["clusters"]["rk-02"]["allowed_kw"] == 300.0


def test_request_within_limit_is_not_curtailed():
    plan = CurtailPlanner(PROPORTIONAL).plan({"rk-01": 100.0, "rk-02": 100.0}, 400.0)
    assert plan["curtailed"] is False
    assert plan["clusters"]["rk-01"]["allowed_kw"] == 100.0


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError):
        CurtailPlanner("round_robin")


def test_grid_curtail_uses_export_limit(platform):
    result = platform.grid.curtail({"rk-01": 300.0, "rk-02": 300.0}, PROPORTIONAL)
    assert result["allowed_total_kw"] == platform.config.grid.export_limit_kw
    assert result["clusters"]["rk-01"]["allowed_kw"] == 200.0


def test_station_discharge_curtails_before_ramp(platform):
    bring_online(platform)
    connect_grid(platform, power_kw=120.0)
    report = platform.discharge_station(
        {"rk-01": 300.0, "rk-02": 300.0},
        dt=1.0,
        frequency_hz=50.0,
        strategy=PROPORTIONAL,
    )
    assert report["ok"], report["violations"]
    actions = [step["action"] for step in report["steps"]]
    assert actions.index("cluster_curtail") < actions.index("clamp_before_ramp")
    assert actions.index("clamp_before_ramp") < actions.index("frequency_gate")
    assert actions.index("frequency_gate") < actions.index("ramp")
    # 全站爬坡总额不得越过并网点限额。
    assert report["ramped_total_kw"] <= platform.config.grid.export_limit_kw + 1e-9


def test_station_discharge_priority_preserves_critical_cluster(platform):
    bring_online(platform)
    connect_grid(platform, power_kw=120.0)
    report = platform.discharge_station(
        {"rk-01": 300.0, "rk-02": 300.0},
        dt=1.0,
        frequency_hz=50.0,
        strategy=PRIORITY,
        priorities={"rk-01": 0, "rk-02": 9},
    )
    assert report["ok"], report["violations"]
    clusters = report["allocation"]["clusters"]
    assert clusters["rk-01"]["allowed_kw"] == 300.0
    assert clusters["rk-02"]["allowed_kw"] == 100.0


def test_station_discharge_frequency_rejection_holds_last_power(platform):
    bring_online(platform)
    connect_grid(platform, power_kw=120.0)
    platform.discharge_station({"rk-01": 200.0, "rk-02": 200.0}, dt=1.0, frequency_hz=50.0)
    held = platform.pcs_snapshot()["ramp_kw"]

    report = platform.discharge_station(
        {"rk-01": 200.0, "rk-02": 200.0},
        dt=1.0,
        frequency_hz=51.0,
    )
    assert not report["ok"]
    assert report["violations"] == ["frequency_out_of_band"]
    assert "ramp" not in [step["action"] for step in report["steps"]]
    assert report["held_kw"] == held
    assert platform.pcs_snapshot()["ramp_kw"] == held
    assert report["last_rejection"]["requested_kw"] == 400.0

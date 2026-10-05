"""并网点组件。

``GridTie`` 把限额、频率与电量流向收在一起，对外给三个独立判断：这个功率
指令要不要先钳位、这次并网频率成不成立、各簇越限后怎么缩功。钳位、频率
校验、爬坡是三处独立判断，放电编排里调用顺序写死成「钳位 → 频率校验 →
爬坡」，任何一条不成立都在爬坡之前返回，并网点不会出现"先越限再回调"
或"带着越窗频率往上爬"的瞬间。频率越窗的指令当场拒收并把证据留在拒收
簿里，重试时可追溯上一次拒收时刻。
"""

from __future__ import annotations

from typing import Mapping

from ..config import GridLimits
from .curtail import CurtailPlanner
from .flow import PowerFlow
from .limit import PowerLimit
from .monitor import FrequencyMonitor
from .rejections import RejectionLedger
from .snapshot import GridSnapshot


class GridTie:
    """并网点组件。"""

    def __init__(self, limits: GridLimits) -> None:
        self._limits = limits
        self._limit = PowerLimit(limits)
        self._flow = PowerFlow()
        self._monitor = FrequencyMonitor(limits)
        self._rejections_book = RejectionLedger()
        self._checks = 0
        self._rejections = 0

    @property
    def limits(self) -> GridLimits:
        return self._limits

    @property
    def limit(self) -> PowerLimit:
        return self._limit

    @property
    def flow(self) -> PowerFlow:
        return self._flow

    @property
    def rejections_book(self) -> RejectionLedger:
        return self._rejections_book

    def monitor_frequency(self, frequency_hz: float, now: float) -> dict:
        """记一次频率采样。"""

        return self._monitor.sample(frequency_hz, now)

    def frequency_admission(self, frequency_hz: float, now: float) -> dict:
        """频率窗口校验（独立于钳位），返回是否放行与证据。"""

        sample = self.monitor_frequency(frequency_hz, now)
        return {
            "frequency_hz": sample["frequency_hz"],
            "deviation_hz": sample["deviation_hz"],
            "band_hz": self._limits.frequency_band_hz,
            "ts": sample["ts"],
            "admitted": sample["ok"],
        }

    def reject_discharge(
        self,
        *,
        station: str | None,
        op_id: str | None,
        requested_kw: float,
        allowed_kw: float,
        admission: dict,
        held_kw: float,
        now: float,
    ) -> dict:
        """把一次放电拒收连证据一起记入拒收簿。"""

        self._rejections += 1
        evidence = {
            "station": station,
            "op_id": op_id,
            "reason": "frequency_out_of_band",
            "requested_kw": requested_kw,
            "allowed_kw": allowed_kw,
            "frequency_hz": admission["frequency_hz"],
            "deviation_hz": admission["deviation_hz"],
            "held_kw": held_kw,
            "ts": float(now),
        }
        return self._rejections_book.record(evidence)

    def last_rejection(self) -> dict | None:
        """返回最近一次放电拒收证据，供重试方追溯上一次被拒时刻。"""

        return self._rejections_book.last()

    def rejection_history(self, limit: int = 20) -> list[dict]:
        """返回最近的放电拒收证据。"""

        return self._rejections_book.recent(limit)

    def curtail(
        self,
        requested: Mapping[str, float],
        strategy: str = "proportional",
        priorities: Mapping[str, int] | None = None,
    ) -> dict:
        """按总上网限额与所选策略，把各簇请求功率缩到允许值。"""

        requested_total = sum(abs(float(value)) for value in requested.values())
        allowed_total = self._limit.clamp(requested_total)
        plan = CurtailPlanner(strategy).plan(requested, allowed_total, priorities)
        return {"limit": self._limit.as_dict(), **plan}

    def check(self, frequency_hz: float, power_kw: float, now: float) -> dict:
        """核对并网条件与功率越限，返回判定结果。"""

        self._checks += 1
        sample = self.monitor_frequency(frequency_hz, now)
        violations: list[str] = []
        if not sample["ok"]:
            violations.append("frequency_out_of_band")
        allowed = self._limit.clamp(power_kw)
        if abs(allowed - float(power_kw)) > 1e-9:
            violations.append("power_clamped")
        if violations and "frequency_out_of_band" in violations:
            self._rejections += 1
        return {
            "requested_kw": round(float(power_kw), 3),
            "allowed_kw": round(allowed, 3),
            "headroom_kw": round(self._limit.headroom_kw(allowed), 3),
            "ratio": round(self._limit.ratio(allowed), 4),
            "frequency_hz": sample["frequency_hz"],
            "violations": violations,
        }

    def clamp_for_ramp(self, power_kw: float) -> dict:
        """在爬坡之前钳位，返回钳位记录。"""

        allowed = self._limit.clamp(power_kw)
        return {
            "requested_kw": round(float(power_kw), 3),
            "allowed_kw": round(allowed, 3),
            "clamped": abs(allowed - float(power_kw)) > 1e-9,
            "ratio": round(self._limit.ratio(allowed), 4),
        }

    def record_flow(self, power_kw: float, dt: float) -> dict:
        """按功率方向累计电量。"""

        if float(power_kw) >= 0:
            energy = self._flow.discharge(power_kw, dt)
            direction = "export"
        else:
            energy = self._flow.charge(power_kw, dt)
            direction = "import"
        return {"direction": direction, "energy_kwh": round(energy, 6), "totals": self._flow.totals()}

    def snapshot(self) -> GridSnapshot:
        """返回并网点状态快照。"""

        return GridSnapshot(
            limit=self._limit.as_dict(),
            flow=self._flow.totals(),
            frequency=self._monitor.snapshot(),
            checks=self._checks,
            rejections=self._rejections,
            last_rejection=self._rejections_book.last(),
        )

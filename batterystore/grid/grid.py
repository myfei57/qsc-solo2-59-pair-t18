"""并网点组件。

``GridTie`` 把限额、频率与电量流向收在一起。放电链路上三处判断各自独立，执行
顺序固定为「并网限额钳位 → 频率窗口校验 → PCS 爬坡」：钳位只产生本轮允许
目标，频率拒收不下发新目标，PCS 因此保持上一个有效功率；只有频率在窗口内，
才允许向钳位后的目标爬坡。
"""

from __future__ import annotations

from ..config import GridLimits
from .curtail import proportional_curtail
from .flow import PowerFlow
from .limit import PowerLimit
from .monitor import FrequencyMonitor
from .snapshot import GridSnapshot


class GridTie:
    """并网点组件。"""

    def __init__(self, limits: GridLimits) -> None:
        self._limits = limits
        self._limit = PowerLimit(limits)
        self._flow = PowerFlow()
        self._monitor = FrequencyMonitor(limits)
        self._checks = 0
        self._rejections = 0
        self._last_rejection: dict | None = None

    @property
    def limits(self) -> GridLimits:
        return self._limits

    @property
    def limit(self) -> PowerLimit:
        return self._limit

    @property
    def flow(self) -> PowerFlow:
        return self._flow

    def monitor_frequency(self, frequency_hz: float, now: float) -> dict:
        """记一次频率采样。"""

        return self._monitor.sample(frequency_hz, now)

    def check(self, frequency_hz: float, power_kw: float, now: float) -> dict:
        """核对并网条件与功率越限，返回判定结果。"""

        self._checks += 1
        allowed = self._limit.clamp(power_kw)
        gate = self.frequency_gate(frequency_hz, power_kw, allowed, now, held_kw=0.0)
        violations: list[str] = []
        if not gate["ok"]:
            violations.append("frequency_out_of_band")
        if abs(allowed - float(power_kw)) > 1e-9:
            violations.append("power_clamped")
        return {
            "requested_kw": round(float(power_kw), 3),
            "allowed_kw": round(allowed, 3),
            "headroom_kw": round(self._limit.headroom_kw(allowed), 3),
            "ratio": round(self._limit.ratio(allowed), 4),
            "frequency_hz": gate["frequency_hz"],
            "frequency_gate": gate,
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
            "strategy": "proportional",
        }

    def clamp_clusters(self, clusters: dict[str, float]) -> dict:
        """在爬坡之前按同一比例收缩各簇目标功率。"""

        return proportional_curtail(
            clusters,
            self._limits.export_limit_kw,
            limit_kw=self._limits.export_limit_kw,
        )

    def frequency_gate(
        self,
        frequency_hz: float,
        requested_kw: float,
        allowed_kw: float,
        now: float,
        *,
        held_kw: float,
    ) -> dict:
        """频率窗口校验；拒收时返回证据且不产生新的功率目标。"""

        sample = self.monitor_frequency(frequency_hz, now)
        evidence = {
            "sample_ts": sample["ts"],
            "frequency_hz": sample["frequency_hz"],
            "rated_frequency_hz": self._limits.rated_frequency_hz,
            "band_hz": self._limits.frequency_band_hz,
            "deviation_hz": sample["deviation_hz"],
            "ok": sample["ok"],
            "requested_kw": round(float(requested_kw), 3),
            "allowed_kw": round(float(allowed_kw), 3),
            "held_kw": round(float(held_kw), 3),
            "rejected": not sample["ok"],
            "previous_rejection_ts": None
            if self._last_rejection is None
            else self._last_rejection.get("sample_ts", self._last_rejection.get("last_rejection_ts")),
        }
        if not sample["ok"]:
            self._rejections += 1
            evidence["rejection_count"] = self._rejections
            evidence["last_rejection_ts"] = evidence["sample_ts"]
            self._last_rejection = dict(evidence)
        elif self._last_rejection is not None:
            evidence["last_rejection_ts"] = self._last_rejection.get(
                "sample_ts", self._last_rejection.get("last_rejection_ts")
            )
        return evidence

    def last_frequency_rejection(self) -> dict | None:
        """返回最近一次频率拒收证据。"""

        return dict(self._last_rejection) if self._last_rejection is not None else None

    def record_frequency_rejection(self, record: dict) -> None:
        """从审计流水恢复最近一次频率拒收，供重试追溯上一次拒收时间。"""

        if not isinstance(record, dict) or not record:
            return
        self._rejections = max(self._rejections, int(record.get("rejection_count", self._rejections)))
        self._last_rejection = dict(record)

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
            last_rejection=self.last_frequency_rejection(),
        )

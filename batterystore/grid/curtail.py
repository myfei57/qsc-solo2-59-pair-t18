"""簇群越限缩功率略。

并网点上网限额对的是"所有簇合成后"的总功率，越限之后怎么把总功率压回
限额内，本模块给两种策略，由电站按场合二选一：

``proportional``（按比例一起缩，默认）
    所有参与簇乘同一个系数，一次计算落定。优点是公平、响应快，所有簇
    同比例出力，没有谁先被牺牲；缺点是不区分簇的重要性，关键簇也会被
    一起压低，恢复时要等总目标重新回到限额内。适合常规调峰、计划放电
    这类簇间无主次、要求一拍内压住并网点的场合。

``priority``（按优先级一簇一簇往下压）
    从优先级最低的簇开始逐簇清零，直到总功率不超限额，最后一档只压到
    刚好够用。优点是能保住关键簇（优先级高的簇最后才被动）；缺点是低
    优先级簇可能整簇退出，恢复时也得按优先级顺序一簇一簇放回去，过程
    慢且对低优先级簇不公平。适合保供、黑启动支撑这类必须保住关键簇、
    允许牺牲其余簇的场合。

两种策略都只做"分配"，不下发：真正的爬坡仍由各簇 PCS 在钳位之后执行，
顺序不会因为换了缩功策略而被颠倒。
"""

from __future__ import annotations

from typing import Mapping

PROPORTIONAL = "proportional"
PRIORITY = "priority"
STRATEGIES = (PROPORTIONAL, PRIORITY)


class CurtailPlanner:
    """把超出并网点限额的总功率分配回各簇。"""

    def __init__(self, strategy: str = PROPORTIONAL) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"未知缩功策略: {strategy}，可选 {STRATEGIES}")
        self._strategy = strategy

    @property
    def strategy(self) -> str:
        return self._strategy

    def plan(
        self,
        requested: Mapping[str, float],
        allowed_total_kw: float,
        priorities: Mapping[str, int] | None = None,
    ) -> dict:
        """按所选策略计算各簇允许功率。

        ``requested`` 为各簇请求功率（按上网方向取正值），``priorities``
        数值越小优先级越高，仅在优先级策略下使用。
        """

        total_requested = sum(float(value) for value in requested.values())
        allowed_total = max(0.0, float(allowed_total_kw))
        if total_requested <= allowed_total + 1e-9 or total_requested <= 1e-9:
            ratio = 1.0 if total_requested <= 1e-9 else allowed_total / total_requested
            return {
                "strategy": self._strategy,
                "curtailed": False,
                "requested_total_kw": round(total_requested, 3),
                "allowed_total_kw": round(allowed_total, 3),
                "ratio": round(min(1.0, ratio), 4),
                "clusters": {
                    rack_id: {
                        "requested_kw": round(float(value), 3),
                        "allowed_kw": round(float(value), 3),
                        "ratio": 1.0,
                        "cut_kw": 0.0,
                        "zeroed": False,
                    }
                    for rack_id, value in requested.items()
                },
            }
        if self._strategy == PROPORTIONAL:
            allocation = self._proportional(requested, allowed_total, total_requested)
        else:
            allocation = self._priority(requested, allowed_total, priorities or {})
        return {
            "strategy": self._strategy,
            "curtailed": True,
            "requested_total_kw": round(total_requested, 3),
            "allowed_total_kw": round(allowed_total, 3),
            "ratio": round(allowed_total / total_requested, 4),
            "clusters": allocation,
        }

    def _proportional(
        self, requested: Mapping[str, float], allowed_total: float, total_requested: float
    ) -> dict:
        ratio = allowed_total / total_requested
        return {
            rack_id: self._cluster_entry(float(value), float(value) * ratio)
            for rack_id, value in requested.items()
        }

    def _priority(
        self, requested: Mapping[str, float], allowed_total: float, priorities: Mapping[str, int]
    ) -> dict:
        # 优先级数值大的先被压；优先级相同按簇编号排序，保证结果确定可追溯。
        order = sorted(requested, key=lambda rack_id: (-int(priorities.get(rack_id, 0)), rack_id))
        targets = {rack_id: float(value) for rack_id, value in requested.items()}
        for rack_id in order:
            if sum(targets.values()) <= allowed_total + 1e-9:
                break
            value = targets[rack_id]
            targets[rack_id] = max(0.0, value - (sum(targets.values()) - allowed_total))
        return {rack_id: self._cluster_entry(float(requested[rack_id]), targets[rack_id]) for rack_id in requested}

    @staticmethod
    def _cluster_entry(requested_kw: float, allowed_kw: float) -> dict:
        cut = requested_kw - allowed_kw
        return {
            "requested_kw": round(requested_kw, 3),
            "allowed_kw": round(allowed_kw, 3),
            "ratio": round(allowed_kw / requested_kw, 4) if requested_kw > 1e-9 else 1.0,
            "cut_kw": round(cut, 3),
            "zeroed": allowed_kw <= 1e-9 and requested_kw > 1e-9,
        }

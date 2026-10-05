"""放电簇功率收缩策略。

并网限额不足时，本系统选择按比例收缩：所有有放电请求的簇使用同一系数，一次
就能把并网点总功率压到限额内。它适合普通调峰、现货跟踪等追求快速公平响应的
场景。按优先级逐簇下压可保住关键簇，但需要逐簇下发、确认与恢复，适合保障
供电或有明确簇级重要度的场景；本系统的快速闭环不采用该策略。
"""

from __future__ import annotations

PROPORTIONAL = "proportional"


def proportional_curtail(
    clusters: dict[str, float],
    allowed_total_kw: float,
    *,
    precision: int = 6,
    limit_kw: float | None = None,
) -> dict:
    """把各簇放电功率按同一比例收缩到并网限额内。"""

    requested = {str(name): max(0.0, float(power_kw)) for name, power_kw in clusters.items()}
    total = sum(requested.values())
    limit = max(0.0, float(allowed_total_kw))
    allocated_total = min(total, limit)
    ratio = 1.0 if total <= limit else limit / total if total > 0.0 else 1.0
    load_ratio = allocated_total / float(limit_kw) if limit_kw else 1.0
    allocated = {name: round(value * ratio, precision) for name, value in requested.items()}
    return {
        "strategy": PROPORTIONAL,
        "requested_total_kw": round(total, precision),
        "allowed_total_kw": round(allocated_total, precision),
        "ratio": round(ratio, 6),
        "load_ratio": round(max(0.0, min(1.0, load_ratio)), 6),
        "clusters": allocated,
    }

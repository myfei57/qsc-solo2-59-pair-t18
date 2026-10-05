"""放电指令拒收留痕。

频率越窗的放电指令在爬坡之前被拒收，证据必须留下来：指令功率、钳位后的
允许功率、频率实测值与偏差、拒收时刻，以及拒收后实际保持的功率。重试时
还要能追溯到上一次拒收——每条记录带 ``prev_ts`` 与 ``since_prev_s``，
回答"这条重试距离上一次被拒隔了多久"。留痕窗口只保留有限条记录，长期
运行不会无限占用内存；累计计数不随窗口滚动丢失。
"""

from __future__ import annotations


class RejectionLedger:
    """放电拒收证据簿。"""

    def __init__(self, limit: int = 240) -> None:
        self._limit = max(1, limit)
        self._records: list[dict] = []
        self._total = 0

    def record(self, evidence: dict) -> dict:
        """补编序号与上次拒收时间后落一条证据，返回落簿记录。"""

        previous = self._records[-1] if self._records else None
        now = float(evidence["ts"])
        entry = {
            "seq": self._total + 1,
            "ts": round(now, 3),
            "station": evidence.get("station"),
            "op_id": evidence.get("op_id"),
            "reason": evidence.get("reason", "frequency_out_of_band"),
            "requested_kw": round(float(evidence["requested_kw"]), 3),
            "allowed_kw": round(float(evidence["allowed_kw"]), 3),
            "frequency_hz": round(float(evidence["frequency_hz"]), 4),
            "deviation_hz": round(float(evidence["deviation_hz"]), 4),
            "held_kw": round(float(evidence["held_kw"]), 3),
            "prev_ts": previous["ts"] if previous else None,
            "since_prev_s": round(now - previous["ts"], 3) if previous else None,
        }
        self._records.append(entry)
        if len(self._records) > self._limit:
            del self._records[: len(self._records) - self._limit]
        self._total += 1
        return dict(entry)

    def last(self) -> dict | None:
        """返回最近一次拒收证据。"""

        return dict(self._records[-1]) if self._records else None

    def recent(self, limit: int = 20) -> list[dict]:
        """返回最近的若干条拒收证据。"""

        return [dict(item) for item in self._records[-max(1, limit):]]

    def total(self) -> int:
        """返回累计拒收条数（不随窗口滚动而减少）。"""

        return self._total

"""曲线引擎 —— 温度 → PWM（两路风扇独立曲线）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from fanio import PWM_MAX, PWM_MIN


@dataclass
class Curve:
    """分段线性曲线。points = [(temp_c, pwm), ...]，按 temp 升序。"""

    points: list[tuple[float, int]] = field(default_factory=list)

    def pwm_for(self, temp: float | None) -> int:
        if temp is None or not self.points:
            return PWM_MIN
        pts = sorted(self.points)
        if temp <= pts[0][0]:
            return _clamp(pts[0][1])
        if temp >= pts[-1][0]:
            return _clamp(pts[-1][1])
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            if t0 <= temp <= t1:
                if t1 == t0:
                    return _clamp(p1)
                ratio = (temp - t0) / (t1 - t0)
                return _clamp(round(p0 + (p1 - p0) * ratio))
        return _clamp(pts[-1][1])

    def as_list(self) -> list[list[float]]:
        return [[t, p] for t, p in sorted(self.points)]

    @classmethod
    def from_list(cls, data) -> "Curve":
        pts = []
        for item in data or []:
            try:
                t, p = item
                pts.append((float(t), int(p)))
            except (TypeError, ValueError):
                continue
        return cls(points=sorted(pts))


def _clamp(v: int) -> int:
    return max(PWM_MIN, min(PWM_MAX, int(v)))


# 默认曲线：参考 UGOS /etc/default/dh2600.conf 的断点，但下限抬高防停转。
DEFAULT_CPU = Curve([(45, 60), (55, 90), (70, 140), (80, 190), (90, 235)])
DEFAULT_SYS = Curve([(30, 70), (40, 100), (48, 150), (55, 200), (60, 235)])


def from_ugos(conf: dict) -> tuple[Curve, Curve]:
    """用 UGOS 的配置参数构造曲线。缺失项用默认值。"""
    cpu = Curve(
        [
            (conf.get("cpu_temp_stop", 42), conf.get("fan_pwm_CPU_start", 80)),
            (conf.get("cpu_temp_start", 50), conf.get("fan_pwm_CPU_start", 80)),
            (conf.get("cpu_temp_mid0", 70), conf.get("fan_pwm_CPU_mid0", 140)),
            (conf.get("cpu_temp_full", 78), conf.get("fan_pwm_CPU_full", 200)),
            (conf.get("cpu_temp_max", 90), conf.get("fan_pwm_CPU_max", 230)),
        ]
    )
    sysc = Curve(
        [
            (conf.get("hdd_temp_stop", 30), conf.get("fan_pwm_SYS_start", 90)),
            (conf.get("hdd_temp_start", 40), conf.get("fan_pwm_SYS_start", 90)),
            (conf.get("hdd_temp_mid0", 46), conf.get("fan_pwm_SYS_mid0", 150)),
            (conf.get("hdd_temp_full", 52), conf.get("fan_pwm_SYS_full", 200)),
            (conf.get("hdd_temp_max", 55), conf.get("fan_pwm_SYS_max", 235)),
        ]
    )
    return cpu, sysc


# 三个预设档位（对应 UGOS 三档的近似效果）
PRESETS = {
    "silent": {
        "label": "静音",
        "cpu": 64,
        "sys": 70,
    },
    "balanced": {
        "label": "均衡",
        "cpu": 110,
        "sys": 90,
    },
    "performance": {
        "label": "性能",
        "cpu": 230,
        "sys": 235,
    },
}

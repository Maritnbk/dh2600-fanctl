"""控制循环 —— 持续写入以维持设定值；任何异常都退化为「停手」。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from curve import DEFAULT_CPU, DEFAULT_SYS, Curve, PRESETS, from_ugos
from fanio import FanIO, FanIOError, PWM_MAX, PWM_MIN
from temps import read_temps, read_ugos_curve

log = logging.getLogger("fanctl")


@dataclass
class Settings:
    mode: str = "auto"                 # auto | manual | off
    interval: float = 3.0              # 写入间隔（秒）
    manual_cpu: int = 110
    manual_sys: int = 90
    cpu_curve: Curve = field(default_factory=lambda: Curve(DEFAULT_CPU.as_list()))
    sys_curve: Curve = field(default_factory=lambda: Curve(DEFAULT_SYS.as_list()))
    # 安全阈值
    cpu_trip: float = 85.0             # CPU 超过即满转
    sys_trip: float = 55.0             # 硬盘/机箱超过即满转
    pwm_floor: int = 20                # 曲线下限，防停转
    cpu_pwm_floor: int = 40            # CPU 风扇下限更高


class Controller:
    def __init__(self, fan: FanIO, hwmon_root="/host/hwmon", default_dir="/host/default"):
        self.fan = fan
        self.hwmon_root = hwmon_root
        self.default_dir = default_dir

        self.settings = Settings()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.state = {
            "mode": "auto",
            "rpm": {"cpu": None, "sys": None},
            "temps": {},
            "applied": {"cpu": None, "sys": None},
            "target": {"cpu": None, "sys": None},
            "status": "starting",       # running | suspended | error | stopped
            "message": "",
            "trip": False,
            "suspend_until": 0.0,
            "last_write": 0.0,
            "last_error": "",
            "writes": 0,
            "started_at": time.time(),
        }

        # RPM 停转检测
        self._rpm_zero_since: float | None = None
        self._ever_spun = {"cpu": False, "sys": False}

    # ---------- 生命周期 ----------
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="fanctl", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---------- 对外 ----------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                **{k: v for k, v in self.state.items()},
                "settings": {
                    "mode": self.settings.mode,
                    "interval": self.settings.interval,
                    "manual_cpu": self.settings.manual_cpu,
                    "manual_sys": self.settings.manual_sys,
                    "cpu_curve": self.settings.cpu_curve.as_list(),
                    "sys_curve": self.settings.sys_curve.as_list(),
                    "pwm_floor": self.settings.pwm_floor,
                    "cpu_pwm_floor": self.settings.cpu_pwm_floor,
                    "cpu_trip": self.settings.cpu_trip,
                    "sys_trip": self.settings.sys_trip,
                },
                "presets": PRESETS,
                "uptime": round(time.time() - self.state["started_at"]),
            }

    def update(self, **kwargs) -> None:
        with self._lock:
            s = self.settings
            if "mode" in kwargs and kwargs["mode"] in ("auto", "manual", "off"):
                s.mode = kwargs["mode"]
                self.state["mode"] = s.mode
                # 从 off 恢复时清掉挂起
                if s.mode != "off":
                    self.state["suspend_until"] = 0.0
                    self.state["status"] = "running"
            if "interval" in kwargs:
                try:
                    s.interval = max(1.0, min(30.0, float(kwargs["interval"])))
                except (TypeError, ValueError):
                    pass
            if "manual_cpu" in kwargs:
                s.manual_cpu = max(PWM_MIN, min(PWM_MAX, int(kwargs["manual_cpu"])))
            if "manual_sys" in kwargs:
                s.manual_sys = max(PWM_MIN, min(PWM_MAX, int(kwargs["manual_sys"])))
            if "cpu_curve" in kwargs:
                s.cpu_curve = Curve.from_list(kwargs["cpu_curve"])
            if "sys_curve" in kwargs:
                s.sys_curve = Curve.from_list(kwargs["sys_curve"])

    def apply_preset(self, name: str) -> bool:
        p = PRESETS.get(name)
        if not p:
            return False
        with self._lock:
            self.settings.mode = "manual"
            self.settings.manual_cpu = p["cpu"]
            self.settings.manual_sys = p["sys"]
            self.state["mode"] = "manual"
            self.state["suspend_until"] = 0.0
            self.state["status"] = "running"
        return True

    def load_ugos_curve(self) -> bool:
        conf = read_ugos_curve(self.default_dir)
        if not conf:
            return False
        cpu, sysc = from_ugos(conf)
        with self._lock:
            self.settings.cpu_curve = cpu
            self.settings.sys_curve = sysc
        return True

    # ---------- 主循环 ----------
    def _loop(self):
        log.info("control loop started, interval=%.1fs", self.settings.interval)
        while not self._stop.is_set():
            interval = self.settings.interval
            try:
                self._tick()
                self._set(status=self.state["status"])
            except FanIOError as exc:
                log.warning("fan io error: %s", exc)
                self._set(status="error", last_error=str(exc))
            except Exception:
                log.exception("unexpected error in control loop")
                self._set(status="error", last_error="unexpected error")
            self._stop.wait(interval)

    def _tick(self):
        now = time.time()

        temps = read_temps(self.hwmon_root)
        try:
            rpm = self.fan.read()
        except FanIOError:
            rpm = {"cpu": None, "sys": None}

        with self._lock:
            self.state["temps"] = temps
            self.state["rpm"] = rpm

        self._check_stall(rpm, now)

        # 挂起中：完全不写，交给 hwmonitor
        with self._lock:
            suspend_until = self.state["suspend_until"]
            mode = self.settings.mode

        if suspend_until and now < suspend_until:
            self._set(status="suspended")
            return
        if suspend_until and now >= suspend_until:
            with self._lock:
                self.state["suspend_until"] = 0.0
                self._rpm_zero_since = None
            log.info("suspend window over, resuming")

        if mode == "off":
            self._set(status="stopped")
            return

        # 计算目标
        if self._tripped(temps):
            target = {"cpu": PWM_MAX, "sys": PWM_MAX}
            self._set(trip=True)
        else:
            self._set(trip=False)
            if mode == "manual":
                target = {
                    "cpu": self.settings.manual_cpu,
                    "sys": self.settings.manual_sys,
                }
            else:
                target = {
                    "cpu": max(
                        self.settings.cpu_curve.pwm_for(temps.get("cpu")),
                        self.settings.cpu_pwm_floor,
                    ),
                    "sys": max(
                        self.settings.sys_curve.pwm_for(
                            temps.get("sys_max") or temps.get("max")
                        ),
                        self.settings.pwm_floor,
                    ),
                }

        with self._lock:
            self.state["target"] = target

        self._apply(target, now)
        self._set(status="running")

    def _apply(self, target: dict, now: float):
        with self._lock:
            applied = dict(self.state["applied"])

        for key, setter in (("cpu", self.fan.set_cpu), ("sys", self.fan.set_sys)):
            want = target.get(key)
            if want is None:
                continue
            if applied.get(key) == want:
                continue          # 减少无谓写入
            setter(want)
            with self._lock:
                self.state["applied"][key] = want
                self.state["writes"] += 1
                self.state["last_write"] = now

    def _tripped(self, temps: dict) -> bool:
        cpu = temps.get("cpu")
        sys_hot = temps.get("sys_max")
        if cpu is not None and cpu >= self.settings.cpu_trip:
            return True
        if sys_hot is not None and sys_hot >= self.settings.sys_trip:
            return True
        return False

    def _check_stall(self, rpm: dict, now: float):
        """转速为 0 持续 30 秒（且曾经转过）→ 挂起 5 分钟，让 UGOS 接管。"""
        zero = any(
            rpm.get(k) == 0 and self._ever_spun[k] for k in ("cpu", "sys")
        )
        for k in ("cpu", "sys"):
            if rpm.get(k):
                self._ever_spun[k] = True

        if not zero:
            self._rpm_zero_since = None
            return

        if self._rpm_zero_since is None:
            self._rpm_zero_since = now
            return

        if now - self._rpm_zero_since >= 30:
            log.error("fan stall detected (rpm=%s), suspending for 5 min", rpm)
            with self._lock:
                self.state["suspend_until"] = now + 300
                self.state["status"] = "suspended"
                self.state["message"] = "风扇停转，已停手 5 分钟交由 UGOS 接管"
            self._rpm_zero_since = None

    def _set(self, **kwargs):
        with self._lock:
            self.state.update(kwargs)

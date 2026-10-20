"""FanIO —— /proc/it86/fan 的读写封装。

由 ug_it86x_sio_dh2600.ko 提供：
  读 → "cpufan speed:<N>\nsysfan1 speed:<N>\n"
  写 → "set N"   设置系统风扇 PWM (1-255)
       "cpu N"   设置 CPU 风扇 PWM (1-255)

注意：节点权限 0222。写不需要 root，读需要 root（CAP_DAC_OVERRIDE）。
"""

from __future__ import annotations

import re
import threading

_CPU_RE = re.compile(r"cpufan speed:(\d+)")
_SYS_RE = re.compile(r"sysfan1 speed:(\d+)")

PWM_MIN = 1
PWM_MAX = 255


class FanIOError(RuntimeError):
    pass


class FanIO:
    def __init__(self, path: str = "/it86/fan"):
        self.path = path
        self._lock = threading.Lock()

    # ---------- 读 ----------
    def read(self) -> dict:
        """返回 {'cpu': int|None, 'sys': int|None}。失败抛 FanIOError。"""
        try:
            with open(self.path, "r") as f:
                text = f.read()
        except OSError as exc:
            raise FanIOError(f"read {self.path}: {exc}") from exc

        cpu = _CPU_RE.search(text)
        sysf = _SYS_RE.search(text)
        if cpu is None and sysf is None:
            raise FanIOError(f"unexpected payload: {text!r}")

        return {
            "cpu": int(cpu.group(1)) if cpu else None,
            "sys": int(sysf.group(1)) if sysf else None,
        }

    # ---------- 写 ----------
    def set_sys(self, pwm: int) -> None:
        self._write(f"set {_clamp(pwm)}")

    def set_cpu(self, pwm: int) -> None:
        self._write(f"cpu {_clamp(pwm)}")

    def _write(self, cmd: str) -> None:
        with self._lock:
            try:
                with open(self.path, "w") as f:
                    f.write(cmd)
            except OSError as exc:
                raise FanIOError(f"write {cmd!r} -> {self.path}: {exc}") from exc


def _clamp(v: int) -> int:
    """内核拒绝 0（EINVAL），下限取 1。"""
    return max(PWM_MIN, min(PWM_MAX, int(v)))


def pwm_to_percent(pwm: int) -> int:
    return round(pwm * 100 / PWM_MAX)


def percent_to_pwm(pct: float) -> int:
    return _clamp(round(float(pct) * PWM_MAX / 100))

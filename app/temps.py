"""温度采集 —— 只读 /host/hwmon/*（coretemp / nvme / acpitz）。"""

from __future__ import annotations

import glob
import os
import re


def _read_int(path: str):
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_temps(hwmon_root: str = "/host/hwmon") -> dict:
    """返回各温度（摄氏度，整数）与最大值。

    {
      "cpu": int|None,      # coretemp 各核最大
      "cpu_cores": [...],
      "nvme": int|None,     # 所有 nvme 传感器最大
      "chipset": int|None,  # acpitz
      "max": int|None,      # 以上全部最大
    }
    """
    cpu_cores: list[int] = []
    nvme: list[int] = []
    chipset = None

    for hw in _iter_hwmon_dirs(hwmon_root):
        name_path = os.path.join(hw, "name")
        try:
            with open(name_path, "r") as f:
                name = f.read().strip()
        except OSError:
            continue

        for tpath in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            milli = _read_int(tpath)
            if milli is None:
                continue
            celsius = round(milli / 1000)

            if name == "coretemp":
                cpu_cores.append(celsius)
            elif name == "nvme":
                nvme.append(celsius)
            elif name in ("acpitz", "k10temp", "zenpower"):
                chipset = celsius if chipset is None else max(chipset, celsius)

    cpu = max(cpu_cores) if cpu_cores else None
    nvme_max = max(nvme) if nvme else None

    # 系统风扇的温度源：不含 CPU（CPU 有自己那一路风扇和更高的阈值）
    sys_candidates = [v for v in (nvme_max, chipset) if v is not None]
    sys_max = max(sys_candidates) if sys_candidates else None

    candidates = [v for v in (cpu, nvme_max, chipset) if v is not None]
    return {
        "cpu": cpu,
        "cpu_cores": cpu_cores,
        "nvme": nvme_max,
        "nvme_all": nvme,
        "chipset": chipset,
        "sys_max": sys_max,
        "max": max(candidates) if candidates else None,
    }


def _iter_hwmon_dirs(hwmon_root: str):
    """遍历 hwmon 目录。

    /sys/class/hwmon 下是相对符号链接（如 hwmon3 -> ../../devices/...），
    若只挂载了 /sys/class/hwmon，链接在容器内无法解析。此时退回到
    已挂载的 /host/devices 里按名字搜索。
    """
    dirs = []
    for hw in sorted(glob.glob(os.path.join(hwmon_root, "hwmon*"))):
        if os.path.isdir(hw) and os.path.exists(os.path.join(hw, "name")):
            dirs.append(hw)

    if dirs:
        return dirs

    # 兜底：直接在 /sys/devices 树里找 hwmon 目录
    alt = "/host/devices"
    if not os.path.isdir(alt):
        return []
    found = []
    for root, subdirs, files in os.walk(alt):
        if os.path.basename(root).startswith("hwmon") and "name" in files:
            found.append(root)
        if root.count(os.sep) - alt.count(os.sep) > 6:
            subdirs[:] = []
    return sorted(found)


def read_ugos_curve(default_dir: str = "/host/default") -> dict:
    """读取 UGOS 的温控曲线参数（只读参考，供 UI 展示/载入）。"""
    path = os.path.join(default_dir, "dh2600.conf")
    out: dict[str, int] = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if re.fullmatch(r"-?\d+", v):
                    out[k] = int(v)
    except OSError:
        return {}
    return out

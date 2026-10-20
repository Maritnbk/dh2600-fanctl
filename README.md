# DH2600 Fan Control

通过 `/proc/it86/fan`（厂商内核模块 `ug_it86x_sio_dh2600` 暴露的接口）控制绿联 DH2600 的两路风扇。

- **读** → 实时转速（RPM）
- **写** → 任意 PWM（1–255），CPU / 系统风扇独立
- **不需要** `--privileged`、`SYS_RAWIO`、`/dev/port`、内核模块编译
- **不需要** 停用 UGOS `hwmonitor`（它继续运行，作为失效兜底）

![version](https://img.shields.io/badge/version-v1.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 快速开始

### 方式一：用预构建镜像（推荐）

```bash
docker pull ghcr.io/maritnbk/dh2600-fanctl:v1.0
```

`docker-compose.yml` 里把 `build:` 换成 `image:`：

```yaml
services:
  fanctl:
    image: ghcr.io/maritnbk/dh2600-fanctl:v1.0
    # build: ...  ← 删掉或用注释掉
```

然后 `docker compose up -d`。

### 方式二：本地构建

```bash
cd /volume1/docker/fanctl

# 可选：设置访问令牌（留空则不鉴权，仅限纯内网）
export FANCTL_TOKEN=$(openssl rand -hex 16)

docker compose up -d --build
docker compose logs -f
```

访问 `http://<NAS-IP>:2600`。

---

## 镜像

| 项 | 值 |
|---|---|
| 镜像地址 | `ghcr.io/maritnbk/dh2600-fanctl` |
| 当前版本 | `v1.0`（也打 `1.0`、`latest`） |
| 架构 | `linux/amd64` |
| 基础镜像 | `python:3.12-slim` |
| 大小 | 158 MB |

```bash
# 拉取
docker pull ghcr.io/maritnbk/dh2600-fanctl:v1.0

# 或固定到次版本 / 最新
docker pull ghcr.io/maritnbk/dh2600-fanctl:1.0
docker pull ghcr.io/maritnbk/dh2600-fanctl:latest
```

镜像由 GitHub Actions 在打 tag 时自动构建发布，见 `.github/workflows/docker.yml`。
标签规则：

| Git 标签 | 生成的镜像标签 |
|---|---|
| `v1.0` | `v1.0`、`1.0`、`latest` |
| `v1.2.3` | `v1.2.3`、`1.2.3`、`1.2`、`1`、`latest` |
| `main` 分支推送 | `main`、`latest` |


---

## 配置

`docker-compose.yml`：

| 项 | 值 | 说明 |
|---|---|---|
| 网络 | `bridge` | 默认桥接网络 |
| 端口 | `2600:2600` | Web UI |
| 卷 | `/proc/it86:/it86:rw` | 风扇接口 |
| 卷 | `/sys/class/hwmon:/host/hwmon:ro` | 温度源 |
| 卷 | `/sys/devices:/host/devices:ro` | 温度源符号链接解析 |
| 卷 | `/etc/default:/host/default:ro` | UGOS 曲线参数 |
| 环境 | `INTERVAL=3` | 写入间隔（秒），2–5 之间较合适 |
| 环境 | `TOKEN=...` | 访问令牌，留空则不鉴权 |


### 为什么容器要 root

`/proc/it86/fan` 权限是 `0222`：

- **写**（设 PWM）→ 任何用户都能写
- **读**（看转速）→ 需要 root（`CAP_DAC_OVERRIDE` 绕过缺失的读位）

用 `-u 1000:1000` 跑容器会导致读不到转速，UI 上 RPM 显示 `—`。

---

## 工作模式

| 模式 | 行为 |
|---|---|
| **auto**（曲线自动） | 按温度查曲线算 PWM，持续写入 |
| **manual**（手动固定） | 固定 PWM，持续写入 |
| **off**（停手） | 停止写入，UGOS `hwmonitor` 在数十秒内自动接管 |

**关键设计**：所有异常路径的终点都是「停手」。因为 `hwmonitor` 一直在跑，停手就等于回到安全状态。

---

## 为什么需要"持续写入"

`hwmonitor` 的 `setFanLimit()` 在温度漂移、目标 PWM 变化时会重新写入 `/proc/it86/fan`，覆盖外部设定：

- `mode 2`（均衡）：外部值约能撑 **3 分钟**
- `mode 3`（性能）：约 **12 秒**就被覆盖

所以控制器每 `INTERVAL` 秒重写一次。这不是缺陷——容器崩了，`hwmonitor` 自动接管，等于免费的安全网。

---

## 安全机制

| 机制 | 判据 | 动作 |
|---|---|---|
| 超温强制 | CPU ≥ 85°C 或最高温 ≥ 55°C | 两路 PWM → 255 |
| 停转保护 | RPM 为 0 持续 30 秒（且曾转过） | 停手 5 分钟，交给 UGOS |
| PWM 下限 | 曲线值低于下限 | CPU 钳到 40，系统钳到 20 |
| 接口异常 | 读写连续抛错 | 停手 + UI 告警 |
| 一键回退 | UI「停手」按钮 | 停止写入 |

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web UI |
| GET | `/api/status` | 完整状态快照 |
| GET | `/api/stream?token=` | SSE，2 秒推一次 |
| POST | `/api/settings` | 改模式 / 间隔 / 手动 PWM / 曲线 |
| POST | `/api/preset` | `{"name":"silent\|balanced\|performance"}` |
| POST | `/api/ugos-curve` | 载入 UGOS 当前曲线参数 |
| POST | `/api/handback` | 停手，交还 UGOS |
| GET | `/healthz` | 健康检查（可读 RPM 则 200） |

鉴权：`Authorization: Bearer <TOKEN>`。SSE 用 query 参数（EventSource 限制）。

---

## 排查

```bash
# 接口在不在
ls -l /proc/it86/
cat /proc/it86/fan

# 模块加载了吗
lsmod | grep it86

# 容器状态
docker compose ps
docker compose logs --tail 50

# 容器内能否读
docker exec dh2600-fanctl python -c "from fanio import FanIO; print(FanIO().read())"

# 手动写（宿主机）
echo set 120 > /proc/it86/fan
echo cpu 80  > /proc/it86/fan
```

### 常见问题

**UI 上 RPM 显示 `—`**
容器不是 root 运行，读不到 `/proc/it86/fan`。检查 compose 有没有 `user:` 配置。

**PWM 设了但转速不变**
1. 看 `docker compose logs` 有没有写入错误
2. 宿主机直接 `echo set 200 > /proc/it86/fan` 试
3. 确认 `/proc/it86/fan` 存在

**`/proc/it86/` 不存在**（UGOS 升级后）
```bash
grep -n "DH2600" /usr/sbin/ug-load-drive.sh
find /lib/modules/$(uname -r) -name "*it86*"
modprobe ug_it86x-sio-dh2600
```

**风扇转速过高/过低**
调曲线，或切手动模式直接拖滑块。降噪优先动 **CPU 风扇**（4.3k RPM vs 系统风扇 1.9k，噪音主要来自前者）。

---

## UGOS 升级后

```bash
uname -r
ls -l /proc/it86/
md5sum /lib/modules/$(uname -r)/kernel/drivers/ugreen/dh2600/ug_it86x-sio-dh2600.ko
```

若接口消失，容器会自动停手并告警（不会把风扇锁死在错误值）。

---

## 卸载

```bash
docker compose down
docker rmi dh2600-fanctl:latest
rm -rf /volume1/docker/fanctl
```

无需恢复 `hwmonitor`——它从没被停过。

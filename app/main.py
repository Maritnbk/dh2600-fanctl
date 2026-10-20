"""FastAPI 入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from fanctl import Controller
from fanio import FanIO, FanIOError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("fanctl.web")

TOKEN = os.environ.get("TOKEN", "").strip()
INTERVAL = float(os.environ.get("INTERVAL", "3"))
STATIC = Path(__file__).parent / "static"

bearer = HTTPBearer(auto_error=False)


def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
    """TOKEN 未设置则不做鉴权（仅建议在内网且明确知晓时使用）。"""
    if not TOKEN:
        return True
    if cred is None or cred.credentials != TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")
    return True


fan = FanIO(os.environ.get("FAN_PATH", "/it86/fan"))
ctrl = Controller(fan)
ctrl.settings.interval = INTERVAL

app = FastAPI(title="DH2600 Fan Control", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup():
    # 启动自检：读不到转速说明权限不对（需要 root）
    try:
        rpm = fan.read()
        log.info("fan interface OK: %s", rpm)
    except FanIOError as exc:
        log.error("fan interface NOT readable: %s", exc)
        log.error("容器必须以 root 运行（/proc/it86/fan 是 0222，读需要 CAP_DAC_OVERRIDE）")
    ctrl.start()


@app.on_event("shutdown")
def _shutdown():
    ctrl.stop()


# ---------- 页面 ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


# ---------- API ----------
@app.get("/api/status")
def status(_=Depends(auth)):
    return ctrl.snapshot()


@app.get("/api/stream")
async def stream(request: Request, token: str | None = None):
    # EventSource 无法携带 Authorization 头，token 走 query 参数
    if TOKEN and token != TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")

    async def gen():
        while True:
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(ctrl.snapshot(), ensure_ascii=False)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class UpdateReq(BaseModel):
    mode: str | None = Field(default=None, pattern="^(auto|manual|off)$")
    interval: float | None = None
    manual_cpu: int | None = Field(default=None, ge=1, le=255)
    manual_sys: int | None = Field(default=None, ge=1, le=255)
    cpu_curve: list[list[float]] | None = None
    sys_curve: list[list[float]] | None = None


@app.post("/api/settings")
def update_settings(req: UpdateReq, _=Depends(auth)):
    data = {k: v for k, v in req.model_dump().items() if v is not None}
    ctrl.update(**data)
    return ctrl.snapshot()


class PresetReq(BaseModel):
    name: str


@app.post("/api/preset")
def preset(req: PresetReq, _=Depends(auth)):
    if not ctrl.apply_preset(req.name):
        raise HTTPException(status_code=400, detail="unknown preset")
    return ctrl.snapshot()


@app.post("/api/ugos-curve")
def ugos_curve(_=Depends(auth)):
    if not ctrl.load_ugos_curve():
        raise HTTPException(status_code=404, detail="/host/default/dh2600.conf 不可读")
    return ctrl.snapshot()


@app.post("/api/handback")
def handback(_=Depends(auth)):
    """停手，把控制权交还 UGOS hwmonitor。"""
    ctrl.update(mode="off")
    return ctrl.snapshot()


@app.get("/healthz")
def healthz():
    try:
        fan.read()
        return {"ok": True}
    except FanIOError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

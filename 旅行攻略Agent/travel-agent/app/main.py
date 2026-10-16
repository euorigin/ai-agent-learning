"""应用入口：组装 FastAPI，挂载 API 与单页前端。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .settings import APP_NAME, APP_VERSION, SETTINGS, WEB_DIR

app = FastAPI(title=APP_NAME, version=APP_VERSION,
              description="大模型驱动的旅行攻略 Agent：任意目的地、真实天气、真实路网、区间化预算。")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", include_in_schema=False)
def index():
    page = WEB_DIR / "index.html"
    if not page.exists():
        return JSONResponse({"error": "前端页面缺失", "expected": str(page)}, status_code=500)
    return FileResponse(page)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    icon = WEB_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return JSONResponse({}, status_code=204)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.on_event("startup")
def _banner() -> None:
    status = SETTINGS.status()
    print("=" * 68)
    print(f"  {APP_NAME} v{APP_VERSION}")
    print(f"  引擎        : {status['engine']}（TRAVEL_ENGINE={status['engine_setting']}）")
    print(f"  大模型      : {status['llm_model']} @ {status['llm_base_url']}")
    print(f"  API Key     : {status['llm_api_key']}")
    print(f"  地图/坐标源 : {status['geo_source']}")
    print(f"  监听        : http://{SETTINGS.host}:{SETTINGS.port}")
    if not status["llm_configured"]:
        print("  提示        : 尚未配置大模型，将使用规则引擎（功能受限）。"
              "请在 .env 中填写 TRAVEL_LLM_BASE_URL / TRAVEL_LLM_MODEL / TRAVEL_LLM_API_KEY")
    print("=" * 68)

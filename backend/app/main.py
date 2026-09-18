from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routers import access, apps, auth, certs, db, docker, github, mail, overview, proxy, rclone
from app.services import auth_svc, backup_svc, cert_alerts_svc, container_alerts_svc, discover_svc, proxy_svc


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Path("/data").mkdir(parents=True, exist_ok=True)
    Path("/config/rclone").mkdir(parents=True, exist_ok=True)
    conf = Path("/config/rclone/rclone.conf")
    if not conf.exists():
        conf.write_text("")
    # Load or migrate auth store (empty until the first owner is created in the browser)
    auth_svc.load_auth()
    proxy_svc.remember_detected()
    proxy_svc.ensure_dashboard_on_network()
    try:
        discover_svc.sync()
    except Exception:
        pass
    backup_svc.start_scheduler()
    container_alerts_svc.start_monitor()
    cert_alerts_svc.start_monitor()
    yield
    backup_svc.stop_scheduler()


app = FastAPI(title="VPS Dashboard", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(access.router)
app.include_router(overview.router)
app.include_router(overview.backup_router)
app.include_router(docker.router)
app.include_router(apps.router)
app.include_router(github.router)
app.include_router(certs.router)
app.include_router(proxy.router)
app.include_router(db.router)
app.include_router(rclone.router)
app.include_router(mail.router)

static_dir = Path(__file__).resolve().parent.parent / "static"


@app.get("/api/health")
def health():
    return {"ok": True}


if static_dir.exists():
    assets = static_dir / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return {"detail": "Not Found"}
        index = static_dir / "index.html"
        file_path = static_dir / full_path
        if full_path and file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(
            index,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )

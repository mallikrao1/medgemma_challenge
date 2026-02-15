from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .database import Base, SessionLocal, engine
from .gateway import ApiGatewayMiddleware
from .seed import seed_default_data
from .routers import admin_ui, audit_logs, auth, health, jobs, policies, tenants, users, workflows


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_default_data(db)
    finally:
        db.close()
    yield


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.add_middleware(ApiGatewayMiddleware)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(users.router)
app.include_router(audit_logs.router)
app.include_router(policies.router)
app.include_router(workflows.router)
app.include_router(jobs.router)
app.include_router(admin_ui.router)


@app.get("/")
def root() -> dict:
    return {"service": settings.app_name, "version": settings.app_version, "status": "running"}

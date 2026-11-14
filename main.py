from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from core.config import get_settings, setup_logging
from core.errors import register_exception_handlers
from core.initialization import run_db_migrations
from core.lifespan import lifespan
from routes.miniapp_api import router as miniapp_router
from routes.settings_api import router as settings_router
from routes.webhooks import router as webhooks_router

setup_logging()
settings = get_settings()

app = FastAPI(lifespan=lifespan)
app.include_router(webhooks_router)
app.include_router(settings_router)
app.include_router(miniapp_router)

app.mount("/webapp", StaticFiles(directory="static", html=True), name="static")
register_exception_handlers(app)

if __name__ == "__main__":
    import uvicorn

    run_db_migrations()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
    )

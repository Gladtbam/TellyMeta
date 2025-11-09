from fastapi import FastAPI

import bot.handlers
from core.config import setup_logging
from core.lifespan import lifespan
from routes.webhooks import router as webhooks_router

setup_logging()

app = FastAPI(lifespan=lifespan)
app.include_router(webhooks_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5080, log_level="info", reload=False)

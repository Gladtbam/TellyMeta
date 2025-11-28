from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

import bot.handlers
from core.config import setup_logging
from core.lifespan import lifespan
from routes.webhooks import router as webhooks_router

setup_logging()

app = FastAPI(lifespan=lifespan)
app.include_router(webhooks_router)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.error("URL: {} 请求失败： {}", request.url, exc.errors())
    return JSONResponse(
        status_code=422,
        content=""
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5080, log_level="info", reload=False)

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
import time
from datetime import datetime, timezone  # ✅ ДОБАВЛЕНО: timezone
from loguru import logger

from models_api import (
    ParseRequest,
    StartParseResponse,
    TaskResponse,
    StopParseResponse,
    HealthResponse,
    TaskStatus
)
from state_manager import task_manager
from tasks import run_parsing_task

# Инициализация FastAPI
app = FastAPI(
    title="Realty Parser Service",
    description="Сервис парсинга недвижимости (Avito + Cian)",
    version="1.0.0"
)

# Время запуска сервиса (для health check)
start_time = time.time()


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Проверка работоспособности сервиса"""
    return HealthResponse(
        status="ok",
        version="1.0.0",
        uptime_seconds=time.time() - start_time
    )


@app.post("/parse/start", response_model=StartParseResponse)
async def start_parsing(
        request: ParseRequest,
        background_tasks: BackgroundTasks
):
    """
    Запустить парсинг недвижимости

    - **user_id**: ID пользователя в Telegram
    - **avito_url**: Ссылка Avito с фильтрами (опционально)
    - **cian_url**: Ссылка Cian с фильтрами (опционально)
    - **pages**: Количество страниц для парсинга
    - **notification_bot_token**: Токен бота для уведомлений
    - **notification_chat_id**: Chat ID для уведомлений
    """

    # Валидация: хотя бы одна ссылка должна быть
    if not request.avito_url and not request.cian_url:
        raise HTTPException(
            status_code=400,
            detail="Необходимо указать хотя бы одну ссылку (Avito или Cian)"
        )

    # Создаём задачу
    task_id = task_manager.create_task(
        user_id=request.user_id,
        avito_url=str(request.avito_url) if request.avito_url else None,
        cian_url=str(request.cian_url) if request.cian_url else None,
        pages=request.pages
    )

    logger.info(f"Создана задача {task_id} для пользователя {request.user_id}")

    # Запускаем парсинг в фоне
    background_tasks.add_task(
        run_parsing_task,
        task_id=task_id,
        user_id=request.user_id,
        avito_url=str(request.avito_url) if request.avito_url else None,
        cian_url=str(request.cian_url) if request.cian_url else None,
        pages=request.pages,
        notification_bot_token=request.notification_bot_token,
        notification_chat_id=request.notification_chat_id
    )

    return StartParseResponse(
        task_id=task_id,
        status=TaskStatus.PENDING,
        message="Парсинг запущен",
        started_at=datetime.now(timezone.utc)
    )


@app.get("/parse/status/{task_id}", response_model=TaskResponse)
async def get_status(task_id: str):
    """
    Получить статус задачи парсинга

    - **task_id**: ID задачи
    """
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail=f"Задача {task_id} не найдена"
        )

    return task


@app.post("/parse/stop/{task_id}", response_model=StopParseResponse)
async def stop_parsing(task_id: str):
    """
    Остановить парсинг

    - **task_id**: ID задачи
    """
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail=f"Задача {task_id} не найдена"
        )

    # Проверяем статус
    if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED]:
        raise HTTPException(
            status_code=400,
            detail=f"Задача уже завершена со статусом: {task.status}"
        )

    # Запрашиваем остановку
    success = task_manager.request_stop(task_id)

    if not success:
        raise HTTPException(
            status_code=500,
            detail="Не удалось остановить задачу"
        )

    logger.info(f"Запрошена остановка задачи {task_id}")

    return StopParseResponse(
        task_id=task_id,
        status=TaskStatus.STOPPED,
        message="Запрос на остановку отправлен",
        stopped_at=datetime.now(timezone.utc)
    )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Обработка всех необработанных исключений"""
    logger.error(f"Необработанное исключение: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Внутренняя ошибка сервера"}
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import asyncio
import os
import signal
import time
from datetime import datetime, timezone  # ✅ ДОБАВЛЕНО: timezone
from loguru import logger
from pydantic import BaseModel

from models_api import (
    ParseRequest,
    StartParseResponse,
    TaskResponse,
    TaskProgress,
    SourceType,
    StopParseResponse,
    HealthResponse,
    MonitorHealthResponse,
    TaskStatus
)
from state_manager import task_manager, monitoring_state
# Phase 2: run_parsing_task больше не используется (мониторинг через monitoring_state)

# Время запуска сервиса (для health check)
start_time = time.time()


# Phase 2: Lifespan events для запуска/остановки мониторов
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager для управления мониторами"""
    logger.info("=" * 60)
    logger.info("ЗАПУСК СЕРВИСА: Realty Parser (Phase 2 - Monitoring Mode)")
    logger.info("=" * 60)

    # Startup: запуск очереди уведомлений и мониторов
    from monitor import avito_monitor, cian_monitor
    from notification_queue import notification_queue

    logger.info("Запуск очереди уведомлений...")
    await notification_queue.start()

    logger.info("Запуск мониторов...")
    await avito_monitor.start()
    await cian_monitor.start()
    logger.success("Мониторы и очередь уведомлений запущены")

    yield

    # Shutdown: остановка мониторов, затем очереди
    logger.info("=" * 60)
    logger.info("ОСТАНОВКА СЕРВИСА")
    logger.info("=" * 60)

    # Сначала обновляем статусы всех задач в БД (graceful shutdown)
    logger.info("Обновление статусов задач в БД...")
    stopped_count = monitoring_state.stop_all_tasks()
    logger.info(f"✅ Обновлено {stopped_count} задач в статус 'stopped'")

    logger.info("Остановка мониторов...")
    await avito_monitor.stop()
    await cian_monitor.stop()

    logger.info("Остановка очереди уведомлений...")
    await notification_queue.stop()
    logger.success("Сервис остановлен")


# Инициализация FastAPI с lifespan
app = FastAPI(
    title="Realty Parser Service",
    description="Сервис мониторинга недвижимости (Avito + Cian) - Monitoring Mode",
    version="2.0.0",  # Phase 2
    lifespan=lifespan
)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Проверка работоспособности сервиса"""
    return HealthResponse(
        status="ok",
        version="2.0.0",  # Phase 2
        uptime_seconds=time.time() - start_time
    )


@app.get("/monitor/health", response_model=MonitorHealthResponse)
async def monitor_health():
    """
    Phase 2: Проверка здоровья мониторов

    Возвращает:
    - Статус Avito/Cian мониторов
    - Метрики мониторинга (активные URL, ошибки, циклы)
    - Информацию о cookies (возраст, TTL)
    """
    from monitor import avito_monitor, cian_monitor
    from cookie_manager import cookie_manager

    return MonitorHealthResponse(
        status="ok",
        monitors={
            "avito": avito_monitor.get_metrics(),
            "cian": cian_monitor.get_metrics()
        },
        monitoring_state=monitoring_state.get_metrics(),
        cookie_manager=cookie_manager.get_cache_info()
    )


@app.get("/notifications/health")
async def notifications_health():
    """
    Phase 3: Метрики очереди уведомлений

    Возвращает:
    - Размер очереди
    - Количество отправленных/ошибочных/дропнутых сообщений
    - Rate limit
    """
    from notification_queue import notification_queue

    return {
        "status": "ok",
        **notification_queue.get_metrics()
    }


@app.post("/parse/start", response_model=StartParseResponse)
async def start_parsing(request: ParseRequest):
    """
    Phase 2: Запустить мониторинг недвижимости

    Вместо запуска отдельного парсера, регистрирует URL в Monitor.
    Monitor будет периодически проверять первую страницу и уведомлять о новых объявлениях.

    - **user_id**: ID пользователя в Telegram
    - **avito_url**: Ссылка Avito с фильтрами (опционально)
    - **cian_url**: Ссылка Cian с фильтрами (опционально)
    - **pages**: Игнорируется в режиме мониторинга (всегда 1 страница)
    - **notification_bot_token**: Токен бота для уведомлений
    - **notification_chat_id**: Chat ID для уведомлений
    """

    # Валидация: хотя бы одна ссылка должна быть
    if not request.avito_url and not request.cian_url:
        raise HTTPException(
            status_code=400,
            detail="Необходимо указать хотя бы одну ссылку (Avito или Cian)"
        )

    # Подготовка конфига пользователя (фильтры)
    user_config = {
        "tg_token": request.notification_bot_token,
        "tg_chat_id": [str(request.notification_chat_id)],
        "pause_chat_id": str(request.pause_notification_chat_id or request.notification_chat_id),
        "min_price": 0,  # TODO: добавить в ParseRequest
        "max_price": 999_999_999,
        "keys_word_white_list": [],
        "keys_word_black_list": [],
        "seller_black_list": [],
        "geo": None,
        "max_age": 24 * 60 * 60,
        "ignore_reserv": True,
        "ignore_promotion": False
    }

    registered_tasks = []

    # Регистрация Avito URL
    if request.avito_url:
        import uuid
        avito_task_id = f"avito_{uuid.uuid4()}"
        success = monitoring_state.register_url(
            task_id=avito_task_id,
            url=str(request.avito_url),
            platform="avito",
            user_id=request.user_id,
            config=user_config
        )
        if success:
            registered_tasks.append(avito_task_id)
            logger.info(f"Зарегистрирован Avito URL для мониторинга: {avito_task_id}")

    # Регистрация Cian URL
    if request.cian_url:
        import uuid
        cian_task_id = f"cian_{uuid.uuid4()}"

        # Для Cian добавляем специфичные поля
        cian_config = user_config.copy()
        cian_config.update({
            "location": "Москва",  # TODO: извлечь из URL
            "deal_type": "rent_long",
            "min_area": 0,
            "max_area": 999_999
        })

        success = monitoring_state.register_url(
            task_id=cian_task_id,
            url=str(request.cian_url),
            platform="cian",
            user_id=request.user_id,
            config=cian_config
        )
        if success:
            registered_tasks.append(cian_task_id)
            logger.info(f"Зарегистрирован Cian URL для мониторинга: {cian_task_id}")

    if not registered_tasks:
        raise HTTPException(
            status_code=500,
            detail="Не удалось зарегистрировать ни один URL"
        )

    # Возвращаем первый task_id (для обратной совместимости)
    # В реальности зарегистрировано может быть 2 task_id (avito + cian)
    primary_task_id = registered_tasks[0]

    return StartParseResponse(
        task_id=primary_task_id,
        status=TaskStatus.MONITORING,
        message=f"Мониторинг запущен (зарегистрировано URL: {len(registered_tasks)})",
        started_at=datetime.now(timezone.utc)
    )


@app.get("/parse/status/{task_id}", response_model=TaskResponse)
async def get_status(task_id: str):
    """
    Phase 2: Получить статус задачи/мониторинга

    - **task_id**: ID задачи
    """
    # Проверяем в monitoring_state (новый режим)
    url_data = monitoring_state.get_url_data(task_id)

    if url_data:
        # Phase 2: конвертируем в TaskResponse
        status_map = {
            "active": TaskStatus.MONITORING,
            "paused": TaskStatus.PAUSED,
            "stopped": TaskStatus.STOPPED
        }

        # Конвертируем platform строку в SourceType enum
        platform = url_data["platform"]
        source = SourceType.AVITO if platform == "avito" else SourceType.CIAN

        return TaskResponse(
            task_id=task_id,
            user_id=url_data["user_id"],
            status=status_map.get(url_data["status"], TaskStatus.MONITORING),
            progress=TaskProgress(
                source=source,
                current_page=1,
                found_ads=url_data.get("notifications_sent", 0),
                filtered_ads=0
            ),
            started_at=url_data["registered_at"],
            updated_at=url_data.get("last_check"),
            error_message=url_data.get("last_error")
        )

    # Fallback: проверяем в старом task_manager
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
    Phase 2: Остановить мониторинг

    Удаляет URL из реестра мониторинга.

    - **task_id**: ID задачи
    """
    # Проверяем в monitoring_state (новый режим)
    url_data = monitoring_state.get_url_data(task_id)

    if url_data:
        # Phase 2: удаление из мониторинга
        success = monitoring_state.unregister_url(task_id)

        if not success:
            raise HTTPException(
                status_code=500,
                detail="Не удалось остановить мониторинг"
            )

        logger.info(f"Мониторинг остановлен: {task_id} ({url_data['url']})")

        return StopParseResponse(
            task_id=task_id,
            status=TaskStatus.STOPPED,
            message="Мониторинг остановлен",
            stopped_at=datetime.now(timezone.utc)
        )

    # Fallback: проверяем в старом task_manager (обратная совместимость)
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

    # Запрашиваем остановку (старый режим)
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


@app.post("/parse/resume/{task_id}")
async def resume_parsing(task_id: str):
    """
    Phase 2: Возобновить приостановленный мониторинг

    Сбрасывает счётчик ошибок и возвращает URL в активный мониторинг.

    - **task_id**: ID задачи (должна быть в статусе paused)
    """
    url_data = monitoring_state.get_url_data(task_id)

    if not url_data:
        raise HTTPException(
            status_code=404,
            detail=f"Задача {task_id} не найдена"
        )

    if url_data["status"] != "paused":
        raise HTTPException(
            status_code=400,
            detail=f"Задача не приостановлена (статус: {url_data['status']})"
        )

    monitoring_state.resume_url(task_id)
    logger.info(f"Мониторинг возобновлён: {task_id} ({url_data['url']})")

    return {
        "task_id": task_id,
        "status": "active",
        "message": "Мониторинг возобновлён"
    }


class ProxyUpdateRequest(BaseModel):
    proxy_string: str
    proxy_change_url: str


@app.get("/config/proxy")
async def get_proxy_config():
    """Получить текущие настройки прокси из config.toml"""
    from load_config import get_proxy_config
    return get_proxy_config()


@app.post("/config/proxy")
async def update_proxy_config(request: ProxyUpdateRequest):
    """Обновить настройки прокси (proxy_string и proxy_change_url) в config.toml для Avito и Cian"""
    from load_config import save_proxy_config
    try:
        save_proxy_config(request.proxy_string, request.proxy_change_url)
        logger.info(f"Настройки прокси обновлены: {request.proxy_string[:20]}...")
        return {"status": "ok", "message": "Настройки прокси обновлены"}
    except Exception as e:
        logger.error(f"Ошибка обновления прокси: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/restart")
async def restart_service():
    """Перезапустить сервис — Docker поднимет его заново с обновлёнными настройками"""
    async def _shutdown():
        await asyncio.sleep(0.5)  # Дать время на отправку ответа
        logger.info("Получена команда перезапуска от администратора. Завершение процесса...")
        os.kill(os.getpid(), signal.SIGTERM)
    asyncio.create_task(_shutdown())
    return {"status": "restarting", "message": "Сервис перезапускается..."}


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Обработка всех необработанных исключений"""
    import traceback
    logger.error(f"Необработанное исключение: {exc}")
    logger.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": f"Внутренняя ошибка сервера: {str(exc)}"}
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8009)
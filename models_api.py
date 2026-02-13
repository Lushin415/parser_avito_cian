from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum


class SourceType(str, Enum):
    """Источник парсинга"""
    AVITO = "avito"
    CIAN = "cian"


class TaskStatus(str, Enum):
    """Статус задачи парсинга"""
    PENDING = "pending"  # В очереди
    RUNNING = "running"  # Выполняется
    COMPLETED = "completed"  # Завершено успешно
    FAILED = "failed"  # Ошибка
    STOPPED = "stopped"  # Остановлено пользователем
    # Phase 2: новые статусы для мониторинга
    MONITORING = "monitoring"  # Активный мониторинг
    PAUSED = "paused"  # Приостановлен после ошибок
    ERROR = "error"  # Критическая ошибка


class ParseRequest(BaseModel):
    """Запрос на запуск парсинга"""
    user_id: int = Field(..., description="ID пользователя в Telegram")
    avito_url: Optional[HttpUrl] = Field(None, description="Ссылка Avito с фильтрами")
    cian_url: Optional[HttpUrl] = Field(None, description="Ссылка Cian с фильтрами")
    pages: int = Field(3, ge=1, le=100, description="Количество страниц для парсинга")

    # Уведомления в СВОЙ бот пользователя
    notification_bot_token: str = Field(..., description="Токен бота для уведомлений")
    notification_chat_id: int = Field(..., description="Chat ID для уведомлений")

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": 123456,
                "avito_url": "https://www.avito.ru/moskva/kommercheskaya_nedvizhimost",
                "cian_url": "https://cian.ru/cat.php?deal_type=rent&region=1",
                "pages": 3,
                "notification_bot_token": "123456:ABC-DEF...",
                "notification_chat_id": 123456
            }
        }


class TaskProgress(BaseModel):
    """Прогресс выполнения задачи"""
    total_pages: int = 0
    current_page: int = 0
    found_ads: int = 0
    filtered_ads: int = 0
    source: Optional[SourceType] = None  # Какой источник сейчас парсится


class TaskResponse(BaseModel):
    """Ответ с информацией о задаче"""
    task_id: str
    user_id: int
    status: TaskStatus
    progress: Optional[TaskProgress] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None

    # Результаты (если completed)
    results: Optional[Dict[str, Any]] = None


class StartParseResponse(BaseModel):
    """Ответ на запуск парсинга"""
    task_id: str
    status: TaskStatus
    message: str = "Парсинг запущен"
    started_at: datetime


class StopParseResponse(BaseModel):
    """Ответ на остановку парсинга"""
    task_id: str
    status: TaskStatus
    message: str = "Парсинг остановлен"
    stopped_at: datetime


class HealthResponse(BaseModel):
    """Ответ на health check"""
    status: str = "ok"
    version: str = "1.0.0"
    uptime_seconds: float


class MonitorHealthResponse(BaseModel):
    """Ответ на monitor health check (Phase 2)"""
    status: str = "ok"
    monitors: Dict[str, Any] = {}  # avito_monitor, cian_monitor
    monitoring_state: Dict[str, Any] = {}  # метрики из monitoring_state
    cookie_manager: Dict[str, Any] = {}  # информация о cookies
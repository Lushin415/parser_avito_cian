import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional, List
from loguru import logger
from models_api import TaskStatus, TaskProgress, TaskResponse
import uuid


class TaskStateManager:
    """Управление состоянием задач парсинга (старый режим - для обратной совместимости)"""

    def __init__(self):
        self._tasks: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_task(self, user_id: int, **kwargs) -> str:
        """Создать новую задачу"""
        task_id = str(uuid.uuid4())

        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "user_id": user_id,
                "status": TaskStatus.PENDING,
                "progress": TaskProgress().model_dump(),
                "started_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "completed_at": None,
                "error_message": None,
                "results": None,
                "stop_event": threading.Event(),  # Для остановки парсинга
                **kwargs
            }

        return task_id

    def get_task(self, task_id: str) -> Optional[TaskResponse]:
        """Получить информацию о задаче"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            # Убираем stop_event из ответа (не сериализуется)
            task_copy = task.copy()
            task_copy.pop("stop_event", None)

            return TaskResponse(**task_copy)

    def update_task(self, task_id: str, **updates):
        """Обновить состояние задачи"""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(updates)
                self._tasks[task_id]["updated_at"] = datetime.now(timezone.utc)

    def update_progress(
            self,
            task_id: str,
            current_page: int = None,
            found_ads: int = None,
            filtered_ads: int = None,
            source: str = None
    ):
        """Обновить прогресс задачи"""
        with self._lock:
            if task_id in self._tasks:
                progress = self._tasks[task_id]["progress"]

                if current_page is not None:
                    progress["current_page"] = current_page
                if found_ads is not None:
                    progress["found_ads"] = found_ads
                if filtered_ads is not None:
                    progress["filtered_ads"] = filtered_ads
                if source is not None:
                    progress["source"] = source

                self._tasks[task_id]["updated_at"] = datetime.now(timezone.utc)

    def set_running(self, task_id: str):
        """Установить статус 'выполняется'"""
        self.update_task(task_id, status=TaskStatus.RUNNING)

    def set_completed(self, task_id: str, results: dict = None):
        """Установить статус 'завершено'"""
        self.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            results=results or {}
        )

    def set_failed(self, task_id: str, error: str):
        """Установить статус 'ошибка'"""
        self.update_task(
            task_id,
            status=TaskStatus.FAILED,
            completed_at=datetime.now(timezone.utc),
            error_message=error
        )

    def set_stopped(self, task_id: str):
        """Установить статус 'остановлено'"""
        self.update_task(
            task_id,
            status=TaskStatus.STOPPED,
            completed_at=datetime.now(timezone.utc)
        )

    def get_stop_event(self, task_id: str) -> Optional[threading.Event]:
        """Получить stop_event для задачи"""
        with self._lock:
            task = self._tasks.get(task_id)
            return task.get("stop_event") if task else None

    def request_stop(self, task_id: str) -> bool:
        """Запросить остановку задачи"""
        stop_event = self.get_stop_event(task_id)
        if stop_event:
            stop_event.set()
            return True
        return False


class MonitoringStateManager:
    """
    Управление состоянием мониторинга (Phase 2)

    Хранит URL registry: task_id -> {url, platform, user_id, config, error_count, status}
    Персистентность: SQLite таблица monitored_urls — восстановление после рестарта.
    """

    def __init__(self, db_name: str = "database.db"):
        self._monitored_urls: Dict[str, dict] = {}  # task_id -> url_data
        self._lock = threading.Lock()
        self._db_name = db_name

        # Метрики
        self._metrics = {
            "total_registered": 0,
            "total_stopped": 0,
            "total_errors": 0,
            "last_error": None
        }

        # Создание таблицы и восстановление состояния
        self._init_db()
        self._restore_from_db()

    def _init_db(self):
        """Создание таблицы monitored_urls если не существует"""
        with sqlite3.connect(self._db_name) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS monitored_urls (
                    task_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    config TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    started_at REAL NOT NULL,
                    registered_at REAL NOT NULL
                )
                """
            )
            # Migration: добавляем колонки если их нет
            for col_def in ["last_check REAL", "notifications_sent INTEGER DEFAULT 0"]:
                try:
                    conn.execute(f"ALTER TABLE monitored_urls ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass  # Колонка уже существует
            conn.commit()

    def _restore_from_db(self):
        """Восстановление активных URL из БД после рестарта"""
        with sqlite3.connect(self._db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT task_id, url, platform, user_id, config, status, started_at, registered_at, "
                "last_check, notifications_sent "
                "FROM monitored_urls WHERE status IN ('active', 'paused')"
            )
            rows = cursor.fetchall()

        if not rows:
            return

        for row in rows:
            task_id, url, platform, user_id, config_json, status, started_at, registered_at, last_check_ts, notifications_sent = row
            try:
                config = json.loads(config_json)
            except json.JSONDecodeError:
                logger.error(f"Ошибка парсинга config для {task_id}, пропускаю")
                continue

            self._monitored_urls[task_id] = {
                "task_id": task_id,
                "url": url,
                "platform": platform,
                "user_id": user_id,
                "config": config,
                "error_count": 0,
                "status": status,
                "registered_at": datetime.fromtimestamp(registered_at, tz=timezone.utc),
                "started_at": started_at,
                "last_check": datetime.fromtimestamp(last_check_ts, tz=timezone.utc) if last_check_ts else None,
                "last_error": None,
                "notifications_sent": notifications_sent or 0,
            }

        self._metrics["total_registered"] = len(self._monitored_urls)
        logger.info(f"Восстановлено {len(self._monitored_urls)} URL из БД")

    def _db_save(self, url_data: dict):
        """Сохранение URL в БД"""
        try:
            with sqlite3.connect(self._db_name) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO monitored_urls
                    (task_id, url, platform, user_id, config, status, started_at, registered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        url_data["task_id"],
                        url_data["url"],
                        url_data["platform"],
                        url_data["user_id"],
                        json.dumps(url_data["config"], ensure_ascii=False),
                        url_data["status"],
                        url_data["started_at"],
                        url_data["registered_at"].timestamp(),
                    )
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка сохранения в БД: {e}")

    def _db_delete(self, task_id: str):
        """Удаление URL из БД"""
        try:
            with sqlite3.connect(self._db_name) as conn:
                conn.execute("DELETE FROM monitored_urls WHERE task_id = ?", (task_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка удаления из БД: {e}")

    def _db_update_check_stats(self, task_id: str, last_check_ts: float, notifications_sent: int):
        """Обновление статистики проверки в БД"""
        try:
            with sqlite3.connect(self._db_name) as conn:
                conn.execute(
                    "UPDATE monitored_urls SET last_check = ?, notifications_sent = ? WHERE task_id = ?",
                    (last_check_ts, notifications_sent, task_id)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка обновления статистики в БД: {e}")

    def _db_update_status(self, task_id: str, status: str):
        """Обновление статуса в БД"""
        try:
            with sqlite3.connect(self._db_name) as conn:
                conn.execute(
                    "UPDATE monitored_urls SET status = ? WHERE task_id = ?",
                    (status, task_id)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка обновления статуса в БД: {e}")

    def register_url(
        self,
        task_id: str,
        url: str,
        platform: str,
        user_id: int,
        config: dict
    ) -> bool:
        """
        Регистрация URL для мониторинга

        Args:
            task_id: Уникальный ID задачи
            url: URL для мониторинга
            platform: "avito" или "cian"
            user_id: ID пользователя
            config: Конфиг фильтров пользователя

        Returns:
            bool: True если успешно зарегистрирован
        """
        with self._lock:
            if task_id in self._monitored_urls:
                return False

            url_data = {
                "task_id": task_id,
                "url": url,
                "platform": platform.lower(),
                "user_id": user_id,
                "config": config,
                "error_count": 0,
                "status": "active",  # active, paused, stopped
                "registered_at": datetime.now(timezone.utc),
                "started_at": time.time(),  # unix timestamp для фильтрации объявлений
                "last_check": None,
                "last_error": None,
                "notifications_sent": 0,
            }
            self._monitored_urls[task_id] = url_data
            self._metrics["total_registered"] += 1

        # Сохранение в БД (вне lock — IO операция)
        self._db_save(url_data)
        return True

    def unregister_url(self, task_id: str) -> bool:
        """
        Удаление URL из мониторинга

        Args:
            task_id: ID задачи

        Returns:
            bool: True если успешно удалён
        """
        with self._lock:
            if task_id in self._monitored_urls:
                del self._monitored_urls[task_id]
                self._metrics["total_stopped"] += 1
            else:
                return False

        self._db_delete(task_id)
        return True

    def get_url_data(self, task_id: str) -> Optional[dict]:
        """Получение данных URL"""
        with self._lock:
            return self._monitored_urls.get(task_id)

    def get_urls_for_platform(self, platform: str) -> List[dict]:
        """
        Получение всех активных URL для платформы

        Args:
            platform: "avito" или "cian"

        Returns:
            List[dict]: Список данных URL
        """
        with self._lock:
            return [
                url_data.copy()
                for url_data in self._monitored_urls.values()
                if url_data["platform"] == platform.lower()
                   and url_data["status"] == "active"
            ]

    def get_all_active_urls(self) -> List[dict]:
        """Получение всех активных URL"""
        with self._lock:
            return [
                url_data.copy()
                for url_data in self._monitored_urls.values()
                if url_data["status"] == "active"
            ]

    def increment_error(self, task_id: str, error_msg: str = None) -> Optional[dict]:
        """
        Увеличить счётчик ошибок для URL

        После 5 последовательных ошибок - пауза URL.

        Returns:
            Снимок url_data если задача только что приостановлена, иначе None.
            Используется вызывающим кодом для отправки уведомления.
        """
        paused_snapshot = None
        with self._lock:
            if task_id not in self._monitored_urls:
                return None

            url_data = self._monitored_urls[task_id]
            url_data["error_count"] += 1
            url_data["last_error"] = error_msg
            url_data["last_check"] = datetime.now(timezone.utc)

            self._metrics["total_errors"] += 1
            self._metrics["last_error"] = {
                "task_id": task_id,
                "error": error_msg,
                "timestamp": datetime.now(timezone.utc)
            }

            # Паузим после 5 ошибок
            if url_data["error_count"] >= 5:
                url_data["status"] = "paused"
                paused_snapshot = url_data.copy()
                logger.warning(
                    f"URL {url_data['url']} приостановлен после 5 ошибок"
                )

        if paused_snapshot:
            self._db_update_status(task_id, "paused")

        return paused_snapshot

    def record_check(self, task_id: str, new_items_count: int = 0):
        """Запись результата успешной проверки: сброс ошибок, обновление статистики"""
        last_check_ts = None
        notif_count = None
        with self._lock:
            if task_id not in self._monitored_urls:
                return
            url_data = self._monitored_urls[task_id]
            url_data["error_count"] = 0
            url_data["last_check"] = datetime.now(timezone.utc)
            url_data["notifications_sent"] = url_data.get("notifications_sent", 0) + new_items_count
            last_check_ts = url_data["last_check"].timestamp()
            notif_count = url_data["notifications_sent"]
        self._db_update_check_stats(task_id, last_check_ts, notif_count)

    def pause_url(self, task_id: str):
        """Приостановка мониторинга URL"""
        with self._lock:
            if task_id in self._monitored_urls:
                self._monitored_urls[task_id]["status"] = "paused"
            else:
                return
        self._db_update_status(task_id, "paused")

    def resume_url(self, task_id: str):
        """Возобновление мониторинга URL"""
        with self._lock:
            if task_id in self._monitored_urls:
                self._monitored_urls[task_id]["status"] = "active"
                self._monitored_urls[task_id]["error_count"] = 0
            else:
                return
        self._db_update_status(task_id, "active")

    def get_metrics(self) -> dict:
        """Получение метрик мониторинга"""
        with self._lock:
            active_count = sum(
                1 for url in self._monitored_urls.values()
                if url["status"] == "active"
            )
            paused_count = sum(
                1 for url in self._monitored_urls.values()
                if url["status"] == "paused"
            )

            return {
                "total_monitored": len(self._monitored_urls),
                "active": active_count,
                "paused": paused_count,
                "total_registered": self._metrics["total_registered"],
                "total_stopped": self._metrics["total_stopped"],
                "total_errors": self._metrics["total_errors"],
                "last_error": self._metrics["last_error"]
            }

    def get_status(self, task_id: str) -> Optional[str]:
        """Получение статуса URL"""
        with self._lock:
            url_data = self._monitored_urls.get(task_id)
            return url_data["status"] if url_data else None

    def stop_all_tasks(self):
        """
        Остановка всех активных задач (graceful shutdown)

        Вызывается при остановке сервиса для корректного обновления
        статусов всех задач в БД.
        """
        with self._lock:
            active_tasks = [
                task_id for task_id, data in self._monitored_urls.items()
                if data["status"] in ("active", "paused")
            ]

        # Обновляем статусы в БД (вне lock)
        # Пишем 'paused' вместо 'stopped' — чтобы _restore_from_db() восстановил их после рестарта
        for task_id in active_tasks:
            self._db_update_status(task_id, "paused")

        logger.info(f"🛑 Graceful shutdown: приостановлено {len(active_tasks)} задач (восстановятся после рестарта)")
        return len(active_tasks)


# Глобальные экземпляры
task_manager = TaskStateManager()  # Старый режим
monitoring_state = MonitoringStateManager()  # Новый режим (Phase 2)

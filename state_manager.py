import threading
from datetime import datetime, timezone
from typing import Dict, Optional
from models_api import TaskStatus, TaskProgress, TaskResponse
import uuid


class TaskStateManager:
    """Управление состоянием задач парсинга"""

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


# Глобальный экземпляр
task_manager = TaskStateManager()
"""
tasks.py - Legacy код для старого режима парсинга (Phase 1)

В Phase 2 (Monitoring Mode) этот файл НЕ используется.
Регистрация URL происходит напрямую в api.py через monitoring_state.

Оставлен для обратной совместимости.
"""
import threading
import time
from typing import Dict, Any
from loguru import logger
from state_manager import task_manager
from models_api import SourceType
from dto import AvitoConfig, CianConfig


def run_avito_parsing(task_id: str, avito_url: str, pages: int,
                      notification_bot_token: str, notification_chat_id: int,
                      stop_event: threading.Event, results: Dict[str, Any]):
    """Запуск парсинга Avito в отдельном потоке"""
    try:
        logger.info(f"[Task {task_id}] Запуск парсинга Avito")
        task_manager.update_progress(
            task_id,
            source=SourceType.AVITO,
            current_page=0
        )

        from avito_parser import AvitoParse

        config = AvitoConfig(
            urls=[str(avito_url)],
            count=pages,
            tg_token=notification_bot_token,
            tg_chat_id=[str(notification_chat_id)],
            one_time_start=False,
        )

        parser = AvitoParse(config=config, stop_event=stop_event)

        parser.start()

        results["avito"] = {
            "status": "completed",
            "good_requests": parser.good_request_count,
            "bad_requests": parser.bad_request_count
        }

        logger.info(f"[Task {task_id}] Avito парсинг завершён")

    except Exception as e:
        logger.error(f"[Task {task_id}] Ошибка парсинга Avito: {e}")
        results["avito"] = {
            "status": "failed",
            "error": str(e)
        }


def run_cian_parsing(task_id: str, cian_url: str, pages: int,
                     notification_bot_token: str, notification_chat_id: int,
                     stop_event: threading.Event, results: Dict[str, Any]):
    """Запуск парсинга Cian в отдельном потоке"""
    try:
        logger.info(f"[Task {task_id}] Запуск парсинга Cian")
        task_manager.update_progress(
            task_id,
            source=SourceType.CIAN,
            current_page=0
        )

        from cian_parser import CianParser
        from urllib.parse import urlparse, parse_qs
        from cian_cities import CIAN_CITIES

        # Определяем город из URL
        parsed = urlparse(str(cian_url))
        query_params = parse_qs(parsed.query)
        region = query_params.get('region', [None])[0]

        location = "Москва"  # по умолчанию
        for city_name, city_code in CIAN_CITIES.items():
            if city_code == region:
                location = city_name
                break

        config = CianConfig(
            urls=[str(cian_url)],
            location=location,
            count=pages,
            tg_token=notification_bot_token,
            tg_chat_id=[str(notification_chat_id)],
            one_time_start=False,
        )

        parser = CianParser(config=config, stop_event=stop_event)

        parser.start()

        results["cian"] = {
            "status": "completed",
            "good_requests": parser.good_request_count,
            "bad_requests": parser.bad_request_count
        }

        logger.info(f"[Task {task_id}] Cian парсинг завершён")

    except Exception as e:
        logger.error(f"[Task {task_id}] Ошибка парсинга Cian: {e}")
        results["cian"] = {
            "status": "failed",
            "error": str(e)
        }

def run_parsing_task(
        task_id: str,
        user_id: int,
        avito_url: str = None,
        cian_url: str = None,
        pages: int = 3,
        notification_bot_token: str = None,
        notification_chat_id: int = None
):
    """
    Запуск парсинга в фоновом потоке (ПАРАЛЛЕЛЬНО!)
    """
    try:
        task_manager.set_running(task_id)
        stop_event = task_manager.get_stop_event(task_id)

        results: Dict[str, Any] = {
            "avito": None,
            "cian": None
        }

        threads = []

        # ✅ ЗАПУСКАЕМ AVITO В ОТДЕЛЬНОМ ПОТОКЕ
        if avito_url:
            avito_thread = threading.Thread(
                target=run_avito_parsing,
                args=(task_id, avito_url, pages, notification_bot_token,
                      notification_chat_id, stop_event, results),
                name=f"avito-{task_id}"
            )
            avito_thread.start()
            threads.append(avito_thread)

        # ✅ ЗАПУСКАЕМ CIAN В ОТДЕЛЬНОМ ПОТОКЕ (ОДНОВРЕМЕННО!)
        if cian_url:
            cian_thread = threading.Thread(
                target=run_cian_parsing,
                args=(task_id, cian_url, pages, notification_bot_token,
                      notification_chat_id, stop_event, results),
                name=f"cian-{task_id}"
            )
            cian_thread.start()
            threads.append(cian_thread)

        # ✅ ЖДЁМ ЗАВЕРШЕНИЯ ОБОИХ ПОТОКОВ
        for thread in threads:
            thread.join()

        # --- ЗАВЕРШЕНИЕ ---
        if stop_event.is_set():
            task_manager.set_stopped(task_id)
            logger.info(f"[Task {task_id}] Парсинг остановлен пользователем")
        else:
            task_manager.set_completed(task_id, results=results)
            logger.info(f"[Task {task_id}] Парсинг завершён успешно123")

    except Exception as e:
        logger.error(f"[Task {task_id}] Критическая ошибка: {e}")
        task_manager.set_failed(task_id, error=str(e))
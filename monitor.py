"""
Monitor - система мониторинга Avito/Cian вместо per-user парсеров

Архитектура:
- AvitoMonitor и CianMonitor работают как asyncio tasks
- Один curl_cffi.Session на платформу (shared)
- Последовательный polling всех пользовательских URL
- Задержка 5-15 сек между запросами = built-in rate limiting
- Cookies через CookieManager (Phase 1)
- Фильтрация через Option B (shared parser instance, reconfig before each URL)
"""
import asyncio
import random
import time
from typing import List, Dict, Optional
from dataclasses import dataclass

from curl_cffi import requests
from loguru import logger

from cookie_manager import cookie_manager
from avito_parser import AvitoParse
from cian_parser import CianParser
from state_manager import monitoring_state
from db_service import SQLiteDBHandler
from dto import AvitoConfig, CianConfig
from models import Item
from cian_models import CianItem


@dataclass
class MonitoredURL:
    """Структура мониторимого URL"""
    url: str
    user_id: int
    platform: str  # "avito" или "cian"
    config: dict  # Конфиг фильтров для пользователя
    error_count: int = 0
    status: str = "active"  # active, paused, error
    last_check: float = 0
    task_id: str = ""


class BaseMonitor:
    """Базовый класс для мониторов"""

    def __init__(self, platform: str, num_workers: int = 3):
        self.platform = platform
        self.db_handler = SQLiteDBHandler()
        self.running = False
        self.task: Optional[asyncio.Task] = None

        # Воркеры: каждый со своей curl_cffi сессией
        self.num_workers = num_workers
        self.sessions: List[requests.Session] = [
            requests.Session() for _ in range(num_workers)
        ]
        self._url_queue: Optional[asyncio.Queue] = None
        self._worker_tasks: List[asyncio.Task] = []

        # Тайминги (могут быть переопределены в подклассах из config)
        self.pause_between_requests = (5, 10)  # мин/макс секунд между URL
        self.pause_between_cycles = 30  # секунд между полными циклами

        # Защита от бана: при 403/429 увеличиваем паузу между циклами
        self._block_detected = False
        self._block_cooldown = 300  # 5 минут при блокировке
        self._consecutive_blocks = 0  # счётчик подряд блокировок

        # Метрики
        self.total_cycles = 0
        self.total_requests = 0
        self.total_errors = 0
        self.last_cycle_time = 0

        logger.info(f"{platform.upper()} Monitor инициализирован ({num_workers} воркеров)")

    async def start(self, proxy=None):
        """Запуск мониторинга

        Args:
            proxy: Прокси для браузера (опционально, берётся из config.toml)
        """
        if self.running:
            logger.warning(f"{self.platform} Monitor уже запущен")
            return

        self.running = True
        logger.info(f"Запуск {self.platform} Monitor...")

        # Запуск браузера для cookies (передаём прокси если есть)
        await cookie_manager.start(proxy=proxy)

        # Создание asyncio task
        self.task = asyncio.create_task(self._monitor_loop())
        logger.success(f"{self.platform} Monitor запущен")

    async def stop(self):
        """Остановка мониторинга"""
        if not self.running:
            return

        logger.info(f"Остановка {self.platform} Monitor...")
        self.running = False

        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        # Остановка браузера
        await cookie_manager.stop()

        logger.success(f"{self.platform} Monitor остановлен")

    async def _monitor_loop(self):
        """Основной цикл: раздаёт URL воркерам через очередь"""
        logger.info(f"{self.platform} Monitor: основной цикл запущен")

        self._url_queue = asyncio.Queue()

        # Запуск воркеров
        self._worker_tasks = []
        for i in range(self.num_workers):
            task = asyncio.create_task(self._worker(i, self.sessions[i]))
            self._worker_tasks.append(task)
        logger.info(f"{self.platform} Monitor: запущено {self.num_workers} воркеров")

        try:
            while self.running:
                try:
                    cycle_start = time.time()

                    # Получение списка активных URL
                    monitored_urls = monitoring_state.get_urls_for_platform(self.platform)

                    if not monitored_urls:
                        logger.debug(f"{self.platform}: нет активных URL, ожидание...")
                        await asyncio.sleep(10)
                        continue

                    logger.info(
                        f"{self.platform} Monitor: начало цикла, "
                        f"активных URL: {len(monitored_urls)}, воркеров: {self.num_workers}"
                    )

                    # Сброс флага блокировки перед циклом
                    self._block_detected = False

                    # Раздаём URL воркерам через очередь
                    for url_data in monitored_urls:
                        if self._block_detected or not self.running:
                            break
                        await self._url_queue.put(url_data)

                    # Ждём пока все URL обработаны
                    await self._url_queue.join()

                    # Статистика цикла
                    cycle_time = time.time() - cycle_start
                    self.last_cycle_time = cycle_time
                    self.total_cycles += 1

                    logger.info(
                        f"{self.platform} Monitor: цикл завершён за {cycle_time:.1f}с, "
                        f"обработано URL: {len(monitored_urls)}"
                    )

                    # Пауза между циклами
                    if self._block_detected:
                        self._consecutive_blocks += 1
                        cooldown = min(self._block_cooldown * self._consecutive_blocks, 1800)
                        logger.warning(
                            f"{self.platform} Monitor: блокировка #{self._consecutive_blocks}, "
                            f"увеличенная пауза {cooldown}с"
                        )
                        await asyncio.sleep(cooldown)
                    else:
                        self._consecutive_blocks = 0
                        logger.info(
                            f"{self.platform} Monitor: пауза {self.pause_between_cycles}с до следующего цикла"
                        )
                        await asyncio.sleep(self.pause_between_cycles)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"{self.platform} Monitor: ошибка в цикле: {e}")
                    await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info(f"{self.platform} Monitor: получен сигнал остановки")
        finally:
            # Остановка воркеров
            for t in self._worker_tasks:
                t.cancel()
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            logger.info(f"{self.platform} Monitor: основной цикл завершён")

    async def _worker(self, worker_id: int, session: requests.Session):
        """Воркер: берёт URL из очереди, обрабатывает, ждёт паузу"""
        logger.debug(f"{self.platform} Worker-{worker_id}: запущен")

        try:
            while True:
                url_data = await self._url_queue.get()
                try:
                    if self._block_detected or not self.running:
                        # При блокировке — пропускаем
                        self._url_queue.task_done()
                        continue

                    await self._process_url(url_data, session)

                    # Пауза между запросами (rate limiting)
                    min_delay, max_delay = self.pause_between_requests
                    delay = random.uniform(min_delay, max_delay)
                    logger.debug(f"{self.platform} W-{worker_id}: пауза {delay:.1f}с")
                    await asyncio.sleep(delay)

                except Exception as e:
                    logger.error(
                        f"{self.platform} W-{worker_id}: ошибка {url_data['url']}: {e}"
                    )
                    monitoring_state.increment_error(
                        url_data['task_id'], error_msg=str(e)
                    )
                    self.total_errors += 1
                finally:
                    self._url_queue.task_done()

        except asyncio.CancelledError:
            logger.debug(f"{self.platform} Worker-{worker_id}: остановлен")

    async def _process_url(self, url_data: dict, session: requests.Session = None):
        """
        Обработка одного URL (должен быть переопределён в подклассах)

        Args:
            url_data: Словарь с данными URL из monitoring_state
            session: curl_cffi сессия воркера
        """
        raise NotImplementedError

    def get_metrics(self) -> dict:
        """Получение метрик мониторинга"""
        return {
            "platform": self.platform,
            "running": self.running,
            "num_workers": self.num_workers,
            "total_cycles": self.total_cycles,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "last_cycle_time": self.last_cycle_time,
            "active_urls": len(monitoring_state.get_urls_for_platform(self.platform)),
            "consecutive_blocks": self._consecutive_blocks,
            "block_detected": self._block_detected,
        }


class AvitoMonitor(BaseMonitor):
    """Монитор для Avito"""

    def __init__(self):
        super().__init__("avito")

        # Загрузка базового конфига (для прокси и технических настроек)
        from load_config import load_avito_config
        try:
            base_config = load_avito_config("config.toml")
            logger.debug(f"Avito Monitor: загружен config с прокси: {base_config.proxy_string[:20]}..." if base_config.proxy_string else "без прокси")
        except Exception as e:
            logger.warning(f"Не удалось загрузить config.toml для Avito Monitor: {e}, работаем без прокси")
            base_config = AvitoConfig(urls=[])

        # Сохраняем прокси для передачи в CookieManager
        from dto import Proxy
        if base_config.proxy_string and base_config.proxy_change_url:
            self.proxy = Proxy(
                proxy_string=base_config.proxy_string,
                change_ip_link=base_config.proxy_change_url
            )
            logger.info(f"Avito Monitor: прокси настроен ({base_config.proxy_string.split('@')[0] if '@' in base_config.proxy_string else base_config.proxy_string.split(':')[0]})")
        else:
            self.proxy = None
            logger.info("Avito Monitor: работаем без прокси")

        # Тайминги из config.toml (5-10с между запросами, 30с между циклами)
        self.pause_between_requests = (
            max(base_config.pause_between_links, 5),
            max(base_config.pause_between_links * 2, 10)
        )
        self.pause_between_cycles = max(base_config.pause_general, 30)
        logger.info(
            f"Avito Monitor: тайминги - между запросами {self.pause_between_requests[0]}-{self.pause_between_requests[1]}с, "
            f"между циклами {self.pause_between_cycles}с"
        )

        # Option B: один экземпляр парсера, будем менять config перед каждым URL
        self.parser = AvitoParse(config=base_config)

    async def start(self):
        """Запуск мониторинга (переопределяем чтобы передать прокси)"""
        await super().start(proxy=self.proxy)

    async def _process_url(self, url_data: dict, session: requests.Session = None):
        """Обработка одного Avito URL"""
        url = url_data['url']
        user_id = url_data['user_id']
        task_id = url_data['task_id']
        user_config = url_data['config']
        session = session or self.sessions[0]

        logger.debug(f"Avito: обработка {url} (user={user_id})")

        try:
            # 1. Получение cookies и User-Agent
            cookies, user_agent = await cookie_manager.get_cookies("avito")

            if not cookies:
                logger.warning(f"Avito: нет валидных cookies, пропускаю {url}")
                return

            # 2. Fetch HTML (sync операция в async context)
            from common_data import HEADERS
            headers = HEADERS.copy()
            headers["user-agent"] = user_agent

            html = await asyncio.to_thread(
                self._fetch_html,
                session,
                url,
                cookies,
                headers
            )

            if not html:
                logger.warning(f"Avito: не удалось получить HTML для {url}")
                monitoring_state.increment_error(task_id, "Failed to fetch HTML")
                return

            self.total_requests += 1

            # 3. Парсинг JSON из HTML (@staticmethod, без экземпляра)
            data = AvitoParse.find_json_on_page(html)
            catalog = data.get("data", {}).get("catalog") or {}

            if not catalog.get("items"):
                logger.debug(f"Avito: нет объявлений на {url}")
                monitoring_state.reset_error_count(task_id)
                return

            # Парсинг items
            from models import ItemsResponse
            try:
                ads_models = ItemsResponse(**catalog)
                items = ads_models.items
            except Exception as e:
                logger.error(f"Avito: ошибка валидации: {e}")
                return

            # Очистка null items
            items = [ad for ad in items if ad.id]

            # Добавление seller
            items = self.parser._add_seller_to_ads(items)

            logger.info(f"Avito: найдено {len(items)} объявлений на {url}")

            # 4. Фильтрация (Option B: переконфигурируем parser)
            filtered_items = await self._filter_items(items, user_config)

            logger.info(f"Avito: после фильтрации осталось {len(filtered_items)} объявлений")

            # 5. Фильтр по времени старта мониторинга (только новые объявления)
            started_at = url_data.get('started_at', 0)
            if started_at and filtered_items:
                # Debug: показать значения sortTimeStamp для диагностики
                sample = filtered_items[0]
                logger.debug(
                    f"Avito: started_at={started_at:.0f}, "
                    f"sample sortTimeStamp={sample.sortTimeStamp}, "
                    f"sample id={sample.id}"
                )
                before_count = len(filtered_items)
                started_at_ms = started_at * 1000  # sortTimeStamp в миллисекундах
                filtered_items = [
                    ad for ad in filtered_items
                    if ad.sortTimeStamp and ad.sortTimeStamp > started_at_ms
                ]
                skipped = before_count - len(filtered_items)
                if skipped:
                    logger.debug(f"Avito: отфильтровано {skipped} старых объявлений (до старта мониторинга)")

            # 6. Проверка против БД (не отправлять повторно)
            new_items = [ad for ad in filtered_items if not self._is_viewed(ad)]

            logger.info(f"Avito: новых объявлений: {len(new_items)}")

            # 7. Отправка уведомлений
            if new_items:
                await self._send_notifications(new_items, user_config)

            # 8. Сохранение в БД
            if new_items:
                self.db_handler.add_record_from_page(new_items)

            # Сброс счётчика ошибок при успехе
            monitoring_state.reset_error_count(task_id)

        except Exception as e:
            logger.error(f"Avito: ошибка обработки {url}: {e}")
            monitoring_state.increment_error(task_id, str(e))
            raise

    def _fetch_html(self, session: requests.Session, url: str, cookies: dict, headers: dict) -> Optional[str]:
        """Синхронная загрузка HTML (вызывается через asyncio.to_thread)"""
        try:
            response = session.get(
                url=url,
                headers=headers,
                cookies=cookies,
                impersonate="chrome",
                timeout=20,
                verify=False
            )

            if response.status_code == 200:
                return response.text
            elif response.status_code in [403, 429]:
                logger.warning(f"Avito: блокировка {response.status_code}")
                self._block_detected = True
                return None
            else:
                logger.warning(f"Avito: status {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Avito: fetch error: {e}")
            return None

    async def _filter_items(self, items: List[Item], user_config: dict) -> List[Item]:
        """Фильтрация объявлений (Option B: переконфигурируем parser)"""
        # Создаём временный config из user_config
        from dto import AvitoConfig

        config = AvitoConfig(
            urls=[],
            min_price=user_config.get("min_price", 0),
            max_price=user_config.get("max_price", 999_999_999),
            keys_word_white_list=user_config.get("keys_word_white_list", []),
            keys_word_black_list=user_config.get("keys_word_black_list", []),
            seller_black_list=user_config.get("seller_black_list", []),
            geo=user_config.get("geo"),
            max_age=user_config.get("max_age", 24 * 60 * 60),
            ignore_reserv=user_config.get("ignore_reserv", True),
            ignore_promotion=user_config.get("ignore_promotion", False)
        )

        # Переконфигурируем parser
        self.parser.config = config

        # Вызываем filter_ads
        return self.parser.filter_ads(items)

    def _is_viewed(self, ad: Item) -> bool:
        """Проверка просмотрено ли объявление"""
        return self.db_handler.record_exists(ad.id, ad.priceDetailed.value)

    async def _send_notifications(self, items: List[Item], user_config: dict):
        """Phase 3: Отправка уведомлений через notification queue"""
        from notification_queue import notification_queue

        for ad in items:
            try:
                await notification_queue.enqueue_ad(
                    ad=ad,
                    user_config=user_config,
                    platform="avito"
                )
            except Exception as e:
                logger.error(f"Avito: ошибка добавления в очередь: {e}")


class CianMonitor(BaseMonitor):
    """Монитор для Cian"""

    def __init__(self):
        super().__init__("cian")

        # Загрузка базового конфига (для прокси и технических настроек)
        from load_config import load_cian_config
        try:
            base_config = load_cian_config("config.toml")
            logger.debug(f"Cian Monitor: загружен config с прокси: {base_config.proxy_string[:20]}..." if base_config.proxy_string else "без прокси")
        except Exception as e:
            logger.warning(f"Не удалось загрузить config.toml для Cian Monitor: {e}, работаем без прокси")
            base_config = CianConfig(urls=[], location="Москва")

        # Сохраняем прокси для передачи в CookieManager
        from dto import Proxy
        if base_config.proxy_string and base_config.proxy_change_url:
            self.proxy = Proxy(
                proxy_string=base_config.proxy_string,
                change_ip_link=base_config.proxy_change_url
            )
            logger.info(f"Cian Monitor: прокси настроен ({base_config.proxy_string.split('@')[0] if '@' in base_config.proxy_string else base_config.proxy_string.split(':')[0]})")
        else:
            self.proxy = None
            logger.info("Cian Monitor: работаем без прокси")

        # Тайминги из config.toml (5-10с между запросами, 30с между циклами)
        self.pause_between_requests = (
            max(base_config.pause_between_links, 5),
            max(base_config.pause_between_links * 2, 10)
        )
        self.pause_between_cycles = max(base_config.pause_general, 30)
        logger.info(
            f"Cian Monitor: тайминги - между запросами {self.pause_between_requests[0]}-{self.pause_between_requests[1]}с, "
            f"между циклами {self.pause_between_cycles}с"
        )

        # Option B: один экземпляр парсера
        self.parser = CianParser(config=base_config)

    async def start(self):
        """Запуск мониторинга (переопределяем чтобы передать прокси)"""
        await super().start(proxy=self.proxy)

    async def _process_url(self, url_data: dict, session: requests.Session = None):
        """Обработка одного Cian URL"""
        url = url_data['url']
        user_id = url_data['user_id']
        task_id = url_data['task_id']
        user_config = url_data['config']
        session = session or self.sessions[0]

        logger.debug(f"Cian: обработка {url} (user={user_id})")

        try:
            # 1. Получение cookies
            cookies, user_agent = await cookie_manager.get_cookies("cian")

            if not cookies:
                logger.warning(f"Cian: нет валидных cookies, пропускаю {url}")
                return

            # 2. Fetch HTML
            from common_data import HEADERS
            headers = HEADERS.copy()
            headers["user-agent"] = user_agent

            html = await asyncio.to_thread(
                self._fetch_html,
                session,
                url,
                cookies,
                headers
            )

            if not html:
                logger.warning(f"Cian: не удалось получить HTML для {url}")
                monitoring_state.increment_error(task_id, "Failed to fetch HTML")
                return

            self.total_requests += 1

            # 3. Парсинг списка объявлений
            items = self.parser.parse_list_page(html)

            logger.info(f"Cian: найдено {len(items)} объявлений на {url}")

            if not items:
                monitoring_state.reset_error_count(task_id)
                return

            # 4. Фильтрация
            filtered_items = await self._filter_items(items, user_config)

            logger.info(f"Cian: после фильтрации осталось {len(filtered_items)} объявлений")

            # 5. Фильтр по времени старта мониторинга
            started_at = url_data.get('started_at', 0)
            if started_at:
                before_count = len(filtered_items)
                filtered_items = [
                    ad for ad in filtered_items
                    if ad.timestamp and ad.timestamp > started_at
                ]
                skipped = before_count - len(filtered_items)
                if skipped:
                    logger.debug(f"Cian: отфильтровано {skipped} старых объявлений (до старта мониторинга)")

            # 6. Проверка против БД
            new_items = [ad for ad in filtered_items if not self._is_viewed(ad)]

            logger.info(f"Cian: новых объявлений: {len(new_items)}")

            # 7. Отправка уведомлений
            if new_items:
                await self._send_notifications(new_items, user_config)

            # 8. Сохранение в БД
            if new_items:
                self._save_to_db(new_items)

            # Сброс ошибок
            monitoring_state.reset_error_count(task_id)

        except Exception as e:
            logger.error(f"Cian: ошибка обработки {url}: {e}")
            monitoring_state.increment_error(task_id, str(e))
            raise

    def _fetch_html(self, session: requests.Session, url: str, cookies: dict, headers: dict) -> Optional[str]:
        """Синхронная загрузка HTML"""
        try:
            response = session.get(
                url=url,
                headers=headers,
                cookies=cookies,
                impersonate="chrome",
                timeout=20,
                verify=False
            )

            if response.status_code == 200:
                return response.text
            elif response.status_code in [403, 429]:
                logger.warning(f"Cian: блокировка {response.status_code}")
                self._block_detected = True
                return None
            else:
                logger.warning(f"Cian: status {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Cian: fetch error: {e}")
            return None

    async def _filter_items(self, items: List[CianItem], user_config: dict) -> List[CianItem]:
        """Фильтрация объявлений"""
        from dto import CianConfig

        config = CianConfig(
            urls=[],
            location=user_config.get("location", "Москва"),
            deal_type=user_config.get("deal_type", "rent_long"),
            min_price=user_config.get("min_price", 0),
            max_price=user_config.get("max_price", 999_999_999),
            min_area=user_config.get("min_area", 0),
            max_area=user_config.get("max_area", 999_999)
        )

        # Переконфигурируем parser
        self.parser.config = config

        # Фильтрация
        return self.parser.filter_ads(items)

    def _is_viewed(self, ad: CianItem) -> bool:
        """Проверка просмотрено ли объявление"""
        if not ad.price or ad.price.value <= 0:
            return False

        ad_id = int(ad.id) if ad.id.isdigit() else abs(hash(ad.id))
        return self.db_handler.record_exists(ad_id, ad.price.value)

    def _save_to_db(self, items: List[CianItem]):
        """Сохранение в БД"""
        try:
            import sqlite3
            records = []
            for ad in items:
                if ad.price and ad.price.value > 0:
                    ad_id = int(ad.id) if ad.id.isdigit() else abs(hash(ad.id))
                    records.append((ad_id, ad.price.value))

            if records:
                with sqlite3.connect(self.db_handler.db_name) as conn:
                    cursor = conn.cursor()
                    cursor.executemany(
                        "INSERT OR REPLACE INTO viewed (id, price) VALUES (?, ?)",
                        records
                    )
                    conn.commit()
                logger.debug(f"Cian: сохранено {len(records)} в БД")

        except Exception as e:
            logger.error(f"Cian: ошибка сохранения в БД: {e}")

    async def _send_notifications(self, items: List[CianItem], user_config: dict):
        """Phase 3: Отправка уведомлений через notification queue"""
        from notification_queue import notification_queue

        for ad in items:
            try:
                await notification_queue.enqueue_ad(
                    ad=ad,
                    user_config=user_config,
                    platform="cian"
                )
            except Exception as e:
                logger.error(f"Cian: ошибка добавления в очередь: {e}")


# Глобальные экземпляры мониторов
avito_monitor = AvitoMonitor()
cian_monitor = CianMonitor()

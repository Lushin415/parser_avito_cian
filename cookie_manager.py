"""
CookieManager - singleton для управления единственным экземпляром Playwright браузера
и cookies с TTL для Avito и Cian
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Optional, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from playwright.async_api import async_playwright, Browser

from dto import Proxy
from playwright_setup import ensure_playwright_installed


@dataclass
class CookieCache:
    """Кэш cookies с метаданными"""
    cookies: dict
    user_agent: str
    timestamp: float
    platform: str  # "avito" или "cian"


class CookieManager:
    """
    Singleton для управления единственным браузером Playwright и cookies с TTL

    Возможности:
    - Единственный экземпляр браузера на всю систему
    - Раздельные cookies для Avito и Cian с TTL
    - Автоматическая проактивная обновка cookies перед истечением TTL
    - Ротация User-Agent при каждом обновлении cookies
    - Thread-safe операции с asyncio.Lock
    - Персистентность cookies на диск с file locking
    - Автоматический запуск браузера по требованию (lazy init)
    - Автоматическое завершение браузера при отсутствии активности
    """

    _instance: Optional['CookieManager'] = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Инициализация только один раз
        if hasattr(self, '_initialized'):
            return

        self._initialized = True
        self.browser: Optional[Browser] = None
        self.playwright_context = None
        self.playwright = None

        # Кэш cookies для обеих платформ
        self._cache: Dict[str, CookieCache] = {}

        # Настройки TTL (в секундах)
        self.ttl = 3600  # 1 час по умолчанию
        self.refresh_threshold = 300  # Обновлять за 5 минут до истечения

        # Пути к файлам персистентности
        self.avito_cookie_file = Path("cookies.json")
        self.cian_cookie_file = Path("cookies_cian.json")

        # Блокировка для операций с браузером
        self._browser_lock = asyncio.Lock()

        # Фоновая задача для проактивного обновления
        self._refresh_task: Optional[asyncio.Task] = None

        # Количество активных мониторинг-задач
        self._active_monitors = 0

        # Phase 2: Сохранённый прокси (устанавливается при start())
        self._proxy: Optional[Proxy] = None

        # Cooldown: не пытаться обновить cookies после неудачи
        self._fetch_cooldown: dict = {}  # platform -> timestamp когда можно пробовать снова
        self._cooldown_duration = 300  # 5 минут между попытками обновления

        logger.info("CookieManager инициализирован (singleton)")

    @staticmethod
    def _parse_proxy(proxy_string: str) -> Optional[dict]:
        """
        Парсинг proxy_string в компоненты

        Args:
            proxy_string: Строка вида "ip:port:login:password" или "login:password@ip:port"

        Returns:
            Dict с ключами server, username, password или None
        """
        if not proxy_string:
            return None

        try:
            # Удаляем протокол если есть
            if "://" in proxy_string:
                proxy_string = proxy_string.split("://")[1]

            # Формат: login:password@ip:port или ip:port@login:password
            if "@" in proxy_string:
                left, right = proxy_string.split("@")
                # Определяем порядок: если в левой части есть точка — это IP (перевёрнутый формат)
                if "." in left:
                    # ip:port@login:password
                    ip_port = left
                    user_pass = right
                else:
                    # login:password@ip:port (стандартный)
                    user_pass = left
                    ip_port = right
                login, password = user_pass.split(":")
            else:
                # Формат: login:password:ip:port или ip:port:login:password
                parts = proxy_string.split(":")
                if len(parts) == 4:
                    # Определяем порядок по наличию точки в первой части (IP адрес)
                    if "." in parts[0]:
                        # ip:port:login:password
                        ip_port = f"{parts[0]}:{parts[1]}"
                        login, password = parts[2], parts[3]
                    else:
                        # login:password:ip:port
                        login, password = parts[0], parts[1]
                        ip_port = f"{parts[2]}:{parts[3]}"
                else:
                    logger.error(f"Неверный формат proxy_string: {proxy_string}")
                    return None

            # Добавляем протокол http:// если нет
            if not ip_port.startswith("http://") and not ip_port.startswith("https://"):
                ip_port = f"http://{ip_port}"

            return {
                "server": ip_port,
                "username": login,
                "password": password
            }

        except Exception as e:
            logger.error(f"Ошибка парсинга proxy_string: {e}")
            return None

    async def start(self, proxy: Optional[Proxy] = None, headless: bool = True):
        """
        Запуск браузера (ленивая инициализация)

        Args:
            proxy: Прокси для браузера
            headless: Запуск в headless режиме
        """
        async with self._browser_lock:
            # Phase 2: Проверяем, нужно ли перезапустить браузер из-за смены прокси
            if self.browser is not None:
                # Сравниваем текущий и новый прокси
                old_proxy_str = self._proxy.proxy_string if self._proxy else None
                new_proxy_str = proxy.proxy_string if proxy else None

                if old_proxy_str != new_proxy_str:
                    logger.info(f"Прокси изменился ({old_proxy_str} → {new_proxy_str}), перезапуск браузера...")
                    # Закрываем старый браузер
                    await self.browser.close()
                    self.browser = None
                    if self.playwright:
                        await self.playwright.stop()
                        self.playwright = None
                    if self.playwright_context:
                        await self.playwright_context.__aexit__(None, None, None)
                        self.playwright_context = None
                else:
                    logger.debug("Браузер уже запущен с тем же прокси")
                    return

            # Phase 2: Сохраняем прокси для использования в get_cookies()
            if proxy:
                self._proxy = proxy
                logger.info(f"CookieManager: прокси установлен ({proxy.proxy_string.split(':')[0]})")
            else:
                self._proxy = None
                logger.info("CookieManager: работаем без прокси")

            logger.info("Запуск Playwright браузера...")
            ensure_playwright_installed("chromium")

            from playwright_stealth import Stealth
            stealth = Stealth()
            self.playwright_context = stealth.use_async(async_playwright())
            self.playwright = await self.playwright_context.__aenter__()

            launch_args = {
                "headless": headless,
                "chromium_sandbox": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--start-maximized",
                    "--window-size=1920,1080",
                ]
            }

            # Phase 2: Добавляем прокси на уровне браузера (более надёжно для мобильных прокси)
            if self._proxy:
                # Парсим proxy_string для получения компонентов
                proxy_split = self._parse_proxy(self._proxy.proxy_string)
                if proxy_split:
                    launch_args["proxy"] = {
                        "server": proxy_split["server"],
                        "username": proxy_split["username"],
                        "password": proxy_split["password"]
                    }
                    logger.info(f"Браузер запускается с прокси: {proxy_split['server']}")

            self.browser = await self.playwright.chromium.launch(**launch_args)
            self._active_monitors += 1
            logger.info(f"Браузер запущен (активных мониторов: {self._active_monitors})")

            # Запуск фоновой задачи для проактивного обновления
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._background_refresh())

    async def stop(self):
        """Остановка браузера"""
        async with self._browser_lock:
            self._active_monitors = max(0, self._active_monitors - 1)

            if self._active_monitors > 0:
                logger.info(f"Браузер оставлен (активных мониторов: {self._active_monitors})")
                return

            logger.info("Остановка браузера...")

            # Остановка фоновой задачи
            if self._refresh_task and not self._refresh_task.done():
                self._refresh_task.cancel()
                try:
                    await self._refresh_task
                except asyncio.CancelledError:
                    pass

            if self.browser:
                await self.browser.close()
                self.browser = None

            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

            if self.playwright_context:
                await self.playwright_context.__aexit__(None, None, None)
                self.playwright_context = None

            logger.info("Браузер остановлен")

    async def get_cookies(
        self,
        platform: str,
        proxy: Optional[Proxy] = None,
        headless: bool = True,
        force_refresh: bool = False
    ) -> Tuple[dict, str]:
        """
        Получение cookies для платформы (Avito или Cian)

        Args:
            platform: "avito" или "cian"
            proxy: Прокси (опционально)
            headless: Headless режим
            force_refresh: Принудительное обновление cookies

        Returns:
            Tuple[cookies, user_agent]
        """
        if platform not in ["avito", "cian"]:
            raise ValueError(f"Неизвестная платформа: {platform}")

        # Phase 2: Используем сохранённый прокси если не передан явно
        if proxy is None and self._proxy is not None:
            proxy = self._proxy
            logger.debug(f"Используется сохранённый прокси для {platform}")

        # Проверка кэша
        if not force_refresh and platform in self._cache:
            cache = self._cache[platform]
            age = time.time() - cache.timestamp

            if age < self.ttl:
                logger.debug(f"Использую кэшированные cookies для {platform} (возраст: {age:.0f}с)")
                return cache.cookies, cache.user_agent

        # Обновление cookies
        async with self._lock:
            # Double-check после получения lock
            if not force_refresh and platform in self._cache:
                cache = self._cache[platform]
                age = time.time() - cache.timestamp
                if age < self.ttl:
                    return cache.cookies, cache.user_agent

            # Cooldown: не долбить сайт после неудачи
            cooldown_until = self._fetch_cooldown.get(platform, 0)
            if time.time() < cooldown_until:
                remaining = int(cooldown_until - time.time())
                logger.warning(
                    f"Cooldown для {platform}: следующая попытка через {remaining}с. "
                    f"Используем старые cookies."
                )
                return await self._load_from_disk(platform)

            logger.info(f"Обновление cookies для {platform}...")

            # Запуск браузера если не запущен
            if self.browser is None:
                await self.start(proxy=proxy, headless=headless)

            # Получение нового User-Agent
            from common_data import get_random_user_agent
            user_agent = get_random_user_agent()

            # Получение cookies через Playwright
            cookies = await self._fetch_cookies_from_browser(platform, user_agent, proxy)

            if cookies:
                # Успех — сбрасываем cooldown
                self._fetch_cooldown.pop(platform, None)

                # Сохранение в кэш
                self._cache[platform] = CookieCache(
                    cookies=cookies,
                    user_agent=user_agent,
                    timestamp=time.time(),
                    platform=platform
                )

                # Сохранение на диск
                await self._save_to_disk(platform, cookies, user_agent)

                logger.info(f"Cookies для {platform} обновлены (UA: {user_agent[:50]}...)")
                return cookies, user_agent
            else:
                # Неудача — ставим cooldown (5 минут)
                self._fetch_cooldown[platform] = time.time() + self._cooldown_duration
                logger.warning(
                    f"Не удалось получить cookies для {platform}. "
                    f"Cooldown {self._cooldown_duration}с. Используем старые cookies."
                )
                return await self._load_from_disk(platform)

    async def _fetch_cookies_from_browser(
        self,
        platform: str,
        user_agent: str,
        proxy: Optional[Proxy]
    ) -> dict:
        """Получение cookies через браузер"""
        from get_cookies import PlaywrightClient

        client = PlaywrightClient(
            browser=self.browser,
            proxy=proxy,
            headless=True,
            user_agent=user_agent
        )

        # URL для получения cookies (реальные страницы — не вызывают подозрений у антибота)
        if platform == "avito":
            url = "https://www.avito.ru/moskva"
        else:  # cian
            url = "https://www.cian.ru/"

        # Phase 2: Retry logic для ERR_NETWORK_CHANGED (типично для мобильных прокси)
        max_retries = 3
        retry_delay = 2

        for attempt in range(max_retries):
            context = None
            try:
                # Создание контекста и страницы
                context_args = {
                    "user_agent": user_agent,
                    "viewport": {"width": 1920, "height": 1080},
                    "screen": {"width": 1920, "height": 1080},
                    "device_scale_factor": 1,
                    "is_mobile": False,
                    "has_touch": False,
                }

                context = await self.browser.new_context(**context_args)
                client.context = context
                client.page = await context.new_page()
                await client._stealth(client.page)  # noqa: protected access — наш код

                # Загрузка страницы
                cookies = await client.load_page(url)

                if cookies:
                    logger.info(f"Cookies получены для {platform} (попытка {attempt + 1}/{max_retries})")
                    return cookies
                else:
                    logger.warning(f"Пустые cookies для {platform} (попытка {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    return {}

            except Exception as e:
                error_msg = str(e)

                is_network_error = any(err in error_msg for err in [
                    "ERR_NETWORK_CHANGED",
                    "ERR_PROXY_CONNECTION_FAILED",
                    "ERR_TUNNEL_CONNECTION_FAILED",
                    "ERR_CONNECTION_RESET",
                    "ERR_CONNECTION_TIMED_OUT"
                ])

                if is_network_error and attempt < max_retries - 1:
                    logger.warning(
                        f"Сетевая ошибка при получении cookies для {platform} "
                        f"(попытка {attempt + 1}/{max_retries}): {error_msg}. "
                        f"Повтор через {retry_delay}с..."
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"Ошибка получения cookies для {platform} (попытка {attempt + 1}/{max_retries}): {e}")
                    return {}

            finally:
                # Закрытие только контекста, браузер остается
                if context:
                    await context.close()

        return {}

    async def _save_to_disk(self, platform: str, cookies: dict, user_agent: str):
        """Сохранение cookies на диск"""
        file_path = self.avito_cookie_file if platform == "avito" else self.cian_cookie_file

        data = {
            "cookies": cookies,
            "user_agent": user_agent,
            "timestamp": time.time(),
            "platform": platform
        }

        try:
            import fcntl
            with open(file_path, 'w') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                json.dump(data, f, indent=2)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            logger.debug(f"Cookies сохранены на диск: {file_path}")
        except Exception as e:
            logger.warning(f"Не удалось сохранить cookies на диск: {e}")

    async def _load_from_disk(self, platform: str) -> Tuple[dict, str]:
        """Загрузка cookies с диска"""
        file_path = self.avito_cookie_file if platform == "avito" else self.cian_cookie_file

        if not file_path.exists():
            logger.debug(f"Файл cookies не найден: {file_path}")
            return {}, ""

        try:
            import fcntl
            with open(file_path, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            cookies = data.get("cookies", {})
            user_agent = data.get("user_agent", "")
            timestamp = data.get("timestamp", 0)

            # Проверка TTL
            age = time.time() - timestamp
            if age > self.ttl:
                logger.warning(f"Cookies с диска устарели (возраст: {age:.0f}с)")
                return {}, ""

            # Сохранение в кэш
            self._cache[platform] = CookieCache(
                cookies=cookies,
                user_agent=user_agent,
                timestamp=timestamp,
                platform=platform
            )

            logger.info(f"Cookies загружены с диска: {file_path}")
            return cookies, user_agent

        except Exception as e:
            logger.error(f"Ошибка загрузки cookies с диска: {e}")
            return {}, ""

    async def _background_refresh(self):
        """Фоновая задача для проактивного обновления cookies"""
        logger.info("Запущена фоновая задача обновления cookies")

        while True:
            try:
                await asyncio.sleep(60)  # Проверка каждую минуту

                for platform in ["avito", "cian"]:
                    if platform not in self._cache:
                        continue

                    cache = self._cache[platform]
                    age = time.time() - cache.timestamp
                    time_until_expiry = self.ttl - age

                    # Обновление если осталось меньше порога
                    if time_until_expiry < self.refresh_threshold:
                        logger.info(
                            f"Проактивное обновление cookies для {platform} "
                            f"(до истечения: {time_until_expiry:.0f}с)"
                        )
                        await self.get_cookies(platform, force_refresh=True)

            except asyncio.CancelledError:
                logger.info("Фоновая задача обновления cookies остановлена")
                break
            except Exception as e:
                logger.error(f"Ошибка в фоновой задаче обновления: {e}")

    def get_cache_info(self) -> dict:
        """Получение информации о кэше для мониторинга"""
        info = {}
        for platform, cache in self._cache.items():
            age = time.time() - cache.timestamp
            info[platform] = {
                "age_seconds": age,
                "ttl_seconds": self.ttl,
                "time_until_refresh": max(0, self.ttl - self.refresh_threshold - age),
                "user_agent": cache.user_agent[:50] + "...",
                "cached_at": datetime.fromtimestamp(cache.timestamp).isoformat()
            }

        info["browser_running"] = self.browser is not None
        info["active_monitors"] = self._active_monitors

        return info


# Singleton instance
cookie_manager = CookieManager()

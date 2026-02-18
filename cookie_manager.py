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


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

@dataclass
class CookieCache:
    """Кэш cookies с метаданными"""
    cookies: dict
    user_agent: str
    timestamp: float
    platform: str  # "avito" или "cian"


class CookieManager:
    _instance: Optional["CookieManager"] = None
    _instance_lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return

        self._initialized = True

        self.browser: Optional[Browser] = None
        self.playwright_context = None

        self._browser_lock = asyncio.Lock()
        self._lock = asyncio.Lock()

        self._cache: Dict[str, CookieCache] = {}

        self.ttl = 3600
        self.refresh_threshold = 300

        self._proxy: Optional[Proxy] = None

        self._clients = 0

        self._refresh_task: Optional[asyncio.Task] = None

        self._fetch_cooldown: dict = {}
        self._cooldown_duration = 30

        self.avito_cookie_file = Path("cookies.json")
        self.cian_cookie_file = Path("cookies_cian.json")

        logger.info("CookieManager инициализирован")

    # ------------------------------------------------

    async def acquire(self):
        async with self._browser_lock:
            self._clients += 1
            if self.browser is None:
                await self._start_browser()

    async def release(self):
        async with self._browser_lock:
            self._clients = max(0, self._clients - 1)

            if self._clients == 0:
                await self._stop_browser()

    # ------------------------------------------------

    async def _start_browser(self):
        ensure_playwright_installed("chromium")

        from playwright_stealth import Stealth

        stealth = Stealth()
        self.playwright_context = stealth.use_async(async_playwright())
        playwright = await self.playwright_context.__aenter__()

        launch_args = {
            "headless": True,
            "chromium_sandbox": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }

        if self._proxy:
            from get_cookies import PlaywrightClient

            proxy_data = PlaywrightClient(proxy=self._proxy).get_proxy_obj()

            if proxy_data:
                launch_args["proxy"] = {
                    "server": proxy_data.ip_port,
                    "username": proxy_data.login,
                    "password": proxy_data.password,
                }

        self.browser = await playwright.chromium.launch(**launch_args)

        if not self._refresh_task:
            self._refresh_task = asyncio.create_task(self._background_refresh())

        logger.info("Браузер запущен")

    async def _stop_browser(self):
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None

        if self.browser:
            await self.browser.close()
            self.browser = None

        if self.playwright_context:
            await self.playwright_context.__aexit__(None, None, None)
            self.playwright_context = None

        logger.info("Браузер остановлен")

    # ------------------------------------------------

    async def get_cookies(
        self,
        platform: str,
        proxy: Optional[Proxy] = None,
        force_refresh: bool = False,
    ) -> Tuple[dict, str]:

        if proxy:
            self._proxy = proxy

        if not force_refresh and platform in self._cache:
            cache = self._cache[platform]
            if time.time() - cache.timestamp < self.ttl:
                return cache.cookies, cache.user_agent

        async with self._lock:

            if not force_refresh and platform in self._cache:
                cache = self._cache[platform]
                if time.time() - cache.timestamp < self.ttl:
                    return cache.cookies, cache.user_agent

            cooldown_until = self._fetch_cooldown.get(platform, 0)
            if time.time() < cooldown_until:
                return await self._fallback(platform)

            await self.acquire()

            try:
                cookies = await self._fetch(platform)

                if cookies:
                    self._fetch_cooldown.pop(platform, None)

                    self._cache[platform] = CookieCache(
                        cookies=cookies,
                        user_agent=USER_AGENT,
                        timestamp=time.time(),
                        platform=platform,
                    )

                    await self._save_to_disk(platform, cookies)

                    return cookies, USER_AGENT

                self._fetch_cooldown[platform] = (
                    time.time() + self._cooldown_duration
                )

                return await self._fallback(platform)

            finally:
                await self.release()

    # ------------------------------------------------

    async def _fetch(self, platform: str) -> dict:
        from get_cookies import PlaywrightClient

        url = (
            "https://www.avito.ru/moskva"
            if platform == "avito"
            else "https://www.cian.ru/"
        )

        client = PlaywrightClient(
            browser=self.browser,
            proxy=self._proxy,
            user_agent=USER_AGENT,
        )

        context = await self.browser.new_context(user_agent=USER_AGENT)
        client.context = context
        client.page = await context.new_page()
        await client._stealth(client.page)

        try:
            return await client.load_page(url)
        finally:
            await context.close()

    # ------------------------------------------------

    async def _background_refresh(self):
        while True:
            try:
                await asyncio.sleep(60)

                if not self.browser:
                    continue

                for platform in list(self._cache.keys()):
                    age = time.time() - self._cache[platform].timestamp
                    if self.ttl - age < self.refresh_threshold:
                        await self.get_cookies(platform, force_refresh=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"background refresh error: {e}")

    # ------------------------------------------------

    async def _fallback(self, platform: str) -> Tuple[dict, str]:
        if platform in self._cache:
            c = self._cache[platform]
            return c.cookies, c.user_agent

        return await self._load_from_disk(platform)

    # ------------------------------------------------

    async def _save_to_disk(self, platform: str, cookies: dict):
        path = (
            self.avito_cookie_file
            if platform == "avito"
            else self.cian_cookie_file
        )

        data = {
            "cookies": cookies,
            "user_agent": USER_AGENT,
            "timestamp": time.time(),
        }

        with open(path, "w") as f:
            json.dump(data, f)

    async def _load_from_disk(self, platform: str) -> Tuple[dict, str]:
        path = (
            self.avito_cookie_file
            if platform == "avito"
            else self.cian_cookie_file
        )

        if not path.exists():
            return {}, ""

        with open(path) as f:
            data = json.load(f)

        if time.time() - data["timestamp"] > self.ttl:
            return {}, ""

        return data["cookies"], data["user_agent"]

    # ------------------------------------------------

    def get_cache_info(self):
        info = {}

        for p, c in self._cache.items():
            age = time.time() - c.timestamp
            info[p] = {
                "age": age,
                "expires_in": self.ttl - age,
                "cached_at": datetime.fromtimestamp(c.timestamp).isoformat(),
            }

        info["browser_running"] = self.browser is not None
        info["clients"] = self._clients

        return info


cookie_manager = CookieManager()

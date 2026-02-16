import asyncio
import random
from typing import Optional

import httpx
from loguru import logger
from playwright.async_api import async_playwright, Browser
from playwright_stealth import Stealth

from dto import Proxy, ProxySplit
from playwright_setup import ensure_playwright_installed

MAX_RETRIES = 3
RETRY_DELAY = 10
RETRY_DELAY_WITHOUT_PROXY = 300
BAD_IP_TITLE = "проблема с ip"


class PlaywrightClient:
    def __init__(
        self,
        browser: Browser | None = None,
        proxy: Proxy | None = None,
        headless: bool = True,
        user_agent: Optional[str] = None,
        stop_event=None,
    ):
        self.browser = browser
        self.proxy = proxy
        self.proxy_split_obj = self.get_proxy_obj()
        self.headless = headless
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )
        self.context = None
        self.page = None
        self.stop_event = stop_event

        self.playwright = None
        self.playwright_context = None

    # ---------------- PROXY ---------------- #

    @staticmethod
    def check_protocol(ip_port: str) -> str:
        return ip_port if "http://" in ip_port else f"http://{ip_port}"

    @staticmethod
    def del_protocol(proxy_string: str):
        return proxy_string.split("//")[1] if "//" in proxy_string else proxy_string

    def get_proxy_obj(self) -> ProxySplit | None:
        if not self.proxy:
            return None

        try:
            self.proxy.proxy_string = self.del_protocol(self.proxy.proxy_string)

            if "@" in self.proxy.proxy_string:
                ip_port, user_pass = self.proxy.proxy_string.split("@")
                if "." in user_pass:
                    ip_port, user_pass = user_pass, ip_port
                login, password = user_pass.split(":")
            else:
                login, password, ip, port = self.proxy.proxy_string.split(":")
                if "." in login:
                    login, password, ip, port = ip, port, login, password
                ip_port = f"{ip}:{port}"

            return ProxySplit(
                ip_port=self.check_protocol(ip_port),
                login=login,
                password=password,
                change_ip_link=self.proxy.change_ip_link,
            )
        except Exception as err:
            logger.error(err)
            logger.critical("Неверный формат прокси")

    # ---------------- BROWSER ---------------- #

    async def launch_browser(self):
        if self.browser is None:
            ensure_playwright_installed("chromium")

            stealth = Stealth()
            self.playwright_context = stealth.use_async(async_playwright())
            self.playwright = await self.playwright_context.__aenter__()

            launch_args = {
                "headless": self.headless,
                "chromium_sandbox": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--start-maximized",
                ],
            }

            if self.proxy_split_obj:
                launch_args["proxy"] = {
                    "server": self.proxy_split_obj.ip_port,
                    "username": self.proxy_split_obj.login,
                    "password": self.proxy_split_obj.password,
                }

            self.browser = await self.playwright.chromium.launch(**launch_args)

        self.context = await self.browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
        )

        self.page = await self.context.new_page()
        await self._stealth(self.page)

    # ---------------- COOKIES ---------------- #

    @staticmethod
    def parse_cookie_string(cookie_str: str) -> dict:
        return dict(
            pair.split("=", 1)
            for pair in cookie_str.split("; ")
            if "=" in pair
        )

    async def load_page(self, url: str):
        await self.page.goto(url, timeout=120_000, wait_until="domcontentloaded")

        for _ in range(10):
            if self.stop_event and self.stop_event.is_set():
                return {}

            await self.check_block()

            raw_cookie = await self.page.evaluate("() => document.cookie")
            cookie_dict = self.parse_cookie_string(raw_cookie)

            if cookie_dict:
                logger.info("Cookies получены")
                return cookie_dict

            await asyncio.sleep(5)

        logger.warning("Не удалось получить cookies")
        return {}

    async def extract_cookies(self, url: str):
        external_browser = self.browser is not None

        try:
            await self.launch_browser()
            return await self.load_page(url)

        finally:
            if self.context:
                await self.context.close()

            if not external_browser and self.browser:
                await self.browser.close()

            if self.playwright_context:
                await self.playwright_context.__aexit__(None, None, None)

    async def get_cookies(self, url: str):
        return await self.extract_cookies(url)

    # ---------------- BLOCK / IP ---------------- #

    async def check_block(self):
        title = await self.page.title()

        if BAD_IP_TITLE in title.lower():
            logger.warning("IP заблокирован")

            await self.context.clear_cookies()
            await self.change_ip()

            await self.page.reload(timeout=120_000)

    async def change_ip(self, retries: int = MAX_RETRIES):
        if not self.proxy_split_obj:
            logger.warning("Прокси не настроен")
            await asyncio.sleep(RETRY_DELAY_WITHOUT_PROXY)
            return False

        async with httpx.AsyncClient(timeout=20) as client:
            for attempt in range(1, retries + 1):
                try:
                    r = await client.get(
                        self.proxy_split_obj.change_ip_link + "&format=json"
                    )

                    if r.status_code == 200:
                        logger.info(f"IP изменён: {r.json().get('new_ip')}")
                        return True

                except Exception as e:
                    logger.error(e)

                await asyncio.sleep(RETRY_DELAY)

        return False

    # ---------------- STEALTH ---------------- #

    @staticmethod
    async def _stealth(page):
        await page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """
        )


# ---------------- PUBLIC API ---------------- #

async def get_cookies(proxy: Proxy = None, headless: bool = True, stop_event=None):
    logger.info("Пытаюсь обновить cookies")

    client = PlaywrightClient(
        proxy=proxy,
        headless=headless,
        stop_event=stop_event,
    )

    cookies = await client.get_cookies("https://www.avito.ru")
    return cookies, client.user_agent

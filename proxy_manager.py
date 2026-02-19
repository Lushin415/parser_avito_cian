"""
ProxyManager — централизованная координация ротации IP между мониторами.

Гарантирует:
  - Только одна ротация IP за раз (asyncio.Lock)
  - Все воркеры блокируются во время ротации/cooldown (asyncio.Event)
  - Плавное возобновление с jitter (thundering herd protection)
  - Circuit breaker после MAX_ROTATION_ATTEMPTS неудач подряд (→ FAILED)

Состояния:
  ACTIVE   → воркеры работают нормально
  ROTATING → идёт смена IP, воркеры заблокированы
  COOLDOWN → IP сменён, ждём перед запуском воркеров
  FAILED   → все попытки исчерпаны, нужно ручное вмешательство
"""
import asyncio
import random
from enum import Enum
from typing import Optional

import httpx
from loguru import logger

from dto import Proxy, ProxySplit


# ─── Константы ───────────────────────────────────────────────────────────────

MAX_ROTATION_ATTEMPTS = 3     # попыток ротации до перехода в FAILED
ROTATION_RETRY_DELAY = 10     # сек между попытками ротации внутри одного handle_block
COOLDOWN_DURATION = 60        # сек cooldown после успешной ротации
LOCK_TIMEOUT = 180            # сек: если ротация зависла дольше — принудительный сброс
JITTER_MAX = 30               # сек: максимальный jitter при возобновлении воркеров


# ─── Состояния ───────────────────────────────────────────────────────────────

class ProxyState(Enum):
    ACTIVE   = "active"
    ROTATING = "rotating"
    COOLDOWN = "cooldown"
    FAILED   = "failed"


# ─── ProxyManager ─────────────────────────────────────────────────────────────

class ProxyManager:
    """Singleton. Координирует доступ к прокси между avito_monitor и cian_monitor."""

    _instance: Optional["ProxyManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self._lock = asyncio.Lock()
        self._ready_event = asyncio.Event()
        self._ready_event.set()           # изначально воркеры могут работать

        self._state = ProxyState.ACTIVE
        self._consecutive_failures = 0
        self._proxy: Optional[Proxy] = None

        logger.info("ProxyManager инициализирован")

    # ─── Настройка ────────────────────────────────────────────────────────── #

    def configure(self, proxy: Optional[Proxy]) -> None:
        """Установить прокси. Вызывается при старте мониторов."""
        self._proxy = proxy
        if proxy:
            logger.info("ProxyManager: прокси настроен")
        else:
            logger.info("ProxyManager: прокси не настроен, ротация недоступна")

    # ─── Worker API ───────────────────────────────────────────────────────── #

    async def wait_if_not_ready(self) -> bool:
        """
        Вызывается воркером перед каждым HTTP-запросом к Avito/Cian.

        - Блокирует, если идёт ротация или cooldown.
        - После разблокировки добавляет случайный jitter (thundering herd protection).
        - Возвращает False, если прокси в состоянии FAILED — запрос делать не нужно.
        """
        if self._state == ProxyState.FAILED:
            return False

        if not self._ready_event.is_set():
            logger.debug("ProxyManager: воркер ожидает завершения ротации...")
            await self._ready_event.wait()
            # Проверяем сразу после пробуждения — FAILED не требует jitter
            if self._state == ProxyState.FAILED:
                return False
            # Jitter: распределяем возобновление воркеров по времени
            jitter = random.uniform(0, JITTER_MAX)
            logger.debug(f"ProxyManager: воркер получил сигнал, jitter {jitter:.1f}с")
            await asyncio.sleep(jitter)

        return self._state != ProxyState.FAILED

    # ─── Monitor API ──────────────────────────────────────────────────────── #

    async def handle_block(self, platform: str, url_list: list) -> None:
        """
        Вызывается монитором при обнаружении 403/429.

        - Первый монитор, вызвавший этот метод, захватывает lock и выполняет ротацию.
        - Второй монитор (если вызвал одновременно) просто ждёт завершения.
        """
        if self._state == ProxyState.FAILED:
            logger.warning(
                f"{platform.upper()}: блокировка обнаружена, но прокси в FAILED — пропускаем"
            )
            return

        # Если lock уже занят — другой монитор выполняет ротацию, просто ждём
        if self._lock.locked():
            logger.info(
                f"{platform.upper()}: ротация уже выполняется другим монитором, ожидаем..."
            )
            await self._ready_event.wait()
            return

        # Пытаемся взять lock и выполнить ротацию
        try:
            await asyncio.wait_for(
                self._rotate(platform, url_list),
                timeout=LOCK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self._consecutive_failures += 1
            logger.error(
                f"ProxyManager: ротация превысила таймаут ({LOCK_TIMEOUT}с) "
                f"({self._consecutive_failures}/{MAX_ROTATION_ATTEMPTS})"
            )
            if self._consecutive_failures >= MAX_ROTATION_ATTEMPTS:
                self._state = ProxyState.FAILED
                logger.critical(
                    "ProxyManager: FAILED после таймаута — требуется ручное вмешательство"
                )
                await self._notify_failed(url_list)
            else:
                self._state = ProxyState.ACTIVE
                self._ready_event.set()

    # ─── Internal ─────────────────────────────────────────────────────────── #

    async def _rotate(self, platform: str, url_list: list) -> None:
        """Захватить lock, выполнить ротацию; cooldown — уже за пределами lock."""
        do_cooldown = False

        async with self._lock:
            # Double-check: пока ждали lock, другой монитор мог уже всё сделать
            if self._state in (ProxyState.ROTATING, ProxyState.COOLDOWN):
                logger.debug(
                    f"{platform.upper()}: ротация уже была запущена, ожидаем сигнала"
                )
                await self._ready_event.wait()
                return

            self._state = ProxyState.ROTATING
            self._ready_event.clear()  # блокируем всех воркеров
            logger.warning(
                f"ProxyManager: [{platform.upper()}] начало ротации IP — "
                f"все воркеры остановлены"
            )

            success = await self._do_rotate()

            if success:
                self._consecutive_failures = 0
                self._state = ProxyState.COOLDOWN
                do_cooldown = True
                # lock освобождается здесь; cooldown идёт без него ↓
            else:
                self._consecutive_failures += 1
                logger.error(
                    f"ProxyManager: ротация не удалась "
                    f"({self._consecutive_failures}/{MAX_ROTATION_ATTEMPTS})"
                )
                if self._consecutive_failures >= MAX_ROTATION_ATTEMPTS:
                    self._state = ProxyState.FAILED
                    logger.critical(
                        "ProxyManager: FAILED — все попытки исчерпаны, "
                        "требуется ручное вмешательство"
                    )
                    await self._notify_failed(url_list)
                    # _ready_event остаётся cleared — воркеры не запустятся
                else:
                    # Ещё есть попытки — возобновляем, следующий бан запустит новую ротацию
                    self._state = ProxyState.ACTIVE
                    self._ready_event.set()

        # Cooldown без lock: другие handle_block уже могут входить,
        # но воркеры всё ещё заблокированы через _ready_event (state=COOLDOWN).
        if do_cooldown:
            logger.info(f"ProxyManager: IP сменён, cooldown {COOLDOWN_DURATION}с")
            await asyncio.sleep(COOLDOWN_DURATION)
            self._state = ProxyState.ACTIVE
            self._ready_event.set()  # воркеры стартуют с jitter (в wait_if_not_ready)
            logger.success(
                f"ProxyManager: ротация завершена, воркеры возобновляются "
                f"(jitter до {JITTER_MAX}с)"
            )

    async def _do_rotate(self) -> bool:
        """Вызвать proxy_change_url и верифицировать новый IP."""
        if not self._proxy:
            logger.warning("ProxyManager: прокси не настроен, пропускаем ротацию")
            return False

        change_url = self._proxy.change_ip_link
        if "format=json" not in change_url:
            change_url = change_url + "&format=json"

        async with httpx.AsyncClient(timeout=20) as client:
            for attempt in range(1, MAX_ROTATION_ATTEMPTS + 1):
                try:
                    r = await client.get(change_url)
                    if r.status_code == 200:
                        try:
                            new_ip = r.json().get("new_ip", "unknown")
                        except Exception:
                            new_ip = "unknown"
                        logger.info(f"ProxyManager: смена IP запрошена → новый IP: {new_ip}")

                        if await self._check_proxy_alive():
                            logger.success("ProxyManager: прокси отвечает после смены IP")
                            return True

                        logger.warning(
                            "ProxyManager: IP сменён, но прокси не отвечает — "
                            "возможно истекла подписка"
                        )
                    else:
                        logger.error(
                            f"ProxyManager: попытка {attempt} — "
                            f"change_ip_link вернул {r.status_code}"
                        )
                except Exception as e:
                    logger.error(f"ProxyManager: попытка {attempt} — ошибка: {e}")

                if attempt < MAX_ROTATION_ATTEMPTS:
                    logger.info(f"ProxyManager: повтор через {ROTATION_RETRY_DELAY}с...")
                    await asyncio.sleep(ROTATION_RETRY_DELAY)

        return False

    async def _check_proxy_alive(self) -> bool:
        """Проверить доступность прокси через внешний IP-сервис."""
        proxy_split = self._parse_proxy()
        if not proxy_split:
            return False
        try:
            async with httpx.AsyncClient(
                proxy=proxy_split.ip_port,
                auth=(proxy_split.login, proxy_split.password),
                timeout=10,
            ) as client:
                r = await client.get("http://api.ipify.org")
                return r.status_code == 200
        except Exception as e:
            logger.warning(f"ProxyManager: прокси не отвечает: {e}")
            return False

    def _parse_proxy(self) -> Optional[ProxySplit]:
        """
        Разобрать proxy_string в ProxySplit.
        Поддерживает форматы (аналогично PlaywrightClient.get_proxy_obj):
          user:pass@host:port
          host:port@user:pass
          user:pass:host:port
          host:port:user:pass
        """
        if not self._proxy or not self._proxy.proxy_string:
            return None
        try:
            proxy_str = self._proxy.proxy_string
            if "//" in proxy_str:
                proxy_str = proxy_str.split("//")[1]

            if "@" in proxy_str:
                ip_port, user_pass = proxy_str.split("@")
                if "." in user_pass:   # host:port@user:pass → swap
                    ip_port, user_pass = user_pass, ip_port
                login, password = user_pass.split(":")
            else:
                login, password, ip, port = proxy_str.split(":")
                if "." in login:       # ip:port:user:pass → swap
                    login, password, ip, port = ip, port, login, password
                ip_port = f"{ip}:{port}"

            if "http://" not in ip_port:
                ip_port = f"http://{ip_port}"

            return ProxySplit(
                ip_port=ip_port,
                login=login,
                password=password,
                change_ip_link=self._proxy.change_ip_link,
            )
        except Exception as e:
            logger.error(f"ProxyManager: ошибка парсинга proxy_string: {e}")
            return None

    async def _notify_failed(self, url_list: list) -> None:
        """Уведомить администратора о переходе в FAILED."""
        cfg = next(
            (
                u["config"]
                for u in url_list
                if u.get("config", {}).get("tg_token")
                and u.get("config", {}).get("pause_chat_id")
            ),
            None,
        )
        if not cfg:
            logger.critical(
                "ProxyManager: FAILED, но нет конфига для уведомления администратора!"
            )
            return

        text = (
            "🔴 <b>Прокси полностью недоступен</b>\n\n"
            f"После {MAX_ROTATION_ATTEMPTS} попыток смены IP — все завершились неудачей.\n\n"
            "Мониторинг <b>остановлен</b> до ручного вмешательства.\n\n"
            "Проверьте:\n"
            "• Баланс мобильного прокси\n"
            "• Доступность proxy_change_url\n"
            "• Настройки прокси в конфиге\n\n"
            "После устранения — перезапустите сервис."
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(
                    f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage",
                    json={
                        "chat_id": cfg["pause_chat_id"],
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
                logger.info("ProxyManager: уведомление FAILED отправлено администратору")
            except Exception as e:
                logger.error(f"ProxyManager: не удалось отправить уведомление: {e}")

    # ─── Ручное управление ────────────────────────────────────────────────── #

    def reset_failed(self, proxy: Optional[Proxy] = None) -> None:
        """
        Сброс состояния FAILED после устранения проблемы с прокси.
        Опционально обновляет настройки прокси.
        """
        if proxy:
            self._proxy = proxy
        self._consecutive_failures = 0
        self._state = ProxyState.ACTIVE
        self._ready_event.set()
        logger.info("ProxyManager: состояние сброшено в ACTIVE, мониторинг возобновлён")

    # ─── Статус ───────────────────────────────────────────────────────────── #

    @property
    def state(self) -> ProxyState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state == ProxyState.ACTIVE

    def get_status(self) -> dict:
        return {
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "proxy_configured": self._proxy is not None,
            "is_ready": self.is_ready,
        }


# ─── Singleton ────────────────────────────────────────────────────────────── #

proxy_manager = ProxyManager()

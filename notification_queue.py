"""
Phase 3: Notification Queue

Контролируемая доставка уведомлений в Telegram с rate limiting.

Архитектура:
- asyncio.PriorityQueue для сообщений с приоритетами
- Consumer coroutine: отправка ~28 msg/sec (35мс между сообщениями)
- Приоритеты: 0 = системные/ошибки, 1 = объявления
- Retry на 429 (Telegram flood wait)
- Graceful shutdown (дождаться отправки или таймаут 10с)
"""
import asyncio
import time
from typing import Optional, Union, List
from dataclasses import dataclass, field
from loguru import logger

import requests as http_requests

from models import Item
from cian_models import CianItem
from tg_sender import SendAdToTg


# Константы
TELEGRAM_RATE_LIMIT_INTERVAL = 0.035  # 35мс = ~28 msg/sec (лимит TG: 30/sec)
MAX_QUEUE_SIZE = 1000
MAX_RETRIES = 3
GRACEFUL_SHUTDOWN_TIMEOUT = 10  # секунд

# Приоритеты
PRIORITY_SYSTEM = 0  # Системные сообщения, ошибки
PRIORITY_AD = 1      # Объявления


@dataclass(order=True)
class NotificationItem:
    """Элемент очереди уведомлений"""
    priority: int
    timestamp: float = field(compare=False)
    data: dict = field(compare=False)


class NotificationQueue:
    """
    Очередь уведомлений с rate limiting для Telegram API.

    Singleton — один экземпляр на всё приложение.
    """
    _instance: Optional['NotificationQueue'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=MAX_QUEUE_SIZE)
        self.running = False
        self._consumer_task: Optional[asyncio.Task] = None

        # Метрики
        self.sent_count = 0
        self.failed_count = 0
        self.dropped_count = 0
        self.retry_count = 0
        self._last_send_time = 0.0
        self._start_time = 0.0

        self._initialized = True
        logger.info("NotificationQueue инициализирован (singleton)")

    async def start(self):
        """Запуск consumer coroutine"""
        if self.running:
            logger.warning("NotificationQueue уже запущен")
            return

        self.running = True
        self._start_time = time.time()
        self._consumer_task = asyncio.create_task(self._consumer_loop())
        logger.success("NotificationQueue запущен (rate limit: ~28 msg/sec)")

    async def stop(self):
        """Graceful shutdown — дождаться отправки оставшихся или таймаут"""
        if not self.running:
            return

        self.running = False
        remaining = self.queue.qsize()

        if remaining > 0:
            logger.info(
                f"NotificationQueue: ожидание отправки {remaining} сообщений "
                f"(таймаут {GRACEFUL_SHUTDOWN_TIMEOUT}с)..."
            )

            # Ждём опустошения очереди или таймаут
            try:
                await asyncio.wait_for(
                    self._drain_queue(),
                    timeout=GRACEFUL_SHUTDOWN_TIMEOUT
                )
                logger.info("NotificationQueue: все сообщения отправлены")
            except asyncio.TimeoutError:
                lost = self.queue.qsize()
                logger.warning(f"NotificationQueue: таймаут, потеряно {lost} сообщений")

        # Отменяем consumer
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        logger.info(
            f"NotificationQueue остановлен "
            f"(отправлено: {self.sent_count}, ошибок: {self.failed_count}, "
            f"дропнуто: {self.dropped_count})"
        )

    async def _drain_queue(self):
        """Отправка оставшихся сообщений из очереди"""
        while not self.queue.empty():
            item = await self.queue.get()
            await self._process_item(item)
            self.queue.task_done()

    async def enqueue_ad(
        self,
        ad: Union[Item, CianItem],
        user_config: dict,
        platform: str
    ):
        """
        Добавить объявление в очередь уведомлений.

        Args:
            ad: Объявление (Avito Item или CianItem)
            user_config: Конфиг пользователя (tg_token, tg_chat_id, ...)
            platform: "avito" или "cian"
        """
        tg_token = user_config.get("tg_token")
        tg_chat_id = user_config.get("tg_chat_id")

        if not tg_token or not tg_chat_id:
            return

        data = {
            "type": "ad",
            "ad": ad,
            "bot_token": tg_token,
            "chat_ids": tg_chat_id,
            "platform": platform
        }

        await self._put_to_queue(PRIORITY_AD, data)

    async def enqueue_system_message(
        self,
        msg: str,
        bot_token: str,
        chat_ids: List[str]
    ):
        """
        Добавить системное сообщение (приоритет 0 — отправляется первым).

        Args:
            msg: Текст сообщения
            bot_token: Токен бота
            chat_ids: Список chat_id
        """
        data = {
            "type": "system",
            "msg": msg,
            "bot_token": bot_token,
            "chat_ids": chat_ids,
        }

        await self._put_to_queue(PRIORITY_SYSTEM, data)

    async def _put_to_queue(self, priority: int, data: dict):
        """Добавление элемента в очередь с обработкой переполнения"""
        item = NotificationItem(
            priority=priority,
            timestamp=time.time(),
            data=data
        )

        if self.queue.full():
            # Overflow: дропаем самое старое объявление (не системные!)
            logger.warning(
                f"NotificationQueue переполнена ({MAX_QUEUE_SIZE}), "
                f"дропаем старое уведомление"
            )
            self.dropped_count += 1

            # Пытаемся убрать элемент чтобы освободить место
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                pass

        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.error("NotificationQueue: не удалось добавить в очередь после дропа")
            self.dropped_count += 1

    async def _consumer_loop(self):
        """Основной цикл consumer — забирает из очереди и отправляет"""
        logger.info("NotificationQueue consumer запущен")

        while self.running or not self.queue.empty():
            try:
                # Ждём элемент из очереди с таймаутом (чтобы проверять self.running)
                try:
                    item = await asyncio.wait_for(
                        self.queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                await self._process_item(item)
                self.queue.task_done()

            except asyncio.CancelledError:
                logger.info("NotificationQueue consumer: получен сигнал отмены")
                break
            except Exception as e:
                logger.error(f"NotificationQueue consumer: неожиданная ошибка: {e}")
                await asyncio.sleep(1)

        logger.info("NotificationQueue consumer остановлен")

    async def _process_item(self, item: NotificationItem):
        """Обработка одного элемента очереди"""
        data = item.data
        chat_ids = data.get("chat_ids", [])
        bot_token = data.get("bot_token")

        if not bot_token or not chat_ids:
            return

        for chat_id in chat_ids:
            success = await self._send_single(chat_id, data)

            if not success:
                self.failed_count += 1
            else:
                self.sent_count += 1

            # Rate limiting: пауза между сообщениями
            await asyncio.sleep(TELEGRAM_RATE_LIMIT_INTERVAL)

    async def _send_single(self, chat_id: str, data: dict) -> bool:
        """
        Отправка одного сообщения в один chat_id с retry логикой.

        Returns:
            True если отправлено успешно
        """
        bot_token = data["bot_token"]

        for attempt in range(MAX_RETRIES):
            try:
                response = await asyncio.to_thread(
                    self._do_send, chat_id, bot_token, data
                )

                if response.status_code == 200:
                    self._last_send_time = time.time()
                    return True

                elif response.status_code == 429:
                    # Telegram flood wait — ждём указанное время
                    try:
                        retry_after = response.json().get(
                            "parameters", {}
                        ).get("retry_after", 5)
                    except Exception:
                        retry_after = 5

                    logger.warning(
                        f"TG 429 (flood): ожидание {retry_after}с "
                        f"(попытка {attempt + 1}/{MAX_RETRIES})"
                    )
                    self.retry_count += 1
                    await asyncio.sleep(retry_after)

                elif response.status_code in (400, 401, 403, 404):
                    # Клиентские ошибки — не ретраим (невалидный токен, chat_id и т.д.)
                    try:
                        error_detail = response.json()
                        logger.error(
                            f"TG {response.status_code} (пропускаем): "
                            f"{error_detail.get('description', 'unknown')}"
                        )
                    except Exception:
                        logger.error(f"TG {response.status_code}: {response.text[:200]}")
                    return False

                else:
                    # Прочие ошибки — exponential backoff
                    wait_time = 1 * (2 ** attempt)
                    logger.warning(
                        f"TG {response.status_code}: retry через {wait_time}с "
                        f"(попытка {attempt + 1}/{MAX_RETRIES})"
                    )
                    self.retry_count += 1
                    await asyncio.sleep(wait_time)

            except Exception as e:
                wait_time = 1 * (2 ** attempt)
                logger.error(
                    f"Ошибка отправки TG (попытка {attempt + 1}/{MAX_RETRIES}): {e}"
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait_time)

        return False

    def _do_send(self, chat_id: str, bot_token: str, data: dict) -> http_requests.Response:
        """
        Синхронная отправка одного сообщения (вызывается через asyncio.to_thread).

        Использует SendAdToTg для форматирования, но отправляет напрямую.
        """
        msg_type = data.get("type", "ad")

        if msg_type == "system":
            # Системное сообщение (простой текст)
            payload = {
                "chat_id": chat_id,
                "text": data["msg"],
                "parse_mode": "MarkdownV2",
            }
            return http_requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
                timeout=10
            )

        # Объявление — используем форматирование из SendAdToTg
        ad = data["ad"]
        message = SendAdToTg.format_ad(ad)
        image_url = SendAdToTg.get_first_image(ad)

        if image_url:
            payload = {
                "chat_id": chat_id,
                "caption": message,
                "photo": image_url,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            }
            return http_requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                json=payload,
                timeout=10
            )
        else:
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": False,
            }
            return http_requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
                timeout=10
            )

    def get_metrics(self) -> dict:
        """Метрики для health check"""
        uptime = time.time() - self._start_time if self._start_time else 0

        return {
            "running": self.running,
            "queue_size": self.queue.qsize(),
            "max_queue_size": MAX_QUEUE_SIZE,
            "sent_count": self.sent_count,
            "failed_count": self.failed_count,
            "dropped_count": self.dropped_count,
            "retry_count": self.retry_count,
            "uptime_seconds": round(uptime, 1),
            "rate_limit_msg_per_sec": round(1 / TELEGRAM_RATE_LIMIT_INTERVAL, 1),
        }


# Singleton instance
notification_queue = NotificationQueue()

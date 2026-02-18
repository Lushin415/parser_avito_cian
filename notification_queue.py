import asyncio
import contextlib
import time
from typing import Optional, Union, List, Iterable
from dataclasses import dataclass, field
from loguru import logger
import requests as http_requests

from models import Item
from cian_models import CianItem
from tg_sender import SendAdToTg


TELEGRAM_RATE_LIMIT_INTERVAL = 0.035
MAX_QUEUE_SIZE = 1000
MAX_RETRIES = 3
GRACEFUL_SHUTDOWN_TIMEOUT = 10

PRIORITY_SYSTEM = 0
PRIORITY_AD = 1


@dataclass(order=True)
class NotificationItem:
    priority: int
    timestamp: float = field(compare=False)
    data: dict = field(compare=False)


class NotificationQueue:
    _instance: Optional["NotificationQueue"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(MAX_QUEUE_SIZE)
        self.running = False
        self._consumer_task: Optional[asyncio.Task] = None

        self.sent_count = 0
        self.failed_count = 0
        self.dropped_count = 0
        self.retry_count = 0
        self._start_time = 0.0

        self._initialized = True
        logger.info("NotificationQueue инициализирован")

    async def start(self):
        if self.running:
            return

        self.running = True
        self._start_time = time.time()
        self._consumer_task = asyncio.create_task(self._consumer_loop())

    async def stop(self):
        if not self.running:
            return

        self.running = False

        try:
            await asyncio.wait_for(self.queue.join(), timeout=GRACEFUL_SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("NotificationQueue: graceful shutdown timeout")

        if self._consumer_task:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task

    async def enqueue_ad(
        self,
        ad: Union[Item, CianItem],
        user_config: dict,
        platform: str,
    ):
        tg_token = user_config.get("tg_token")
        tg_chat_id = user_config.get("tg_chat_id")

        if not tg_token or not tg_chat_id:
            return

        chat_ids = self._normalize_chat_ids(tg_chat_id)

        data = {
            "type": "ad",
            "ad": ad,
            "bot_token": tg_token,
            "chat_ids": chat_ids,
            "platform": platform,
        }

        await self._put_to_queue(PRIORITY_AD, data)

    async def enqueue_system_message(
        self,
        msg: str,
        bot_token: str,
        chat_ids: Union[str, int, Iterable[Union[str, int]]],
    ):
        chat_ids = self._normalize_chat_ids(chat_ids)

        data = {
            "type": "system",
            "msg": msg,
            "bot_token": bot_token,
            "chat_ids": chat_ids,
        }

        await self._put_to_queue(PRIORITY_SYSTEM, data)

    def _normalize_chat_ids(
        self, chat_ids: Union[str, int, Iterable[Union[str, int]]]
    ) -> List[Union[str, int]]:
        if isinstance(chat_ids, (str, int)):
            return [chat_ids]

        if isinstance(chat_ids, Iterable):
            return list(chat_ids)

        return []

    async def _put_to_queue(self, priority: int, data: dict):
        item = NotificationItem(priority, time.time(), data)

        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.dropped_count += 1
            except asyncio.QueueEmpty:
                pass

        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped_count += 1

    async def _consumer_loop(self):
        while self.running or not self.queue.empty():
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue

            try:
                await self._process_item(item)
            finally:
                self.queue.task_done()

    async def _process_item(self, item: NotificationItem):
        data = item.data
        bot_token = data.get("bot_token")
        chat_ids = data.get("chat_ids", [])

        if not bot_token or not chat_ids:
            return

        for chat_id in chat_ids:
            success = await self._send_single(chat_id, data)

            if success:
                self.sent_count += 1
            else:
                self.failed_count += 1

            await asyncio.sleep(TELEGRAM_RATE_LIMIT_INTERVAL)

    async def _send_single(self, chat_id: Union[str, int], data: dict) -> bool:
        bot_token = data["bot_token"]

        for attempt in range(MAX_RETRIES):
            try:
                response = await asyncio.to_thread(
                    self._do_send, chat_id, bot_token, data
                )

                if response.status_code == 200:
                    return True

                if response.status_code == 429:
                    retry_after = (
                        response.json()
                        .get("parameters", {})
                        .get("retry_after", 5)
                    )
                    self.retry_count += 1
                    await asyncio.sleep(retry_after)
                    continue

                if response.status_code in (400, 401, 403, 404):
                    logger.error(response.text)
                    return False

                wait_time = 2**attempt
                self.retry_count += 1
                await asyncio.sleep(wait_time)

            except Exception as e:
                logger.error(f"Ошибка отправки TG: {e}")
                await asyncio.sleep(2**attempt)

        return False

    def _do_send(
        self, chat_id: Union[str, int], bot_token: str, data: dict
    ) -> http_requests.Response:
        msg_type = data.get("type")

        if msg_type == "system":
            payload = {
                "chat_id": chat_id,
                "text": data["msg"],
                "parse_mode": "MarkdownV2",
            }
            return http_requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
                timeout=10,
            )

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
            method = "sendPhoto"
        else:
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "MarkdownV2",
            }
            method = "sendMessage"

        return http_requests.post(
            f"https://api.telegram.org/bot{bot_token}/{method}",
            json=payload,
            timeout=10,
        )

    def get_metrics(self) -> dict:
        uptime = time.time() - self._start_time if self._start_time else 0

        return {
            "running": self.running,
            "queue_size": self.queue.qsize(),
            "sent": self.sent_count,
            "failed": self.failed_count,
            "dropped": self.dropped_count,
            "retries": self.retry_count,
            "uptime": round(uptime, 1),
        }


notification_queue = NotificationQueue()

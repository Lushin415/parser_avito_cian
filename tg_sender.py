import requests
import time
import re

from loguru import logger

from models import Item
from cian_models import CianItem
from typing import Union


class SendAdToTg:
    def __init__(self, bot_token: str, chat_id: list, max_retries: int = 5, retry_delay: int = 5):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    @staticmethod
    def escape_markdown(text: str) -> str:
        """Экранирует спецсимволы MarkdownV2, кроме """
        if not text:
            return ""
        text = str(text).replace("\xa0", " ")
        return re.sub(r'([_\[\]()~`>#+\-=|{}.!])', r'\\\1', text)

    @staticmethod
    def get_first_image(ad: Union[Item, CianItem]):
        """Получает первое изображение из объявления"""
        # Для Avito
        if isinstance(ad, Item):
            if not ad.images:
                return None

            def get_largest_image_url(img):
                best_key = max(
                    img.root.keys(),
                    key=lambda k: int(k.split("x")[0]) * int(k.split("x")[1])
                )
                return str(img.root[best_key])

            images_urls = [get_largest_image_url(img) for img in ad.images]
            if images_urls:
                return images_urls[0]

        # Для Cian - изображений в списках нет
        # Можно вернуть плейсхолдер или None
        return None

    @staticmethod
    def format_ad(ad: Union[Item, CianItem]) -> str:
        """Форматирует объявление для Telegram (поддерживает Avito и Cian)"""

        def esc(text: str) -> str:
            if not text:
                return ""
            s = str(text).replace("\xa0", " ")
            return re.sub(r'([_\[\]()~`>#+\-=|{}.!])', r'\\\1', s)

        # Определяем источник и извлекаем данные
        if isinstance(ad, Item):  # Avito
            price = esc(str(ad.priceDetailed.value)) if ad.priceDetailed else ""
            title = esc(ad.title) if ad.title else ""
            url = f"https://avito.ru/{ad.urlPath}" if ad.urlPath else ""
            seller = esc(str(ad.sellerId)) if ad.sellerId else ""
            is_promoted = getattr(ad, "isPromotion", False)
            source = "🔵 Avito"
            area_text = ""  # У Avito нет площади в этом формате

        elif isinstance(ad, CianItem):  # Cian
            price = esc(str(ad.price.value)) if ad.price.value else ""
            title = esc(ad.title) if ad.title else ""
            url = ad.url  # URL не экранируем - он в скобках ссылки
            seller = esc(ad.author.name) if ad.author.name else ""
            is_promoted = False
            source = "🟢 Cian"

            # Площадь - ЭКРАНИРУЕМ!
            if ad.total_meters > 0:
                area_text = f"\n📐 {esc(str(ad.total_meters))} м²"
            else:
                area_text = ""
        else:
            return "Неизвестный тип объявления"

        # Формируем сообщение
        parts = []

        # Источник (эмодзи не экранируем)
        parts.append(source)

        # Цена
        if price:
            price_part = f"💰 *{price} руб/мес*"
            if is_promoted:
                price_part += " 🢁"
            parts.append(price_part)

        # Название (со ссылкой - ссылка НЕ экранируется)
        if title and url:
            parts.append(f"📝 [{title}]({url})")
        elif title:
            parts.append(f"📝 {title}")

        # Площадь (только для Cian) - УЖЕ ЭКРАНИРОВАНА выше
        if area_text:
            parts.append(area_text.strip())

        # Продавец
        if seller:
            parts.append(f"👤 {seller}")

        message = "\n".join(parts)
        return message

    def __send_to_tg(self, chat_id: str | int, ad: Union[Item, CianItem] = None, msg: str = None):
        """Отправляет сообщение в Telegram"""
        if msg:
            # Текстовое сообщение
            payload = {
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "MarkdownV2",
            }
            return requests.post(f"https://api.telegram.org/bot{self.bot_token}/sendMessage", json=payload)

        # Объявление
        message = self.format_ad(ad)
        _image_url = self.get_first_image(ad=ad)

        for attempt in range(1, self.max_retries + 1):
            response = None  # ← ИНИЦИАЛИЗИРУЕМ!
            try:
                if _image_url:
                    # С изображением
                    payload = {
                        "chat_id": chat_id,
                        "caption": message,
                        "photo": _image_url,
                        "parse_mode": "MarkdownV2",
                        "disable_web_page_preview": True,
                    }
                    response = requests.post(self.api_url, json=payload)
                else:
                    # Без изображения (только текст)
                    payload = {
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": "MarkdownV2",
                        "disable_web_page_preview": False,
                    }
                    response = requests.post(f"https://api.telegram.org/bot{self.bot_token}/sendMessage", json=payload)

                # Проверяем статус
                if response.status_code == 400:
                    logger.warning("Ошибка 400 от Telegram API")
                    try:
                        error_data = response.json()
                        logger.error(f"Детали ошибки: {error_data}")
                        logger.error(f"Сообщение которое пытались отправить:\n{message}")
                    except Exception as parse_err:
                        logger.error(f"Не удалось распарсить ошибку: {parse_err}")
                    break

                response.raise_for_status()
                logger.debug(f"Сообщение успешно отправлено (попытка {attempt})")
                break

            except requests.RequestException as e:
                logger.debug(f"Ошибка при отправке (попытка {attempt}): {e}")

                # Логируем детали если response существует
                if response is not None:
                    logger.debug(f"Status code: {response.status_code}")
                    try:
                        logger.debug(f"Response body: {response.text[:500]}")
                    except:
                        pass

                logger.debug(f"Сообщение:\n{message}")

                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
                else:
                    logger.debug("Не удалось отправить сообщение после всех попыток.")

    def send_to_tg(self, ad: Union[Item, CianItem] = None, msg: str = None):
        """Отправляет объявление или сообщение всем получателям"""
        for chat_id in self.chat_id:
            self.__send_to_tg(chat_id=chat_id, ad=ad, msg=msg)
import asyncio
import json
import random
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from cian_cities import get_city_code, is_city_supported, get_all_cities, get_cities_count

from bs4 import BeautifulSoup
from curl_cffi import requests
from loguru import logger
from requests.cookies import RequestsCookieJar

from cian_models import CianItem
from common_data import HEADERS
from db_service import SQLiteDBHandler
from dto import Proxy, CianConfig
from get_cookies import get_cookies
from hide_private_data import log_config
from tg_sender import SendAdToTg
from vk_sender import SendAdToVK
from version import VERSION
from xlsx_service import XLSXHandler


class CianParser:
    def __init__(self, config: CianConfig, stop_event=None):
        self.config = config
        # Валидация города
        if not is_city_supported(self.config.location):
            available_cities = ", ".join(get_all_cities()[:10])
            raise ValueError(
                f"Город '{self.config.location}' не поддерживается Циан.\n"
                f"Доступные города: {available_cities}... (всего {get_cities_count()} городов)"
            )

        # Получаем код города
        self.city_code = get_city_code(self.config.location)
        logger.info(f"📍 Город: {self.config.location} (код региона: {self.city_code})")
        self.proxy_obj = self.get_proxy_obj()
        self.db_handler = SQLiteDBHandler()
        self.tg_handler = self.get_tg_handler()
        self.vk_handler = self.get_vk_handler()
        self.xlsx_handler = XLSXHandler(self.__get_file_title())
        self.stop_event = stop_event
        self.cookies = None
        self.session = requests.Session()
        self.headers = HEADERS.copy()
        self.good_request_count = 0
        self.bad_request_count = 0

        log_config(config=self.config, version=VERSION)
        logger.info(f"Запуск CianParser v{VERSION}")
        logger.info(f"Настройки: location={config.location}, deal_type={config.deal_type}")
        logger.info(f"Фильтры: цена {config.min_price}-{config.max_price}, площадь {config.min_area}-{config.max_area}")

    def get_tg_handler(self) -> SendAdToTg | None:
        if all([self.config.tg_token, self.config.tg_chat_id]):
            return SendAdToTg(bot_token=self.config.tg_token, chat_id=self.config.tg_chat_id)
        return None

    def get_vk_handler(self) -> SendAdToVK | None:
        if all([self.config.vk_token, self.config.vk_user_id]):
            logger.info("VK handler инициализирован")
            return SendAdToVK(vk_token=self.config.vk_token, user_id=self.config.vk_user_id)
        return None

    def _send_to_tg(self, ads: list[CianItem]) -> None:
        """Отправляет объявления в Telegram"""
        for ad in ads:
            self.tg_handler.send_to_tg(ad=ad)

    def _send_to_vk(self, ads: list[CianItem]) -> None:
        """Отправляет объявления в VK"""
        for ad in ads:
            self.vk_handler.send_to_vk(ad=ad)
            time.sleep(1)

    def get_proxy_obj(self) -> Proxy | None:
        if all([self.config.proxy_string, self.config.proxy_change_url]):
            return Proxy(
                proxy_string=self.config.proxy_string,
                change_ip_link=self.config.proxy_change_url
            )
        logger.info("Работаем без прокси")
        return None

    def get_cookies(self, max_retries: int = 1, delay: float = 2.0) -> dict | None:
        """Получение cookies через Playwright (обход блокировок)"""
        if not self.config.use_webdriver:
            return None

        for attempt in range(1, max_retries + 1):
            if self.stop_event and self.stop_event.is_set():
                return None

            try:
                # Используем случайный ID объявления для получения cookies
                random_id = str(random.randint(100000000, 999999999))
                test_url = f"https://www.cian.ru/rent/flat/{random_id}/"

                cookies, user_agent = asyncio.run(
                    get_cookies(proxy=self.proxy_obj, headless=True, stop_event=self.stop_event))

                if cookies:
                    logger.info(f"[get_cookies] Успешно получены cookies с попытки {attempt}")
                    self.headers["user-agent"] = user_agent
                    return cookies
                else:
                    raise ValueError("Пустой результат cookies")
            except Exception as e:
                logger.warning(f"[get_cookies] Попытка {attempt} не удалась: {e}")
                if attempt < max_retries:
                    time.sleep(delay * attempt)
                else:
                    logger.error(f"[get_cookies] Все {max_retries} попытки не удались")
                    return None

    def save_cookies(self) -> None:
        """Сохраняет cookies в JSON файл"""
        with open("cookies_cian.json", "w") as f:
            json.dump(self.session.cookies.get_dict(), f)

    def load_cookies(self) -> None:
        """Загружает cookies из JSON файла"""
        try:
            with open("cookies_cian.json", "r") as f:
                cookies = json.load(f)
                jar = RequestsCookieJar()
                for k, v in cookies.items():
                    jar.set(k, v)
                self.session.cookies.update(jar)
        except FileNotFoundError:
            pass

    def fetch_data(self, url, retries=3, backoff_factor=1):
        """Загрузка страницы с обходом блокировок"""
        proxy_data = None
        if self.proxy_obj:
            proxy_data = {
                "https": f"http://{self.config.proxy_string}"
            }

        for attempt in range(1, retries + 1):
            if self.stop_event and self.stop_event.is_set():
                return None

            try:
                response = self.session.get(
                    url=url,
                    headers=self.headers,
                    proxies=proxy_data,
                    cookies=self.cookies,
                    impersonate="chrome",
                    timeout=20,
                    verify=False,
                )
                logger.debug(f"Попытка {attempt}: {response.status_code}")

                if response.status_code >= 500:
                    raise requests.RequestsError(f"Ошибка сервера: {response.status_code}")

                if response.status_code in [302, 403, 429]:
                    self.bad_request_count += 1
                    self.session = requests.Session()
                    if attempt >= 3:
                        self.cookies = self.get_cookies()
                    self.change_ip()
                    raise requests.RequestsError(f"Блокировка: {response.status_code}")

                self.save_cookies()
                self.good_request_count += 1
                return response.text

            except Exception as e:
                logger.debug(f"Попытка {attempt} неуспешна: {e}")
                if attempt < retries:
                    sleep_time = backoff_factor * attempt
                    logger.debug(f"Повтор через {sleep_time} секунд...")
                    time.sleep(sleep_time)
                else:
                    logger.info("Все попытки неуспешны")
                    return None

    def change_ip(self) -> bool:
        """Смена IP через прокси"""
        if not self.config.proxy_change_url:
            logger.info("Смена IP недоступна (нет прокси)")
            return False

        logger.info("Меняю IP")
        try:
            res = requests.get(url=self.config.proxy_change_url, verify=False)
            if res.status_code == 200:
                logger.info("IP изменен")
                return True
        except Exception as err:
            logger.info(f"Ошибка смены IP: {err}")

        logger.info("Повтор смены IP")
        time.sleep(random.randint(3, 10))
        return self.change_ip()

    def parse_list_page(self, html: str) -> list[CianItem]:
        """Парсинг списка объявлений со страницы"""
        soup = BeautifulSoup(html, 'html.parser')

        # Проверка на капчу
        if "Captcha" in html or "captcha" in html.lower():
            logger.warning("Обнаружена капча!")
            return []

        # Ищем объявления (для коммерческой недвижимости используется другой селектор)
        offers = soup.select("div[data-name='HorizontalCard']")

        if not offers:
            logger.warning("Объявления не найдены на странице")
            return []

        logger.info(f"Найдено {len(offers)} объявлений на странице")

        ads = []
        for offer in offers:
            try:
                ad = self.parse_single_offer(offer)
                if ad:
                    ads.append(ad)
            except Exception as e:
                logger.error(f"Ошибка парсинга объявления: {e}")
                continue

        return ads

    def parse_single_offer(self, offer) -> CianItem | None:
        """Парсинг одного объявления коммерческой недвижимости"""
        try:
            # Импортируем функции из helpers
            from cian_helpers import (
                parse_author,
                parse_location,
                parse_description,
                extract_price_from_title,
                extract_area_from_title
            )

            # Ищем заголовок и ссылку
            title_link = offer.select_one("a[data-name='CommercialTitle']")
            if not title_link:
                return None

            url = title_link.get('href', '')
            if not url:
                return None

            # Заголовок
            title = title_link.get_text(strip=True)

            # ID из URL
            ad_id = self._extract_id_from_url(url)

            # Парсим цену из заголовка
            price_value = extract_price_from_title(title)

            # Парсим площадь из заголовка
            total_meters = extract_area_from_title(title)

            # Создаём объект цены
            from cian_models import CianPrice
            price = CianPrice(
                value=price_value,
                price_per_month=price_value if self.config.deal_type == "rent_long" else None
            )

            # Парсим автора
            author = parse_author(offer)

            # Парсим адрес
            location = parse_location(offer)

            # Парсим описание
            description = parse_description(offer)

            # Создаём объявление
            ad = CianItem(
                id=ad_id,
                url=url if url.startswith('http') else f"https://cian.ru{url}",
                title=title,
                location=self.config.location,
                deal_type=self.config.deal_type,
                price=price,
                total_meters=total_meters,
                author=author,
                location_data=location,
                description=description,
            )

            return ad

        except Exception as e:
            logger.error(f"Ошибка парсинга объявления: {e}")
            return None

    def _extract_price_from_title(self, title: str) -> int:
        """Извлекает цену из заголовка"""
        import re

        # Ищем паттерны: "за 720 000 руб." или "от 674 208 ₽"
        patterns = [
            r'за\s+([\d\s]+)\s*(?:руб|₽)',  # "за 720 000 руб."
            r'от\s*([\d\s]+)\s*(?:руб|₽)',  # "от 674 208 ₽"
        ]

        for pattern in patterns:
            match = re.search(pattern, title)
            if match:
                price_str = match.group(1).replace(' ', '').replace('\xa0', '')
                try:
                    return int(price_str)
                except ValueError:
                    continue

        return 0

    def _extract_area_from_title(self, title: str) -> float:
        """Извлекает площадь из заголовка"""
        import re

        # Ищем паттерны: "209,7 м²" или "147,1 – 1 433,4 м²"
        patterns = [
            r'([\d\s,]+)\s*м[²2]',  # "209,7 м²"
        ]

        for pattern in patterns:
            match = re.search(pattern, title)
            if match:
                area_str = match.group(1).replace(' ', '').replace(',', '.')
                try:
                    # Берём первое число (если диапазон)
                    first_number = area_str.split('–')[0].strip()
                    return float(first_number)
                except ValueError:
                    continue

        return -1.0

    def _extract_id_from_url(self, url: str) -> str:
        """Извлекает ID объявления из URL"""
        parts = url.split('/')
        for part in reversed(parts):
            if part.isdigit():
                return part
        return str(abs(hash(url)))[:10]

    def filter_ads(self, ads: list[CianItem]) -> list[CianItem]:
        """Фильтрация объявлений"""
        filters = [
            self._filter_viewed,
            self._filter_by_price_range,
            self._filter_by_area,
        ]

        for filter_fn in filters:
            ads = filter_fn(ads)
            logger.info(f"После фильтрации {filter_fn.__name__}: {len(ads)} объявлений")
            if not ads:
                return ads

        return ads

    def _filter_viewed(self, ads: list[CianItem]) -> list[CianItem]:
        """Фильтр просмотренных"""
        try:
            return [ad for ad in ads if not self.is_viewed(ad)]
        except Exception as err:
            logger.debug(f"Ошибка фильтра просмотренных: {err}")
            return ads

    def _filter_by_price_range(self, ads: list[CianItem]) -> list[CianItem]:
        """Фильтр по цене"""
        try:
            return [
                ad for ad in ads
                if self.config.min_price <= ad.price.value <= self.config.max_price
            ]
        except Exception as err:
            logger.debug(f"Ошибка фильтра по цене: {err}")
            return ads

    def _filter_by_area(self, ads: list[CianItem]) -> list[CianItem]:
        """Фильтр по площади"""
        if self.config.min_area == 0 and self.config.max_area == 999_999:
            return ads

        try:
            return [
                ad for ad in ads
                if ad.total_meters > 0 and
                   self.config.min_area <= ad.total_meters <= self.config.max_area
            ]
        except Exception as err:
            logger.debug(f"Ошибка фильтра по площади: {err}")
            return ads

    def is_viewed(self, ad: CianItem) -> bool:
        """Проверка просмотрено ли объявление"""
        # Если нет цены, считаем непросмотренным
        if not ad.price:
            return False

        # Преобразуем ID в int
        ad_id = int(ad.id) if ad.id.isdigit() else abs(hash(ad.id))

        return self.db_handler.record_exists(
            record_id=ad_id,
            price=ad.price.value
        )

    def __get_file_title(self) -> str:
        """Формирует название файла для сохранения"""
        return f"result/cian_{self.config.location.lower()}.xlsx"

    def __save_data(self, ads: list[CianItem]) -> None:
        """Сохраняет результат в файл"""
        try:
            logger.info(f"📊 Вызван __save_data с {len(ads)} объявлениями")  # ← ДОБАВЬ
            self.xlsx_handler.append_data_from_page(ads=ads)
            logger.info("✅ Сохранение в Excel завершено")  # ← ДОБАВЬ
        except Exception as err:
            logger.error(f"❌ При сохранении в Excel ошибка: {err}")
            import traceback
            logger.error(traceback.format_exc())  # ← ДОБАВЬ (полный стек ошибки)

    def __save_viewed(self, ads: list[CianItem]) -> None:
        """Сохранение просмотренных объявлений в БД"""
        try:
            # Конвертируем CianItem в формат для БД
            records = [
                self._convert_cian_to_db_format(ad)
                for ad in ads
            ]
            records = [r for r in records if r is not None]  # Убираем None

            # Сохраняем
            if records:
                import sqlite3
                with sqlite3.connect(self.db_handler.db_name) as conn:
                    cursor = conn.cursor()
                    cursor.executemany(
                        "INSERT OR REPLACE INTO viewed (id, price) VALUES (?, ?)",
                        records
                    )
                    conn.commit()
                logger.info(f"Сохранено {len(records)} в БД")
            else:
                logger.info("Нечего сохранять в БД")

        except Exception as err:
            logger.error(f"Ошибка сохранения в БД: {err}")

    def _convert_cian_to_db_format(self, ad: CianItem) -> tuple | None:
        """Конвертирует CianItem в формат для БД (id, price)"""
        try:
            if not ad.price or ad.price.value <= 0:
                return None

            ad_id = int(ad.id) if ad.id.isdigit() else abs(hash(ad.id))
            return (ad_id, ad.price.value)
        except Exception as e:
            logger.error(f"Ошибка конвертации объявления {ad.id}: {e}")
            return None

    def get_next_page_url(self, url: str) -> str:
        """Формирует URL следующей страницы"""
        try:
            url_parts = urlparse(url)
            query_params = parse_qs(url_parts.query)
            current_page = int(query_params.get('p', [1])[0])
            query_params['p'] = [str(current_page + 1)]

            new_query = urlencode(query_params, doseq=True)
            next_url = urlunparse((
                url_parts.scheme, url_parts.netloc, url_parts.path,
                url_parts.params, new_query, url_parts.fragment
            ))
            return next_url
        except Exception as err:
            logger.error(f"Ошибка формирования URL: {err}")
            return url

    def parse(self):
        """Главный метод парсинга"""
        logger.info(f"Начинаем парсинг Циан для города: {self.config.location}")

        for url_index, url in enumerate(self.config.urls):
            logger.info(f"Обработка ссылки {url_index + 1}/{len(self.config.urls)}")
            # Обработка region в URL
            if 'region=' not in url:
                separator = '&' if '?' in url else '?'
                url = f"{url}{separator}region={self.city_code}"
                logger.info(f"➕ Добавлен region={self.city_code} в URL")
            else:
                import re
                url = re.sub(r'region=\d+', f'region={self.city_code}', url)
                logger.info(f"🔄 Заменён region на {self.city_code} в URL")
            for page in range(self.config.count):
                if self.stop_event and self.stop_event.is_set():
                    return

                logger.info(f"Страница {page + 1}/{self.config.count}")

                html = self.fetch_data(url=url, retries=self.config.max_count_of_retry)

                if not html:
                    logger.warning(f"Не удалось получить HTML для {url}")
                    time.sleep(self.config.pause_between_links)
                    continue

                ads = self.parse_list_page(html)

                if not ads:
                    logger.info("Объявления закончились")
                    break

                filtered_ads = self.filter_ads(ads)

                # Отправляем уведомления
                if self.tg_handler and not self.config.one_time_start:
                    for ad in filtered_ads:
                        self.tg_handler.send_to_tg(ad=ad)
                        pass

                if self.vk_handler and not self.config.one_time_start:
                    for ad in filtered_ads:
                        self.vk_handler.send_to_vk(ad=ad)
                        pass

                if filtered_ads:
                    logger.info(f"💾 Сохраняю {len(filtered_ads)} объявлений")
                    self.__save_viewed(ads=filtered_ads)
                    self.__save_data(ads=filtered_ads)

                # Переходим на следующую страницу
                url = self.get_next_page_url(url)

                logger.info(f"Пауза {self.config.pause_between_links} сек.")
                time.sleep(self.config.pause_between_links)

        logger.info(f"Парсинг завершён. Хорошие запросы: {self.good_request_count}, плохие: {self.bad_request_count}")


if __name__ == "__main__":
    from load_config import load_cian_config

    try:
        config = load_cian_config()
    except Exception as err:
        logger.error(f"Ошибка загрузки конфига: {err}")
        exit(1)

    while True:
        try:
            parser = CianParser(config)
            parser.parse()

            if config.one_time_start:
                logger.info("Парсинг завершён (one_time_start)")
                break

            logger.info(f"Пауза {config.pause_general} сек")
            time.sleep(config.pause_general)
        except Exception as err:
            logger.error(f"Ошибка: {err}. Перезапуск через 30 сек")
            time.sleep(30)
import re
from loguru import logger

from cian_models import CianAuthor, CianLocation

"""
Вспомогательные функции для парсинга Циан

ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ:
1. Некоторые объявления специально скрывают цену/площадь в заголовке
   (антипарсинг техника) и указывают их только в описании.
   Текущая версия парсит только из заголовка (~90% объявлений).

2. Автор может быть "Неизвестно" если:
   - Циан скрывает автора до звонка
   - Новая структура карточки
   - Объявление от системы

TODO для будущих улучшений:
- Добавить парсинг цены/площади из описания (fallback)
- Добавить больше вариантов селекторов для автора
- Логировать "подозрительные" объявления для анализа
"""
# TODO Список указан выше!
def parse_author(offer) -> CianAuthor:
    """
    Парсит информацию об авторе объявления

    Типы авторов:
    - owner: Собственник
    - agent: Агент/Риелтор
    - agency: Агентство недвижимости
    - developer: Застройщик
    - unknown: Неизвестно
    """
    try:
        # Ищем бейдж "Собственник"
        owner_badge = offer.select_one("span[data-name='OwnerBadge']")
        if owner_badge and "Собственник" in owner_badge.get_text():
            return CianAuthor(
                name="Собственник",
                type="owner"
            )

        # Ищем информацию об агенте/агентстве
        agent_brand = offer.select_one("div[data-name='HorizontalAgentBrand']")
        if agent_brand:
            # Вариант A: ищем span с классом name_wrapper
            name_elem = agent_brand.select_one("span[class*='name_wrapper']")

            # Вариант B: если не нашли, ищем div с классом name
            if not name_elem:
                name_elem = agent_brand.select_one("div[class*='name--']")

            # Вариант C: ищем любой элемент с "name" в классе
            if not name_elem:
                name_elem = agent_brand.select_one("[class*='name']")

            if name_elem:
                name = name_elem.get_text(strip=True)

                # Определяем тип по ключевым словам
                name_lower = name.lower()
                if any(word in name_lower for word in ['агентство', 'agency', 'недвижимость']):
                    author_type = "agency"
                elif any(word in name_lower for word in ['застройщик', 'девелопер', 'developer']):
                    author_type = "developer"
                else:
                    author_type = "agent"

                return CianAuthor(
                    name=name,
                    type=author_type
                )

        # Если ничего не нашли
        return CianAuthor(
            name="Неизвестно",
            type="unknown"
        )

    except Exception as e:
        logger.error(f"Ошибка парсинга автора: {e}")
        return CianAuthor(name="Ошибка", type="unknown")


def parse_location(offer) -> CianLocation:
    """
    Парсит адрес объявления

    Извлекает:
    - Район
    - Округ (ЦАО, ЗАО и т.д.)
    - Метро
    - Удалённость от метро
    - Улица
    - Номер дома
    - Полный адрес
    """
    try:
        location = CianLocation()

        # Ищем блок с адресом
        address_block = offer.select_one("div[data-name='Address']")
        if not address_block:
            return location

        # Парсим метро и удалённость
        underground = address_block.select_one("div[data-name='Underground']")
        if underground:
            # Название метро
            metro_name = underground.select_one("div[class*='underground-name']")
            if metro_name:
                location.underground = metro_name.get_text(strip=True)

            # Удалённость от метро
            remoteness = underground.select_one("div[class*='remoteness']")
            if remoteness:
                location.metro_remoteness = remoteness.get_text(strip=True)

        # Парсим полный адрес
        address_path = address_block.select_one("div[class*='address-path']")
        if address_path:
            location.full_address = address_path.get_text(strip=True)

        # Парсим адрес (район, улица, дом)
        address_items = address_block.select("a[data-name='AddressPathItem']")

        for item in address_items:
            text = item.get_text(strip=True)
            href = item.get('href', '')

            # Определяем тип элемента по href
            if 'district' in href:
                if any(okrug in text for okrug in
                       ['ЦАО', 'ЗАО', 'САО', 'СВАО', 'ВАО', 'ЮВАО', 'ЮАО', 'ЮЗАО', 'ЗелАО', 'НАО', 'ТАО']):
                    location.district_okrug = text
                elif 'р-н' in text or 'поселение' in text:
                    location.district = text
            elif 'street' in href:
                location.street = text
            elif 'house' in href:
                location.house_number = text

        return location

    except Exception as e:
        logger.error(f"Ошибка парсинга локации: {e}")
        return CianLocation()


def parse_description(offer) -> str:
    """
    Парсит описание объявления
    """
    try:
        # Ищем блок с описанием
        desc_block = offer.select_one("div[class*='description']")
        if desc_block:
            description = desc_block.get_text(strip=True)
            # Ограничиваем длину (для БД и уведомлений)
            return description[:1000] if len(description) > 1000 else description

        return ""

    except Exception as e:
        logger.error(f"Ошибка парсинга описания: {e}")
        return ""


def extract_price_from_title(title: str) -> int:
    """Извлекает цену из заголовка"""
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


def extract_area_from_title(title: str) -> float:
    """Извлекает площадь из заголовка"""
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


def extract_price_from_description(description: str) -> int:
    """Извлекает цену из описания если нет в заголовке"""
    if not description:
        return 0

    patterns = [
        r'цена[:\s]*([\d\s]+)\s*(?:руб|₽)',
        r'аренда[:\s]*([\d\s]+)\s*(?:руб|₽)',
        r'стоимость[:\s]*([\d\s]+)\s*(?:руб|₽)',
        r'от\s*([\d\s]+)\s*(?:руб|₽)',
        r'за\s*([\d\s]+)\s*(?:руб|₽)',
    ]

    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            price_str = match.group(1).replace(' ', '').replace('\xa0', '')
            try:
                return int(price_str)
            except:
                continue

    return 0


def extract_area_from_description(description: str) -> float:
    """Извлекает площадь из описания"""
    if not description:
        return -1.0

    patterns = [
        r'площадь[:\s]*([\d,\.]+)\s*м',
        r'([\d,\.]+)\s*м²',
        r'([\d,\.]+)\s*кв\.?\s*м',
    ]

    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            area_str = match.group(1).replace(',', '.')
            try:
                return float(area_str)
            except:
                continue

    return -1.0
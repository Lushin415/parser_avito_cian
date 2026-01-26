from pydantic import BaseModel
from typing import Optional, List


class CianAuthor(BaseModel):
    """Автор объявления"""
    name: str = ""
    type: str = ""  # homeowner, real_estate_agent, realtor, developer, etc.


class CianLocation(BaseModel):
    """Локация объявления"""
    district: str = ""
    underground: str = ""  # метро
    street: str = ""
    house_number: str = ""


class CianPrice(BaseModel):
    """Цена"""
    value: int = 0  # основная цена
    price_per_month: Optional[int] = None  # для аренды
    commissions: int = 0  # комиссия при аренде


class CianItem(BaseModel):
    """Объявление с Циан"""
    # Основная информация
    id: str  # ID объявления из URL
    url: str
    title: str = ""
    description: str = ""

    # Локация
    location: str = "Москва"  # город
    location_data: CianLocation = CianLocation()

    # Автор
    author: CianAuthor = CianAuthor()

    # Тип объявления
    deal_type: str = "rent_long"  # rent_long, sale
    accommodation_type: str = "flat"  # flat, suburban, newobject

    # Характеристики помещения
    floor: int = -1
    floors_count: int = -1
    rooms_count: int = -1
    total_meters: float = -1.0  # ВАЖНО ДЛЯ ФИЛЬТРА ПО ПЛОЩАДИ
    living_meters: float = -1.0
    kitchen_meters: float = -1.0

    # Цена
    price: CianPrice = CianPrice()

    # Дополнительная информация
    year_of_construction: int = -1
    house_material_type: str = ""
    heating_type: str = ""
    finish_type: str = ""
    phone: str = ""

    # Для фильтрации
    seller_id: Optional[str] = None  # для черного списка продавцов
    timestamp: Optional[int] = None  # время публикации (если есть)


class CianResponse(BaseModel):
    """Список объявлений"""
    items: List[CianItem]
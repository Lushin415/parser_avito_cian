import asyncio
import httpx
import random

API_URL = "http://localhost:8009/parse/start"

# Список реальных ссылок Авито для разнообразия (разные категории/города)
AVITO_URLS = [
    "https://www.avito.ru/moskva/kommercheskaya_nedvizhimost?localPriority=0&s=104",
    "https://www.avito.ru/sankt-peterburg/kommercheskaya_nedvizhimost?localPriority=0&s=104",
    "https://www.avito.ru/kazan/kommercheskaya_nedvizhimost?localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/obuv_zhenskaya-ASgBAgICAUTeAryp1gI?localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/zhenskaya_odezhda-ASgBAgICAUTeAtYL?localPriority=0&s=104",
    "https://www.avito.ru/moskva/chasy_i_ukrasheniya/chasy-ASgBAgICAUTQAYYG?localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda-ASgBAgICAUTeAtgL?localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/pidzhaki_i_kostyumy-ASgBAgICAkTeAtgL4ALkCw?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/verhnyaya_odezhda-ASgBAgICAkTeAtgL4ALeCw?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/kofty_i_futbolki-ASgBAgICAkTeAtgL4ALoCw?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/dzhinsy-ASgBAgICAkTeAtgL4ALgCw?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/bryuki-ASgBAgICAkTeAtgL4ALcCw?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/rubashki-ASgBAgICAkTeAtgL4ALcDg?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/sportivnye_kostiumy-ASgBAgICAkTeAtgL4ALC5Y0D?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/sorty-ASgBAgICAkTeAtgL4ALE5Y0D?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/odezhda_obuv_aksessuary/muzhskaya_odezhda/drugoe-ASgBAgICAkTeAtgL4ALqCw?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/telefony/mobile-ASgBAgICAUSwwQ2I_Dc?localPriority=0&s=104",
    "https://www.avito.ru/moskva/telefony/mobilnye_telefony/apple/iphone_13-ASgBAgICA0SywA3svcgBtMANzqs5sMENiPw3?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/telefony/mobilnye_telefony/apple/iphone_14_pro-ASgBAgICA0SywA3OjuUQtMANzqs5sMENiPw3?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/telefony/mobilnye_telefony/apple/iphone_15-ASgBAgICA0SywA2SoO0RtMANzqs5sMENiPw3?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/telefony/mobilnye_telefony/samsung/galaxy_s24_ultra-ASgBAgICA0SywA2wrPYRtMANnK85sMENiPw3?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/telefony/mobilnye_telefony/samsung/galaxy_s23-ASgBAgICA0SywA2mqscRtMANnK85sMENiPw3?cd=1&localPriority=0&s=104",
    "https://www.avito.ru/moskva/bytovaya_elektronika?cd=1&localPriority=0&q=xiaomi&s=104",
    "https://www.avito.ru/moskva/igry_pristavki_i_programmy/igry_pristavki_i_programmy/igrovye_pristavki_i_aksessuary/igrovye_pristavki-ASgBAgICAkSSAsoJ9M0UmsqPAw?cd=1&localPriority=0&q=playstation+5&s=104",
    "https://www.avito.ru/moskva/planshety_i_elektronnye_knigi/planshety-ASgBAgICAUSYAoZO?f=ASgBAgICAkSYAoZOmoMPjPnwAg&localPriority=0&s=104",
    "https://www.avito.ru/moskva/chasy_i_ukrasheniya/chasy/smart-ASgBAgICAkTQAYYGhNgR_paHAw?cd=1&localPriority=0&q=apple+watch&s=104",
    # Добавь еще штук 5-10 разных
]


async def start_task(client, user_id):
    payload = {
        "user_id": user_id,
        "avito_url": random.choice(AVITO_URLS),
        "notification_bot_token": "8374925023:AAG9QwKAfXnwDZ4ZvtjP6zaoaqkEXeZn6p8",  # Можешь поставить заглушку
        "notification_chat_id": 338908929,  # Твой ID
        "pages": 1
    }
    try:
        resp = await client.post(API_URL, json=payload, timeout=10)
        print(f"User {user_id}: {resp.status_code} - {resp.json().get('task_id')}")
    except Exception as e:
        print(f"Error for user {user_id}: {e}")


async def main():
    async with httpx.AsyncClient() as client:
        tasks = []
        for i in range(50):
            # Имитируем пачку из 50 пользователей
            tasks.append(start_task(client, 1000 + i))
            # Небольшая задержка между регистрациями, чтобы API не захлебнулся
            await asyncio.sleep(0.5)

        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
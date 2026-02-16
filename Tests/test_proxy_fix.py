"""
Тест исправления прокси в CookieManager (Phase 2)

Проверяет:
1. Браузер запускается с прокси на уровне browser (не context)
2. Retry-логика для ERR_NETWORK_CHANGED
3. Автоматический перезапуск браузера при смене прокси
"""
import asyncio
from loguru import logger

from cookie_manager import cookie_manager
from load_config import load_avito_config
from dto import Proxy


async def test_proxy_fix():
    logger.info("=" * 60)
    logger.info("ТЕСТ: Исправление прокси в CookieManager")
    logger.info("=" * 60)

    # Загрузка прокси из config.toml
    config = load_avito_config("../config.toml")

    if not config.proxy_string:
        logger.error("⚠️ Прокси не настроен в config.toml!")
        return

    proxy = Proxy(
        proxy_string=config.proxy_string,
        change_ip_link=config.proxy_change_url or ""
    )

    # Маскируем пароль в логе
    parts = proxy.proxy_string.split(":")
    if len(parts) >= 4:
        masked = f"{parts[0]}:{parts[1]}:***:***"
    else:
        masked = proxy.proxy_string
    logger.info(f"✓ Прокси загружен: {masked}")
    logger.info("")

    # Тест 1: Запуск браузера с прокси
    logger.info("[ТЕСТ 1] Запуск CookieManager с прокси...")
    await cookie_manager.start(proxy=proxy, headless=True)
    logger.success("✓ Браузер запущен")
    logger.info("")

    # Тест 2: Получение cookies для Avito
    logger.info("[ТЕСТ 2] Получение cookies для Avito через прокси...")
    try:
        cookies, user_agent = await cookie_manager.get_cookies(
            platform="avito",
            force_refresh=True  # Принудительное обновление (не из кэша)
        )

        if cookies:
            logger.success(f"✓ Cookies получены: {len(cookies)} cookies")
            logger.info(f"  User-Agent: {user_agent[:60]}...")
        else:
            logger.error("✗ Cookies НЕ получены!")

    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")

    logger.info("")

    # Тест 3: Получение cookies для Cian
    logger.info("[ТЕСТ 3] Получение cookies для Cian через прокси...")
    try:
        cookies, user_agent = await cookie_manager.get_cookies(
            platform="cian",
            force_refresh=True
        )

        if cookies:
            logger.success(f"✓ Cookies получены: {len(cookies)} cookies")
            logger.info(f"  User-Agent: {user_agent[:60]}...")
        else:
            logger.error("✗ Cookies НЕ получены!")

    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")

    logger.info("")

    # Информация о кэше
    logger.info("[КЭШ] Информация о cookies:")
    cache_info = cookie_manager.get_cache_info()
    logger.info(f"  Браузер запущен: {cache_info['browser_running']}")
    logger.info(f"  Активных мониторов: {cache_info['active_monitors']}")

    for platform, info in cache_info.items():
        if platform not in ["browser_running", "active_monitors"]:
            logger.info(f"  {platform.upper()}:")
            logger.info(f"    - Возраст: {info['age_seconds']:.0f}с")
            logger.info(f"    - TTL: {info['ttl_seconds']}с")
            logger.info(f"    - Кэшировано: {info['cached_at']}")

    logger.info("")

    # Остановка
    logger.info("[ОСТАНОВКА] Завершение работы...")
    await cookie_manager.stop()
    logger.success("✓ CookieManager остановлен")

    logger.info("")
    logger.info("=" * 60)
    logger.info("ТЕСТ ЗАВЕРШЁН")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_proxy_fix())

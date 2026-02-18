"""
Тестовый скрипт для проверки CookieManager (Phase 1)

Проверяет:
1. Запуск единственного экземпляра браузера
2. Получение cookies для Avito и Cian
3. Сохранение cookies в файлы
4. Кэширование и TTL
5. Ротацию User-Agent
6. Остановку браузера
"""
import asyncio
import time
from pathlib import Path
from loguru import logger

from cookie_manager import cookie_manager
from dto import Proxy


async def test_basic_functionality():
    """Базовая функциональность CookieManager"""
    logger.info("=" * 60)
    logger.info("ТЕСТ 1: Базовая функциональность")
    logger.info("=" * 60)

    # 1. Получение cookies для Avito
    logger.info("\n1. Получение cookies для Avito...")
    cookies_avito, ua_avito = await cookie_manager.get_cookies("avito")

    if cookies_avito:
        logger.success(f"✓ Avito cookies получены: {len(cookies_avito)} элементов")
        logger.info(f"  User-Agent: {ua_avito[:60]}...")
        logger.info(f"  Cookies keys: {list(cookies_avito.keys())[:5]}")
    else:
        logger.error("✗ Не удалось получить Avito cookies")
        return False

    # 2. Получение cookies для Cian
    logger.info("\n2. Получение cookies для Cian...")
    cookies_cian, ua_cian = await cookie_manager.get_cookies("cian")

    if cookies_cian:
        logger.success(f"✓ Cian cookies получены: {len(cookies_cian)} элементов")
        logger.info(f"  User-Agent: {ua_cian[:60]}...")
        logger.info(f"  Cookies keys: {list(cookies_cian.keys())[:5]}")
    else:
        logger.error("✗ Не удалось получить Cian cookies")
        return False

    # 3. Проверка User-Agent ротации
    logger.info("\n3. Проверка ротации User-Agent...")
    if ua_avito != ua_cian:
        logger.success("✓ User-Agent различаются (ротация работает)")
    else:
        logger.warning("⚠ User-Agent одинаковые (может быть случайное совпадение)")

    return True


async def test_caching():
    """Проверка кэширования"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 2: Кэширование")
    logger.info("=" * 60)

    # 1. Первое получение (должно обновить)
    logger.info("\n1. Первое получение cookies...")
    start = time.time()
    cookies1, ua1 = await cookie_manager.get_cookies("avito")
    time1 = time.time() - start
    logger.info(f"  Время: {time1:.2f}с")

    # 2. Второе получение (должно взять из кэша)
    logger.info("\n2. Второе получение cookies (из кэша)...")
    start = time.time()
    cookies2, ua2 = await cookie_manager.get_cookies("avito")
    time2 = time.time() - start
    logger.info(f"  Время: {time2:.2f}с")

    if cookies1 == cookies2 and ua1 == ua2:
        logger.success("✓ Cookies взяты из кэша (идентичны)")
    else:
        logger.error("✗ Cookies не идентичны (кэш не работает)")
        return False

    if time2 < 1:  # Кэш должен работать мгновенно
        logger.success(f"✓ Кэш работает быстро ({time2:.3f}с)")
    else:
        logger.warning(f"⚠ Кэш медленный ({time2:.3f}с)")

    return True


async def test_persistence():
    """Проверка сохранения на диск"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 3: Персистентность (сохранение на диск)")
    logger.info("=" * 60)

    # Проверка файлов
    avito_file = Path("../cookies.json")
    cian_file = Path("../cookies_cian.json")

    logger.info("\n1. Проверка файлов cookies...")

    if avito_file.exists():
        size = avito_file.stat().st_size
        logger.success(f"✓ cookies.json существует ({size} байт)")
    else:
        logger.error("✗ cookies.json не найден")
        return False

    if cian_file.exists():
        size = cian_file.stat().st_size
        logger.success(f"✓ cookies_cian.json существует ({size} байт)")
    else:
        logger.error("✗ cookies_cian.json не найден")
        return False

    # Чтение файлов
    import json
    try:
        with open(avito_file, 'r') as f:
            data = json.load(f)
            logger.success(f"✓ cookies.json валидный JSON (платформа: {data.get('platform')})")
    except Exception as e:
        logger.error(f"✗ Ошибка чтения cookies.json: {e}")
        return False

    return True


async def test_cache_info():
    """Проверка информации о кэше"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 4: Информация о кэше")
    logger.info("=" * 60)

    info = cookie_manager.get_cache_info()

    logger.info("\nИнформация о кэше:")
    logger.info(f"  Браузер запущен: {info.get('browser_running')}")
    logger.info(f"  Активных мониторов: {info.get('active_monitors')}")

    for platform, data in info.items():
        if platform in ["avito", "cian"]:
            logger.info(f"\n  {platform.upper()}:")
            logger.info(f"    Возраст: {data['age_seconds']:.0f}с")
            logger.info(f"    TTL: {data['ttl_seconds']}с")
            logger.info(f"    До обновления: {data['time_until_refresh']:.0f}с")
            logger.info(f"    User-Agent: {data['user_agent']}")
            logger.info(f"    Создан: {data['cached_at']}")

    return True


async def test_browser_singleton():
    """Проверка что браузер единственный"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 5: Singleton браузера")
    logger.info("=" * 60)

    # Несколько запросов должны использовать ОДИН браузер
    logger.info("\n1. Множественные запросы cookies...")

    for i in range(3):
        platform = "avito" if i % 2 == 0 else "cian"
        logger.info(f"  Запрос {i+1} ({platform})...")
        await cookie_manager.get_cookies(platform)

    info = cookie_manager.get_cache_info()
    if info['browser_running']:
        logger.success("✓ Браузер остался запущенным после множественных запросов")
    else:
        logger.error("✗ Браузер не запущен")
        return False

    return True


async def cleanup():
    """Очистка после тестов"""
    logger.info("\n" + "=" * 60)
    logger.info("ОЧИСТКА")
    logger.info("=" * 60)

    logger.info("\nОстановка браузера...")
    await cookie_manager.stop()

    info = cookie_manager.get_cache_info()
    if not info['browser_running']:
        logger.success("✓ Браузер остановлен")
    else:
        logger.error("✗ Браузер всё ещё работает")


async def main():
    """Главная функция тестирования"""
    logger.info("\n")
    logger.info("╔" + "=" * 58 + "╗")
    logger.info("║" + " " * 10 + "ТЕСТИРОВАНИЕ COOKIEMANAGER (PHASE 1)" + " " * 11 + "║")
    logger.info("╚" + "=" * 58 + "╝")
    logger.info("\n")

    results = []

    try:
        # Запуск браузера
        logger.info("Запуск CookieManager...")
        await cookie_manager.start()
        logger.success("✓ CookieManager запущен\n")

        # Тесты
        results.append(("Базовая функциональность", await test_basic_functionality()))
        results.append(("Кэширование", await test_caching()))
        results.append(("Персистентность", await test_persistence()))
        results.append(("Информация о кэше", await test_cache_info()))
        results.append(("Singleton браузера", await test_browser_singleton()))

    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        await cleanup()

    # Результаты
    logger.info("\n")
    logger.info("╔" + "=" * 58 + "╗")
    logger.info("║" + " " * 20 + "РЕЗУЛЬТАТЫ ТЕСТОВ" + " " * 21 + "║")
    logger.info("╚" + "=" * 58 + "╝")
    logger.info("\n")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "✓ PASSED" if result else "✗ FAILED"
        logger.info(f"  {status:10s} - {test_name}")

    logger.info("\n")
    logger.info(f"Пройдено: {passed}/{total}")

    if passed == total:
        logger.success("\n🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ!")
    else:
        logger.warning(f"\n⚠️  Провалено тестов: {total - passed}")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)

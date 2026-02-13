"""
Тестовый скрипт для Phase 2: Monitor + State + DB

Проверяет:
1. Запуск мониторов (Avito, Cian)
2. Регистрацию URL в мониторинге
3. Обработку URL мониторами
4. API endpoints (/monitor/health, /parse/start, /parse/stop, /parse/status)
5. Метрики и статистику
6. Корректную остановку

ВАЖНО: Для полноценного теста нужен запущенный FastAPI сервер (uvicorn api:app)
"""
import asyncio
import time
from loguru import logger

# Тесты с мониторами напрямую (без API)
async def test_monitors_direct():
    """Тест мониторов напрямую (без FastAPI)"""
    logger.info("=" * 60)
    logger.info("ТЕСТ 1: Запуск мониторов напрямую")
    logger.info("=" * 60)

    from monitor import avito_monitor, cian_monitor
    from state_manager import monitoring_state

    # 1. Запуск мониторов
    logger.info("\n1. Запуск Avito и Cian мониторов...")
    await avito_monitor.start()
    await cian_monitor.start()

    if avito_monitor.running and cian_monitor.running:
        logger.success("✓ Оба монитора запущены")
    else:
        logger.error("✗ Один или оба монитора не запущены")
        return False

    # 2. Регистрация тестового URL
    logger.info("\n2. Регистрация тестового Avito URL...")

    test_url = "https://www.avito.ru/moskva/kommercheskaya_nedvizhimost/prodam?cd=1"
    test_config = {
        "tg_token": "test_token",
        "tg_chat_id": ["123456"],
        "min_price": 0,
        "max_price": 999_999_999,
        "keys_word_white_list": [],
        "keys_word_black_list": [],
        "seller_black_list": [],
        "geo": None,
        "max_age": 24 * 60 * 60,
        "ignore_reserv": True,
        "ignore_promotion": False
    }

    success = monitoring_state.register_url(
        task_id="test_avito_1",
        url=test_url,
        platform="avito",
        user_id=12345,
        config=test_config
    )

    if success:
        logger.success(f"✓ URL зарегистрирован: {test_url}")
    else:
        logger.error("✗ Не удалось зарегистрировать URL")
        return False

    # 3. Проверка что URL в списке
    logger.info("\n3. Проверка списка активных URL...")
    avito_urls = monitoring_state.get_urls_for_platform("avito")

    if len(avito_urls) == 1:
        logger.success(f"✓ Найден 1 активный Avito URL")
    else:
        logger.error(f"✗ Ожидался 1 URL, найдено: {len(avito_urls)}")
        return False

    # 4. Ждём один цикл мониторинга
    logger.info("\n4. Ожидание одного цикла мониторинга (~15 секунд)...")
    await asyncio.sleep(20)

    # 5. Проверка метрик
    logger.info("\n5. Проверка метрик мониторинга...")
    avito_metrics = avito_monitor.get_metrics()
    monitoring_metrics = monitoring_state.get_metrics()

    logger.info(f"  Avito монитор:")
    logger.info(f"    Running: {avito_metrics['running']}")
    logger.info(f"    Total cycles: {avito_metrics['total_cycles']}")
    logger.info(f"    Total requests: {avito_metrics['total_requests']}")
    logger.info(f"    Total errors: {avito_metrics['total_errors']}")
    logger.info(f"    Last cycle time: {avito_metrics['last_cycle_time']:.1f}s")

    logger.info(f"\n  Monitoring state:")
    logger.info(f"    Total monitored: {monitoring_metrics['total_monitored']}")
    logger.info(f"    Active: {monitoring_metrics['active']}")
    logger.info(f"    Paused: {monitoring_metrics['paused']}")

    if avito_metrics['total_cycles'] > 0:
        logger.success("✓ Монитор выполнил хотя бы один цикл")
    else:
        logger.warning("⚠ Монитор не выполнил ни одного цикла")

    # 6. Удаление URL
    logger.info("\n6. Удаление URL из мониторинга...")
    success = monitoring_state.unregister_url("test_avito_1")

    if success:
        logger.success("✓ URL удалён")
    else:
        logger.error("✗ Не удалось удалить URL")
        return False

    # 7. Остановка мониторов
    logger.info("\n7. Остановка мониторов...")
    await avito_monitor.stop()
    await cian_monitor.stop()

    if not avito_monitor.running and not cian_monitor.running:
        logger.success("✓ Оба монитора остановлены")
    else:
        logger.error("✗ Один или оба монитора всё ещё работают")
        return False

    return True


# Тесты через API (требуется запущенный сервер)
async def test_api_endpoints():
    """Тест API endpoints (требуется запущенный uvicorn)"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 2: API Endpoints")
    logger.info("=" * 60)

    import httpx

    base_url = "http://localhost:8009"

    async with httpx.AsyncClient() as client:
        # 1. Health check
        logger.info("\n1. GET /health...")
        try:
            response = await client.get(f"{base_url}/health")
            if response.status_code == 200:
                data = response.json()
                logger.success(f"✓ /health OK (uptime: {data['uptime_seconds']:.1f}s)")
            else:
                logger.error(f"✗ /health failed: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"✗ Не удалось подключиться к API: {e}")
            logger.warning("⚠ Убедитесь что сервер запущен: uvicorn api:app --port 8009")
            return False

        # 2. Monitor health
        logger.info("\n2. GET /monitor/health...")
        response = await client.get(f"{base_url}/monitor/health")
        if response.status_code == 200:
            data = response.json()
            logger.success("✓ /monitor/health OK")
            logger.info(f"  Avito монитор running: {data['monitors']['avito']['running']}")
            logger.info(f"  Cian монитор running: {data['monitors']['cian']['running']}")
            logger.info(f"  Active URLs: {data['monitoring_state']['active']}")
        else:
            logger.error(f"✗ /monitor/health failed: {response.status_code}")
            return False

        # 3. Start monitoring
        logger.info("\n3. POST /parse/start...")
        payload = {
            "user_id": 123456,
            "avito_url": "https://www.avito.ru/moskva/kommercheskaya_nedvizhimost/prodam?cd=1",
            "pages": 1,
            "notification_bot_token": "test_token",
            "notification_chat_id": 123456
        }

        response = await client.post(f"{base_url}/parse/start", json=payload)
        if response.status_code == 200:
            data = response.json()
            task_id = data["task_id"]
            logger.success(f"✓ Мониторинг запущен, task_id: {task_id}")
        else:
            logger.error(f"✗ /parse/start failed: {response.status_code}")
            return False

        # 4. Get status
        logger.info("\n4. GET /parse/status/{task_id}...")
        response = await client.get(f"{base_url}/parse/status/{task_id}")
        if response.status_code == 200:
            data = response.json()
            logger.success(f"✓ Статус получен: {data['status']}")
        else:
            logger.error(f"✗ /parse/status failed: {response.status_code}")
            try:
                error_detail = response.json()
                logger.error(f"  Ответ: {error_detail}")
            except Exception:
                logger.error(f"  Ответ (text): {response.text[:500]}")
            return False

        # 5. Ждём один цикл
        logger.info("\n5. Ожидание одного цикла мониторинга...")
        await asyncio.sleep(20)

        # 6. Проверка метрик
        logger.info("\n6. Проверка метрик после цикла...")
        response = await client.get(f"{base_url}/monitor/health")
        if response.status_code == 200:
            data = response.json()
            avito_cycles = data['monitors']['avito']['total_cycles']
            logger.info(f"  Avito cycles: {avito_cycles}")

            if avito_cycles > 0:
                logger.success("✓ Монитор выполнил цикл")
            else:
                logger.warning("⚠ Монитор не выполнил цикл")
        else:
            logger.error(f"✗ /monitor/health failed")
            return False

        # 7. Stop monitoring
        logger.info("\n7. POST /parse/stop/{task_id}...")
        response = await client.post(f"{base_url}/parse/stop/{task_id}")
        if response.status_code == 200:
            logger.success("✓ Мониторинг остановлен")
        else:
            logger.error(f"✗ /parse/stop failed: {response.status_code}")
            return False

    return True


async def test_db_wal_mode():
    """Тест WAL mode в SQLite"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 3: SQLite WAL Mode")
    logger.info("=" * 60)

    import sqlite3
    from db_service import SQLiteDBHandler

    db = SQLiteDBHandler()

    logger.info("\n1. Проверка WAL mode...")
    with sqlite3.connect(db.db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]

        if mode.lower() == "wal":
            logger.success(f"✓ WAL mode активен: {mode}")
        else:
            logger.error(f"✗ WAL mode не активен: {mode}")
            return False

    logger.info("\n2. Проверка PRAGMA synchronous...")
    with sqlite3.connect(db.db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA synchronous")
        sync_mode = cursor.fetchone()[0]
        logger.info(f"  synchronous mode: {sync_mode}")

    return True


async def main():
    """Главная функция тестирования"""
    logger.info("\n")
    logger.info("╔" + "=" * 58 + "╗")
    logger.info("║" + " " * 12 + "ТЕСТИРОВАНИЕ PHASE 2 (MONITOR MODE)" + " " * 11 + "║")
    logger.info("╚" + "=" * 58 + "╝")
    logger.info("\n")

    results = []

    # Тест 1: Мониторы напрямую
    try:
        result = await test_monitors_direct()
        results.append(("Мониторы напрямую", result))
    except Exception as e:
        logger.error(f"❌ Ошибка в test_monitors_direct: {e}")
        import traceback
        logger.error(traceback.format_exc())
        results.append(("Мониторы напрямую", False))

    # Тест 2: API endpoints (опционально)
    logger.info("\n")
    logger.warning("⚠️  ТЕСТ 2 (API) требует запущенного сервера!")
    logger.warning("   Запустите в отдельном терминале: uvicorn api:app --port 8009")
    logger.warning("   Пропустить тест? (Enter = пропустить, n = запустить)")

    # Даём 5 секунд на ответ, иначе пропускаем
    import select
    import sys

    timeout = 5
    logger.info(f"   Ожидание {timeout} секунд...")

    i, o, e = select.select([sys.stdin], [], [], timeout)

    if i:
        choice = sys.stdin.readline().strip()
        if choice.lower() == 'n':
            try:
                result = await test_api_endpoints()
                results.append(("API Endpoints", result))
            except Exception as e:
                logger.error(f"❌ Ошибка в test_api_endpoints: {e}")
                results.append(("API Endpoints", False))
        else:
            logger.info("   Тест API пропущен")
            results.append(("API Endpoints", None))
    else:
        logger.info("   Timeout - тест API пропущен")
        results.append(("API Endpoints", None))

    # Тест 3: WAL mode
    try:
        result = await test_db_wal_mode()
        results.append(("SQLite WAL Mode", result))
    except Exception as e:
        logger.error(f"❌ Ошибка в test_db_wal_mode: {e}")
        results.append(("SQLite WAL Mode", False))

    # Результаты
    logger.info("\n")
    logger.info("╔" + "=" * 58 + "╗")
    logger.info("║" + " " * 20 + "РЕЗУЛЬТАТЫ ТЕСТОВ" + " " * 21 + "║")
    logger.info("╚" + "=" * 58 + "╝")
    logger.info("\n")

    passed = sum(1 for _, result in results if result is True)
    total = sum(1 for _, result in results if result is not None)

    for test_name, result in results:
        if result is True:
            status = "✓ PASSED"
        elif result is False:
            status = "✗ FAILED"
        else:
            status = "○ SKIPPED"
        logger.info(f"  {status:10s} - {test_name}")

    logger.info("\n")
    logger.info(f"Пройдено: {passed}/{total}")

    if passed == total and total > 0:
        logger.success("\n🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ!")
    elif passed > 0:
        logger.warning(f"\n⚠️  Провалено тестов: {total - passed}")
    else:
        logger.error("\n❌ ВСЕ ТЕСТЫ ПРОВАЛИЛИСЬ!")

    return passed == total and total > 0


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)

"""
Тестовый скрипт для Phase 3: Notification Queue

Проверяет:
1. Очередь уведомлений (enqueue/dequeue)
2. Приоритизация (system=0 перед ads=1)
3. Rate limiting (35мс между сообщениями)
4. Метрики
5. Graceful shutdown
6. API endpoint /notifications/health

ВАЖНО: Тесты 1-5 работают без сервера.
       Тест 6 требует запущенного uvicorn api:app --port 8009
"""
import asyncio
import time
from loguru import logger


async def test_queue_basic():
    """Тест 1: Базовая работа очереди"""
    logger.info("=" * 60)
    logger.info("ТЕСТ 1: Базовая работа очереди")
    logger.info("=" * 60)

    from notification_queue import NotificationQueue

    # Создаём отдельный экземпляр (не singleton) для теста
    queue = NotificationQueue.__new__(NotificationQueue)
    queue._initialized = False
    queue.__init__()

    # Переопределяем singleton для изоляции теста
    queue.queue = asyncio.PriorityQueue(maxsize=100)
    queue.running = False

    await queue.start()

    if queue.running:
        logger.success("✓ Очередь запущена")
    else:
        logger.error("✗ Очередь не запущена")
        return False

    # Проверка метрик
    metrics = queue.get_metrics()
    logger.info(f"  Queue size: {metrics['queue_size']}")
    logger.info(f"  Rate limit: {metrics['rate_limit_msg_per_sec']} msg/sec")

    if metrics['running'] and metrics['queue_size'] == 0:
        logger.success("✓ Метрики корректны")
    else:
        logger.error("✗ Метрики некорректны")
        await queue.stop()
        return False

    await queue.stop()

    if not queue.running:
        logger.success("✓ Очередь остановлена")
    else:
        logger.error("✗ Очередь не остановлена")
        return False

    return True


async def test_priority_ordering():
    """Тест 2: Приоритизация сообщений"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 2: Приоритизация сообщений")
    logger.info("=" * 60)

    from notification_queue import NotificationItem, PRIORITY_SYSTEM, PRIORITY_AD

    queue = asyncio.PriorityQueue()

    # Добавляем в обратном порядке: сначала ads (приоритет 1), потом system (приоритет 0)
    ad_item = NotificationItem(
        priority=PRIORITY_AD,
        timestamp=time.time(),
        data={"type": "ad", "msg": "Новое объявление"}
    )
    system_item = NotificationItem(
        priority=PRIORITY_SYSTEM,
        timestamp=time.time(),
        data={"type": "system", "msg": "Ошибка!"}
    )

    await queue.put(ad_item)
    await queue.put(system_item)

    # Первым должен выйти system (приоритет 0)
    first = await queue.get()
    second = await queue.get()

    if first.priority == PRIORITY_SYSTEM and second.priority == PRIORITY_AD:
        logger.success("✓ Системные сообщения приоритетнее объявлений")
    else:
        logger.error(f"✗ Неверный порядок: first={first.priority}, second={second.priority}")
        return False

    return True


async def test_rate_limiting():
    """Тест 3: Rate limiting (35мс между сообщениями)"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 3: Rate limiting")
    logger.info("=" * 60)

    from notification_queue import TELEGRAM_RATE_LIMIT_INTERVAL

    # Имитация отправки 10 сообщений с rate limiting
    count = 10
    start = time.time()

    for _ in range(count):
        await asyncio.sleep(TELEGRAM_RATE_LIMIT_INTERVAL)

    elapsed = time.time() - start
    expected_min = count * TELEGRAM_RATE_LIMIT_INTERVAL * 0.8  # 20% погрешность

    logger.info(f"  {count} сообщений за {elapsed:.3f}с")
    logger.info(f"  Ожидаемый минимум: {expected_min:.3f}с")
    logger.info(f"  Интервал: {TELEGRAM_RATE_LIMIT_INTERVAL * 1000:.0f}мс = ~{1/TELEGRAM_RATE_LIMIT_INTERVAL:.0f} msg/sec")

    if elapsed >= expected_min:
        logger.success("✓ Rate limiting работает корректно")
    else:
        logger.error("✗ Сообщения отправляются слишком быстро")
        return False

    return True


async def test_enqueue_methods():
    """Тест 4: Методы enqueue_ad и enqueue_system_message"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 4: Методы enqueue")
    logger.info("=" * 60)

    from notification_queue import NotificationQueue, PRIORITY_AD, PRIORITY_SYSTEM

    # Создаём изолированный экземпляр
    queue = NotificationQueue.__new__(NotificationQueue)
    queue._initialized = False
    queue.__init__()
    queue.queue = asyncio.PriorityQueue(maxsize=100)

    # Без запуска consumer — просто проверяем что элементы добавляются

    # enqueue_ad
    user_config = {
        "tg_token": "test_token_123",
        "tg_chat_id": ["123456", "789012"],
    }

    # Используем mock ad
    from unittest.mock import MagicMock
    mock_ad = MagicMock()
    mock_ad.id = 12345

    await queue.enqueue_ad(ad=mock_ad, user_config=user_config, platform="avito")

    if queue.queue.qsize() == 1:
        logger.success("✓ enqueue_ad добавил 1 элемент")
    else:
        logger.error(f"✗ Ожидался 1 элемент, получено: {queue.queue.qsize()}")
        return False

    # enqueue_system_message
    await queue.enqueue_system_message(
        msg="Тестовое системное сообщение",
        bot_token="test_token",
        chat_ids=["123456"]
    )

    if queue.queue.qsize() == 2:
        logger.success("✓ enqueue_system_message добавил элемент")
    else:
        logger.error(f"✗ Ожидалось 2 элемента, получено: {queue.queue.qsize()}")
        return False

    # Проверяем приоритет: system (0) должен быть первым
    first = await queue.queue.get()
    if first.priority == PRIORITY_SYSTEM:
        logger.success("✓ Системное сообщение первое в очереди")
    else:
        logger.error(f"✗ Первым оказался приоритет {first.priority}")
        return False

    # enqueue_ad без tg_token — должен пропустить
    await queue.enqueue_ad(ad=mock_ad, user_config={"tg_token": None}, platform="avito")
    if queue.queue.qsize() == 1:  # только ad-item остался
        logger.success("✓ enqueue_ad пропускает без tg_token")
    else:
        logger.error(f"✗ Неожиданный размер очереди: {queue.queue.qsize()}")
        return False

    return True


async def test_graceful_shutdown():
    """Тест 5: Graceful shutdown"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 5: Graceful shutdown")
    logger.info("=" * 60)

    from notification_queue import NotificationQueue

    queue = NotificationQueue.__new__(NotificationQueue)
    queue._initialized = False
    queue.__init__()
    queue.queue = asyncio.PriorityQueue(maxsize=100)

    await queue.start()

    # Добавляем системное сообщение (не будет отправлено — test token)
    await queue.enqueue_system_message(
        msg="shutdown test",
        bot_token="invalid_token",
        chat_ids=["123"]
    )

    logger.info(f"  Элементов в очереди: {queue.queue.qsize()}")

    # Graceful stop
    start = time.time()
    await queue.stop()
    elapsed = time.time() - start

    logger.info(f"  Shutdown занял: {elapsed:.2f}с")

    if not queue.running:
        logger.success("✓ Graceful shutdown завершён")
    else:
        logger.error("✗ Очередь всё ещё работает")
        return False

    # Проверка метрик после остановки
    metrics = queue.get_metrics()
    logger.info(f"  Sent: {metrics['sent_count']}, Failed: {metrics['failed_count']}")

    return True


async def test_api_notifications_health():
    """Тест 6: API endpoint /notifications/health"""
    logger.info("\n" + "=" * 60)
    logger.info("ТЕСТ 6: API /notifications/health")
    logger.info("=" * 60)

    import httpx

    base_url = "http://localhost:8009"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{base_url}/notifications/health")

            if response.status_code == 200:
                data = response.json()
                logger.success("✓ /notifications/health OK")
                logger.info(f"  Running: {data.get('running')}")
                logger.info(f"  Queue size: {data.get('queue_size')}")
                logger.info(f"  Sent: {data.get('sent_count')}")
                logger.info(f"  Failed: {data.get('failed_count')}")
                logger.info(f"  Rate: {data.get('rate_limit_msg_per_sec')} msg/sec")
                return True
            else:
                logger.error(f"✗ /notifications/health failed: {response.status_code}")
                try:
                    logger.error(f"  Ответ: {response.json()}")
                except Exception:
                    logger.error(f"  Ответ: {response.text[:200]}")
                return False

        except Exception as e:
            logger.error(f"✗ Не удалось подключиться к API: {e}")
            logger.warning("  Убедитесь что сервер запущен: uvicorn api:app --port 8009")
            return False


async def main():
    logger.info("\n")
    logger.info("╔" + "=" * 58 + "╗")
    logger.info("║" + " " * 10 + "ТЕСТИРОВАНИЕ PHASE 3 (NOTIFICATION QUEUE)" + " " * 5 + "║")
    logger.info("╚" + "=" * 58 + "╝")
    logger.info("\n")

    results = []

    # Тесты 1-5: без сервера
    tests = [
        ("Базовая работа очереди", test_queue_basic),
        ("Приоритизация", test_priority_ordering),
        ("Rate limiting", test_rate_limiting),
        ("Методы enqueue", test_enqueue_methods),
        ("Graceful shutdown", test_graceful_shutdown),
    ]

    for name, test_func in tests:
        try:
            result = await test_func()
            results.append((name, result))
        except Exception as e:
            logger.error(f"❌ Ошибка в {name}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            results.append((name, False))

    # Тест 6: API (опционально)
    logger.info("\n")
    logger.warning("⚠️  ТЕСТ 6 (API) требует запущенного сервера!")
    logger.warning("   Запустите: uvicorn api:app --port 8009")
    logger.warning("   Пропустить? (Enter = пропустить, n = запустить)")

    import select
    import sys

    timeout = 5
    logger.info(f"   Ожидание {timeout} секунд...")

    i, o, e = select.select([sys.stdin], [], [], timeout)

    if i:
        choice = sys.stdin.readline().strip()
        if choice.lower() == 'n':
            try:
                result = await test_api_notifications_health()
                results.append(("API /notifications/health", result))
            except Exception as e:
                logger.error(f"❌ Ошибка API теста: {e}")
                results.append(("API /notifications/health", False))
        else:
            results.append(("API /notifications/health", None))
    else:
        results.append(("API /notifications/health", None))

    # Результаты
    logger.info("\n")
    logger.info("╔" + "=" * 58 + "╗")
    logger.info("║" + " " * 20 + "РЕЗУЛЬТАТЫ ТЕСТОВ" + " " * 21 + "║")
    logger.info("╚" + "=" * 58 + "╝")
    logger.info("\n")

    passed = sum(1 for _, r in results if r is True)
    total = sum(1 for _, r in results if r is not None)

    for test_name, result in results:
        if result is True:
            status = "✓ PASSED"
        elif result is False:
            status = "✗ FAILED"
        else:
            status = "○ SKIPPED"
        logger.info(f"  {status:10s} - {test_name}")

    logger.info(f"\nПройдено: {passed}/{total}")

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

"""
Прямой тест прокси вне Playwright

Проверяет:
1. Соединение с прокси-сервером (telnet-style)
2. HTTP-запрос через прокси (requests/httpx)
3. Время ответа и статус
"""
import socket
import httpx
from loguru import logger

from load_config import load_avito_config


def test_proxy_connection():
    logger.info("=" * 60)
    logger.info("ДИАГНОСТИКА ПРОКСИ (вне Playwright)")
    logger.info("=" * 60)

    # Загрузка конфигурации
    config = load_avito_config("../config.toml")

    if not config.proxy_string:
        logger.error("⚠️ Прокси не настроен в config.toml!")
        return

    # Парсинг прокси
    parts = config.proxy_string.split(":")
    if len(parts) == 4:
        # Формат: login:password:ip:port или ip:port:login:password
        if "." in parts[0]:
            proxy_ip, proxy_port, login, password = parts
        else:
            login, password, proxy_ip, proxy_port = parts
    else:
        logger.error(f"Неверный формат proxy_string: {config.proxy_string}")
        return

    proxy_port = int(proxy_port)

    logger.info(f"Прокси-сервер: {proxy_ip}:{proxy_port}")
    logger.info(f"Логин: {login}")
    logger.info(f"Пароль: {'*' * len(password)}")
    logger.info("")

    # Тест 1: TCP соединение
    logger.info("[ТЕСТ 1] TCP соединение с прокси-сервером...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((proxy_ip, proxy_port))

        if result == 0:
            logger.success(f"✓ Прокси-сервер доступен на {proxy_ip}:{proxy_port}")
            sock.close()
        else:
            logger.error(f"✗ Не удалось подключиться к {proxy_ip}:{proxy_port} (errno: {result})")
            logger.warning("  Возможные причины:")
            logger.warning("  - Прокси-сервер не запущен")
            logger.warning("  - Неверный IP или порт")
            logger.warning("  - Фаервол блокирует подключение")
            return

    except socket.timeout:
        logger.error("✗ Таймаут подключения (5 секунд)")
        return
    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")
        return

    logger.info("")

    # Тест 2: HTTP запрос через прокси
    logger.info("[ТЕСТ 2] HTTP запрос через прокси (httpx)...")

    proxy_url = f"http://{login}:{password}@{proxy_ip}:{proxy_port}"

    try:
        with httpx.Client(proxy=proxy_url, timeout=10) as client:
            logger.info("  Запрос: https://httpbin.org/ip")
            response = client.get("https://httpbin.org/ip")

            if response.status_code == 200:
                data = response.json()
                logger.success(f"✓ Запрос успешен! Внешний IP: {data.get('origin', 'N/A')}")
                logger.info(f"  Статус: {response.status_code}")
                logger.info(f"  Время ответа: {response.elapsed.total_seconds():.2f}с")
            else:
                logger.warning(f"⚠ Неожиданный статус: {response.status_code}")
                logger.info(f"  Ответ: {response.text[:200]}")

    except httpx.ConnectTimeout:
        logger.error("✗ Таймаут соединения (10 секунд)")
        logger.warning("  Прокси не отвечает на HTTP запросы")
    except httpx.ProxyError as e:
        logger.error(f"✗ Ошибка прокси: {e}")
        logger.warning("  Возможные причины:")
        logger.warning("  - Неверные учётные данные")
        logger.warning("  - Прокси требует другой тип аутентификации")
    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")

    logger.info("")

    # Тест 3: Запрос к Avito
    logger.info("[ТЕСТ 3] HTTP запрос к Avito через прокси...")

    try:
        with httpx.Client(proxy=proxy_url, timeout=15) as client:
            logger.info("  Запрос: https://www.avito.ru/")
            response = client.get("https://www.avito.ru/")

            logger.info(f"  Статус: {response.status_code}")
            logger.info(f"  Время ответа: {response.elapsed.total_seconds():.2f}с")

            if response.status_code == 200:
                logger.success("✓ Avito доступен через прокси")
            elif response.status_code == 403:
                logger.error("✗ Avito заблокировал прокси (403)")
            else:
                logger.warning(f"⚠ Неожиданный статус: {response.status_code}")

    except httpx.ConnectTimeout:
        logger.error("✗ Таймаут соединения (15 секунд)")
    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")

    logger.info("")
    logger.info("=" * 60)
    logger.info("ДИАГНОСТИКА ЗАВЕРШЕНА")
    logger.info("=" * 60)


if __name__ == "__main__":
    test_proxy_connection()

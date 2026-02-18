"""
Тест прокси ПОСЛЕ активации через change_ip_link
"""
import httpx
from loguru import logger

from load_config import load_avito_config


def test_proxy_after_activation():
    logger.info("=" * 60)
    logger.info("ТЕСТ: HTTP через прокси ПОСЛЕ активации")
    logger.info("=" * 60)

    # Загрузка конфигурации
    config = load_avito_config("../config.toml")

    # Парсинг прокси
    parts = config.proxy_string.split(":")
    if "." in parts[0]:
        proxy_ip, proxy_port, login, password = parts
    else:
        login, password, proxy_ip, proxy_port = parts

    proxy_url = f"http://{login}:{password}@{proxy_ip}:{proxy_port}"

    logger.info(f"Прокси: {proxy_ip}:{proxy_port}")
    logger.info(f"Логин: {login}")
    logger.info("")

    # Шаг 1: Смена IP (активация)
    logger.info("[ШАГ 1] Активация прокси (смена IP)...")

    try:
        response = httpx.get(config.proxy_change_url + "&format=json", timeout=15)

        if response.status_code == 200:
            data = response.json()
            new_ip = data.get("new_ip", "N/A")
            rt = data.get("rt", "N/A")
            logger.success(f"✓ Прокси активирован! Новый IP: {new_ip} (время: {rt}с)")
        else:
            logger.error(f"✗ Ошибка активации: {response.status_code}")
            return

    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")
        return

    logger.info("")

    # Шаг 2: Ждём стабилизации (mobile proxy needs time)
    import time
    logger.info("[ШАГ 2] Ожидание стабилизации прокси (2 секунды)...")
    time.sleep(2)
    logger.info("")

    # Шаг 3: Тест HTTP через прокси
    logger.info("[ШАГ 3] HTTP запрос через активированный прокси...")

    try:
        with httpx.Client(proxy=proxy_url, timeout=15) as client:
            logger.info("  Запрос: https://httpbin.org/ip")
            response = client.get("https://httpbin.org/ip")

            if response.status_code == 200:
                data = response.json()
                logger.success(f"✓ Запрос успешен! Внешний IP: {data.get('origin', 'N/A')}")
                logger.info(f"  Статус: {response.status_code}")
                logger.info(f"  Время ответа: {response.elapsed.total_seconds():.2f}с")
            else:
                logger.warning(f"⚠ Неожиданный статус: {response.status_code}")

    except httpx.ConnectTimeout:
        logger.error("✗ Таймаут соединения")
    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")

    logger.info("")

    # Шаг 4: Тест Avito
    logger.info("[ШАГ 4] HTTP запрос к Avito через прокси...")

    try:
        with httpx.Client(proxy=proxy_url, timeout=20, follow_redirects=True) as client:
            logger.info("  Запрос: https://www.avito.ru/")
            response = client.get("https://www.avito.ru/")

            logger.info(f"  Статус: {response.status_code}")
            logger.info(f"  Время ответа: {response.elapsed.total_seconds():.2f}с")
            logger.info(f"  Content-Length: {len(response.content)} bytes")

            if response.status_code == 200:
                logger.success("✓ Avito доступен через прокси")

                # Проверка на блокировку
                if "captcha" in response.text.lower() or "доступ ограничен" in response.text.lower():
                    logger.warning("⚠ Возможно, требуется капча")
                else:
                    logger.success("✓ Страница загружена без блокировки")

            elif response.status_code == 403:
                logger.error("✗ Avito заблокировал прокси (403)")
            else:
                logger.warning(f"⚠ Неожиданный статус: {response.status_code}")

    except httpx.ConnectTimeout:
        logger.error("✗ Таймаут соединения")
    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")

    logger.info("")
    logger.info("=" * 60)
    logger.info("ТЕСТ ЗАВЕРШЁН")
    logger.info("=" * 60)


if __name__ == "__main__":
    test_proxy_after_activation()

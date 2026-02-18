"""
Тест прокси через curl_cffi (именно эта библиотека используется в парсерах)
"""
from curl_cffi import requests
from loguru import logger

from load_config import load_avito_config


def test_proxy_curlcffi():
    logger.info("=" * 60)
    logger.info("ТЕСТ: curl_cffi (библиотека парсеров)")
    logger.info("=" * 60)

    # Загрузка конфигурации
    config = load_avito_config("../config.toml")

    # Парсинг прокси
    parts = config.proxy_string.split(":")
    if "." in parts[0]:
        proxy_ip, proxy_port, login, password = parts
    else:
        login, password, proxy_ip, proxy_port = parts

    logger.info(f"Прокси: {proxy_ip}:{proxy_port}")
    logger.info(f"Логин: {login}")
    logger.info("")

    # Шаг 1: Смена IP
    logger.info("[ШАГ 1] Смена IP...")

    try:
        response = requests.get(config.proxy_change_url + "&format=json", timeout=15)

        if response.status_code == 200:
            data = response.json()
            new_ip = data.get("new_ip", "N/A")
            logger.success(f"✓ Новый IP: {new_ip}")
        else:
            logger.error(f"✗ Ошибка: {response.status_code}")
            return

    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")
        return

    logger.info("")

    # Шаг 2: Ожидание
    import time
    logger.info("[ШАГ 2] Ожидание 3 секунды...")
    time.sleep(3)
    logger.info("")

    # Шаг 3: Тест через curl_cffi с разными форматами прокси
    logger.info("[ШАГ 3] Тест curl_cffi...")

    # Формат 1: http://login:password@ip:port
    proxy_url = f"http://{login}:{password}@{proxy_ip}:{proxy_port}"

    proxies = {
        "http": proxy_url,
        "https": proxy_url
    }

    logger.info(f"  Прокси формат: http://login:password@ip:port")
    logger.info(f"  Запрос: https://httpbin.org/ip")

    try:
        response = requests.get(
            "https://httpbin.org/ip",
            proxies=proxies,
            timeout=15,
            impersonate="chrome110"
        )

        if response.status_code == 200:
            data = response.json()
            logger.success(f"✓ Успешно! Внешний IP: {data.get('origin', 'N/A')}")
        else:
            logger.warning(f"⚠ Статус: {response.status_code}")

    except requests.errors.ConnectError as e:
        logger.error(f"✗ Ошибка подключения: {e}")
    except requests.errors.Timeout:
        logger.error("✗ Таймаут")
    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")

    logger.info("")

    # Шаг 4: Тест Avito
    logger.info("[ШАГ 4] Тест Avito через curl_cffi...")

    try:
        response = requests.get(
            "https://www.avito.ru/",
            proxies=proxies,
            timeout=20,
            impersonate="chrome110"
        )

        logger.info(f"  Статус: {response.status_code}")
        logger.info(f"  Content-Length: {len(response.content)} bytes")

        if response.status_code == 200:
            logger.success("✓ Avito доступен")

            # Проверка блокировки
            if "captcha" in response.text.lower():
                logger.warning("⚠ Требуется капча")
            elif "доступ ограничен" in response.text.lower():
                logger.error("✗ Доступ ограничен")
            else:
                logger.success("✓ Страница загружена успешно")

        elif response.status_code == 403:
            logger.error("✗ Блокировка 403")
        else:
            logger.warning(f"⚠ Статус: {response.status_code}")

    except requests.errors.ConnectError as e:
        logger.error(f"✗ Ошибка подключения: {e}")
    except requests.errors.Timeout:
        logger.error("✗ Таймаут")
    except Exception as e:
        logger.error(f"✗ Ошибка: {e}")

    logger.info("")
    logger.info("=" * 60)
    logger.info("ТЕСТ ЗАВЕРШЁН")
    logger.info("=" * 60)


if __name__ == "__main__":
    test_proxy_curlcffi()

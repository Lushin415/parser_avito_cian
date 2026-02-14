"""
Проверка загрузки прокси из config.toml
"""
from loguru import logger
from load_config import load_avito_config, load_cian_config

logger.info("Проверка загрузки прокси из config.toml")
logger.info("=" * 60)

# Avito
try:
    avito_config = load_avito_config("config.toml")
    logger.info("\n[AVITO]")
    logger.info(f"  proxy_string: {avito_config.proxy_string}")
    logger.info(f"  proxy_change_url: {avito_config.proxy_change_url}")

    if avito_config.proxy_string:
        # Скрываем пароль
        parts = avito_config.proxy_string.split(":")
        if len(parts) >= 4:
            masked = f"{parts[0]}:{parts[1]}:***:***"
        else:
            masked = avito_config.proxy_string
        logger.success(f"  ✓ Прокси настроен: {masked}")
    else:
        logger.warning("  ⚠ Прокси НЕ настроен")

except Exception as e:
    logger.error(f"Ошибка загрузки Avito config: {e}")

# Cian
try:
    cian_config = load_cian_config("config.toml")
    logger.info("\n[CIAN]")
    logger.info(f"  proxy_string: {cian_config.proxy_string}")
    logger.info(f"  proxy_change_url: {cian_config.proxy_change_url}")

    if cian_config.proxy_string:
        # Скрываем пароль
        parts = cian_config.proxy_string.split(":")
        if len(parts) >= 4:
            masked = f"{parts[0]}:{parts[1]}:***:***"
        else:
            masked = cian_config.proxy_string
        logger.success(f"  ✓ Прокси настроен: {masked}")
    else:
        logger.warning("  ⚠ Прокси НЕ настроен")

except Exception as e:
    logger.error(f"Ошибка загрузки Cian config: {e}")

logger.info("\n" + "=" * 60)
logger.info("Проверка завершена")

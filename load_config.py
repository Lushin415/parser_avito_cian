import tomllib
from pathlib import Path

import tomli_w

from dto import AvitoConfig, CianConfig


def load_avito_config(path: str = "config.toml") -> AvitoConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    avito_data = data.get("avito", {})

    # Phase 2: urls может отсутствовать в config.toml (передаётся через API)
    if "urls" not in avito_data:
        avito_data["urls"] = []

    return AvitoConfig(**avito_data)


def save_avito_config(config: dict):
    with Path("config.toml").open("wb") as f:
        tomli_w.dump(config, f)


def load_cian_config(path: str = "config.toml") -> CianConfig:
    """Загружает конфигурацию для парсера Циан"""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    cian_data = data.get("cian", {})

    # Phase 2: urls может отсутствовать в config.toml (передаётся через API)
    if "urls" not in cian_data:
        cian_data["urls"] = []

    # Если уведомления не заданы для Cian - берём из общих (avito)
    avito_data = data.get("avito", {})
    if not cian_data.get("tg_token") and avito_data.get("tg_token"):
        cian_data["tg_token"] = avito_data["tg_token"]
        cian_data["tg_chat_id"] = avito_data["tg_chat_id"]

    if not cian_data.get("vk_token") and avito_data.get("vk_token"):
        cian_data["vk_token"] = avito_data["vk_token"]
        cian_data["vk_user_id"] = avito_data["vk_user_id"]

    # Phase 2: Если прокси не заданы для Cian - берём из Avito
    if not cian_data.get("proxy_string") and avito_data.get("proxy_string"):
        cian_data["proxy_string"] = avito_data["proxy_string"]
        cian_data["proxy_change_url"] = avito_data["proxy_change_url"]

    return CianConfig(**cian_data)
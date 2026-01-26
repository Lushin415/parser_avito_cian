import tomllib
from pathlib import Path

import tomli_w

from dto import AvitoConfig, CianConfig


def load_avito_config(path: str = "config.toml") -> AvitoConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return AvitoConfig(**data["avito"])


def save_avito_config(config: dict):
    with Path("config.toml").open("wb") as f:
        tomli_w.dump(config, f)


def load_cian_config(path: str = "config.toml") -> CianConfig:
    """Загружает конфигурацию для парсера Циан"""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    cian_data = data.get("cian", {})

    # Если уведомления не заданы для Cian - берём из общих (avito)
    avito_data = data.get("avito", {})
    if not cian_data.get("tg_token") and avito_data.get("tg_token"):
        cian_data["tg_token"] = avito_data["tg_token"]
        cian_data["tg_chat_id"] = avito_data["tg_chat_id"]

    if not cian_data.get("vk_token") and avito_data.get("vk_token"):
        cian_data["vk_token"] = avito_data["vk_token"]
        cian_data["vk_user_id"] = avito_data["vk_user_id"]

    return CianConfig(**cian_data)
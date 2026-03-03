"""
Тесты обработки ошибок мониторинга и уведомлений о приостановке задач.

Запуск (из директории parser_avito_cian/):
    python Tests/test_ip_block.py

Покрытые сценарии:
    1.  increment_error: первые 4 ошибки — active, snapshot=None
    2.  increment_error: 5-я ошибка → paused + snapshot
    3.  increment_error: snapshot содержит config (для отправки уведомления)
    4.  increment_error: статус paused сохраняется в БД
    5.  increment_error: paused URL отсутствует в get_urls_for_platform
    6.  increment_error: несуществующий task_id → None (без краша)
    7.  _send_pause_notification: отправляется каждому chat_id
    8.  _send_pause_notification: текст содержит task_id
    9.  _send_pause_notification: inline-кнопки resume / stop
    10. _send_pause_notification: без tg_token — не отправляем
    11. _send_pause_notification: без chat_ids — не отправляем
    12. _send_pause_notification: ошибка API не крашит процесс
    13. record_check: сбрасывает счётчик ошибок
    14. record_check: аккумулирует notifications_sent
    15. record_check: обновляет last_check
    16. record_check: статистика сохраняется в БД
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from loguru import logger

# Корень проекта в sys.path (чтобы работали все локальные импорты)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# avito_parser.py вызывает logger.add("logs/app.log") при импорте —
# в Docker лог-файл принадлежит root и недоступен для записи.
# Подавляем создание файловых sink'ов до любых локальных импортов.
from loguru import logger as _loguru_logger
_orig_logger_add = _loguru_logger.add
def _patched_logger_add(sink, *args, **kwargs):
    if isinstance(sink, str):   # файловый sink — пропускаем в тестах
        return 0
    return _orig_logger_add(sink, *args, **kwargs)
_loguru_logger.add = _patched_logger_add

PASS = "✅"
FAIL = "❌"

_results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = ""):
    icon = PASS if condition else FAIL
    _results.append((name, condition, detail))
    logger.info(f"  {icon}  {name}" + (f" — {detail}" if detail else ""))
    return condition


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные фабрики
# ──────────────────────────────────────────────────────────────────────────────

def _make_monitoring_state(tmp_db: str):
    """MonitoringStateManager с изолированной БД"""
    from state_manager import MonitoringStateManager
    return MonitoringStateManager(db_name=tmp_db)


def _register_url(state, task_id="task_test", tg_token="tok", chat_ids=None):
    state.register_url(
        task_id=task_id,
        url="https://www.avito.ru/test",
        platform="avito",
        user_id=123456,
        config={
            "tg_token": tg_token,
            "tg_chat_id": chat_ids or ["123456"],
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1–6. increment_error: накопление ошибок и автопауза
# ──────────────────────────────────────────────────────────────────────────────

def test_increment_error():
    logger.info("\n── 1–6. increment_error: накопление ошибок и автопауза ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = os.path.join(tmpdir, "test.db")
        state = _make_monitoring_state(tmp_db)
        _register_url(state, tg_token="bot_token_abc", chat_ids=["111", "222"])

        # Тест 1: первые 4 ошибки — статус active, snapshot=None
        for i in range(4):
            snapshot = state.increment_error("task_test", f"error_{i+1}")
            ok = snapshot is None and state.get_status("task_test") == "active"
            check(f"Ошибка #{i+1} → active, snapshot=None", ok)

        # Тест 2: 5-я ошибка → paused + snapshot
        snapshot = state.increment_error("task_test", "5th error")
        check("Ошибка #5 → статус paused", state.get_status("task_test") == "paused")
        check("Ошибка #5 → snapshot не None", snapshot is not None)

        # Тест 3: snapshot содержит нужные поля
        check("Snapshot содержит task_id", snapshot is not None and snapshot.get("task_id") == "task_test")
        check("Snapshot содержит config", snapshot is not None and "config" in snapshot)
        check(
            "Snapshot config содержит tg_token",
            snapshot is not None and snapshot["config"].get("tg_token") == "bot_token_abc"
        )
        check("Snapshot статус = paused", snapshot is not None and snapshot.get("status") == "paused")

        # Тест 4: статус persisted в БД
        with sqlite3.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT status FROM monitored_urls WHERE task_id='task_test'"
            ).fetchone()
        check("Статус 'paused' сохранён в БД", row is not None and row[0] == "paused")

        # Тест 5: paused URL не возвращается в get_urls_for_platform
        active = state.get_urls_for_platform("avito")
        check("Paused URL отсутствует в списке активных", not any(u["task_id"] == "task_test" for u in active))

        # Тест 6: несуществующий task_id — graceful
        result = state.increment_error("nonexistent", "err")
        check("increment_error для несуществующего task_id → None", result is None)


# ──────────────────────────────────────────────────────────────────────────────
# 7–12. _send_pause_notification: отправка Telegram-уведомления
# ──────────────────────────────────────────────────────────────────────────────

async def test_send_pause_notification():
    logger.info("\n── 7–12. _send_pause_notification ──")
    from monitor import _send_pause_notification

    def _mock_http_client(post_mock):
        """Возвращает контекстный менеджер-мок httpx.AsyncClient"""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        post_mock.return_value = mock_resp
        client = AsyncMock()
        client.post = post_mock
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    # Тест 7: отправляется каждому chat_id
    url_data = {
        "task_id": "avito_abc123",
        "config": {"tg_token": "bot_token", "tg_chat_id": ["111", "222", "333"]},
    }
    post_mock = AsyncMock()
    client = _mock_http_client(post_mock)
    with patch("monitor.httpx.AsyncClient", return_value=client):
        await _send_pause_notification(url_data)
    check("Отправляется 3 сообщения (по chat_id)", post_mock.call_count == 3)

    # Тест 8: текст содержит task_id
    url_data = {
        "task_id": "avito_UNIQUE_ID",
        "config": {"tg_token": "token", "tg_chat_id": ["123"]},
    }
    post_mock = AsyncMock()
    client = _mock_http_client(post_mock)
    with patch("monitor.httpx.AsyncClient", return_value=client):
        await _send_pause_notification(url_data)
    call_json = post_mock.call_args_list[0][1]["json"]
    check("Текст содержит task_id", "avito_UNIQUE_ID" in call_json["text"])
    check("parse_mode=HTML", call_json.get("parse_mode") == "HTML")

    # Тест 9: inline-кнопки resume и stop
    url_data = {
        "task_id": "task_XYZ",
        "config": {"tg_token": "token", "tg_chat_id": ["123"]},
    }
    post_mock = AsyncMock()
    client = _mock_http_client(post_mock)
    with patch("monitor.httpx.AsyncClient", return_value=client):
        await _send_pause_notification(url_data)
    body = post_mock.call_args_list[0][1]["json"]
    buttons = body.get("reply_markup", {}).get("inline_keyboard", [[]])[0]
    callbacks = [btn.get("callback_data", "") for btn in buttons]
    check(
        "Кнопка 'Возобновить' с правильным callback",
        any("resume_realty_task_task_XYZ" in cb for cb in callbacks)
    )
    check(
        "Кнопка 'Остановить' с правильным callback",
        any("stop_realty_task_task_XYZ" in cb for cb in callbacks)
    )
    check("Есть inline_keyboard", len(buttons) >= 2)

    # Тест 10: без tg_token → не вызывает AsyncClient
    url_data_no_token = {
        "task_id": "t1",
        "config": {"tg_token": None, "tg_chat_id": ["123"]},
    }
    with patch("monitor.httpx.AsyncClient") as cls_mock:
        await _send_pause_notification(url_data_no_token)
    check("Без tg_token → AsyncClient не создаётся", not cls_mock.called)

    # Тест 11: без chat_ids → не вызывает AsyncClient
    url_data_no_chats = {
        "task_id": "t2",
        "config": {"tg_token": "token", "tg_chat_id": []},
    }
    with patch("monitor.httpx.AsyncClient") as cls_mock:
        await _send_pause_notification(url_data_no_chats)
    check("Без chat_ids → AsyncClient не создаётся", not cls_mock.called)

    # Тест 12: ошибка Telegram API не крашит процесс
    url_data_err = {
        "task_id": "t3",
        "config": {"tg_token": "token", "tg_chat_id": ["123"]},
    }
    post_mock_err = AsyncMock(side_effect=Exception("Telegram unavailable"))
    client_err = AsyncMock()
    client_err.post = post_mock_err
    client_err.__aenter__ = AsyncMock(return_value=client_err)
    client_err.__aexit__ = AsyncMock(return_value=False)
    crashed = False
    try:
        with patch("monitor.httpx.AsyncClient", return_value=client_err):
            await _send_pause_notification(url_data_err)
    except Exception:
        crashed = True
    check("Ошибка Telegram API не крашит процесс", not crashed)


# ──────────────────────────────────────────────────────────────────────────────
# 13–16. record_check: статистика проверок
# ──────────────────────────────────────────────────────────────────────────────

def test_record_check():
    logger.info("\n── 13–16. record_check: статистика проверок ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = os.path.join(tmpdir, "test.db")
        state = _make_monitoring_state(tmp_db)
        _register_url(state)

        # Тест 13: record_check сбрасывает счётчик ошибок
        state.increment_error("task_test", "e1")
        state.increment_error("task_test", "e2")
        assert state.get_url_data("task_test")["error_count"] == 2
        state.record_check("task_test", new_items_count=0)
        check("record_check сбрасывает error_count в 0",
              state.get_url_data("task_test")["error_count"] == 0)

        # Тест 14: notifications_sent аккумулируется
        state.record_check("task_test", new_items_count=3)
        state.record_check("task_test", new_items_count=5)
        state.record_check("task_test", new_items_count=2)
        check("notifications_sent = 3+5+2 = 10",
              state.get_url_data("task_test")["notifications_sent"] == 10)

        # Тест 15: last_check устанавливается
        check("last_check не None после record_check",
              state.get_url_data("task_test")["last_check"] is not None)

        # Тест 16: статистика сохраняется в БД
        with sqlite3.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT notifications_sent, last_check FROM monitored_urls WHERE task_id='task_test'"
            ).fetchone()
        check("notifications_sent сохранён в БД", row is not None and row[0] == 10)
        check("last_check сохранён в БД", row is not None and row[1] is not None)


# ──────────────────────────────────────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("ТЕСТ: Ошибки мониторинга и уведомления о паузе")
    logger.info("=" * 60)

    test_increment_error()
    await test_send_pause_notification()
    test_record_check()

    # Итог
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = total - passed

    logger.info("\n" + "=" * 60)
    logger.info(f"ИТОГО: {passed}/{total} тестов прошло")
    if failed:
        logger.warning(f"ПРОВАЛЕНО: {failed}")
        for name, ok, detail in _results:
            if not ok:
                logger.error(f"  {FAIL}  {name}" + (f" — {detail}" if detail else ""))
    else:
        logger.success("Все тесты прошли!")
    logger.info("=" * 60)

    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)

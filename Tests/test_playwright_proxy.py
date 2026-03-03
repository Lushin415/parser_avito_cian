"""
Тесты Playwright-прокси архитектуры (Шаги 1, 3, 4 рефакторинга).

Запуск (из директории parser_avito_cian/):
    python Tests/test_playwright_proxy.py

Покрытые сценарии:
    cookie_manager.get_html():
        1.  IP_BLOCK по заголовку "проблема с ip"
        2.  IP_BLOCK по заголовку "докажите, что вы человек"
        3.  IP_BLOCK по HTTP 403
        4.  IP_BLOCK по HTTP 429
        5.  Успешный fetch → возвращает HTML-строку
        6.  Playwright exception (не IP_BLOCK) → None, без краша
        7.  context.close() вызывается в finally при любом исходе (IP_BLOCK)
        8.  browser=None при вызове → _start_browser() запускается

    AvitoMonitor._process_url (fetch-слой):
        9.  get_html=None → increment_error("fetch_html_empty"), total_requests не растёт
        10. get_html=IP_BLOCK → _block_detected=True, increment_error НЕ вызывается
        11. get_html=другое исключение → increment_error(str(e)), _block_detected=False

    BaseMonitor.start() — корректная инициализация прокси:
        12. proxy передан → cookie_manager._proxy установлен ДО acquire()
        13. proxy=None → cookie_manager._proxy не перезаписывается

    ProxyManager:
        14. handle_block без прокси → sleep(NO_PROXY_PAUSE), счётчик блокировок растёт
        15. _do_rotate → change_ip_link 200 + прокси жив → True
        16. _do_rotate → все попытки возвращают 500 → False
        17. После MAX_ROTATION_ATTEMPTS неудач подряд → state=FAILED
        18. wait_if_not_ready в состоянии FAILED → False немедленно
        19. wait_if_not_ready в состоянии ACTIVE → True немедленно
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loguru import logger as _loguru_logger
_orig_logger_add = _loguru_logger.add
def _patched_logger_add(sink, *args, **kwargs):
    if isinstance(sink, str):
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

def _make_cookie_manager(browser=None):
    """CookieManager без синглтона, с предустановленным (или None) браузером."""
    from cookie_manager import CookieManager
    cm = object.__new__(CookieManager)
    cm._browser_lock = asyncio.Lock()
    cm._lock = asyncio.Lock()
    cm._clients = 0
    cm._cache = {}
    cm._proxy = None
    cm._fetch_cooldown = {}
    cm._cooldown_duration = 30
    cm.ttl = 3600
    cm.refresh_threshold = 300
    cm._refresh_task = None
    cm.browser = browser
    return cm


def _make_playwright_mocks(title="Авито", html="<html>ok</html>", status=200):
    """
    Возвращает (browser, context, page, response) — набор AsyncMock/MagicMock
    для Playwright-цепочки: browser → context → page → response.
    """
    response = MagicMock()
    response.status = status

    page = AsyncMock()
    page.title = AsyncMock(return_value=title)
    page.content = AsyncMock(return_value=html)
    page.goto = AsyncMock(return_value=response)

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    return browser, context, page, response


def _make_proxy_manager(proxy=None):
    """ProxyManager без синглтона — изолированный экземпляр для тестов."""
    from proxy_manager import ProxyManager, ProxyState
    pm = object.__new__(ProxyManager)
    pm._initialized = True
    pm._lock = asyncio.Lock()
    pm._ready_event = asyncio.Event()
    pm._ready_event.set()
    pm._state = ProxyState.ACTIVE
    pm._consecutive_failures = 0
    pm._proxy = proxy
    pm._no_proxy_block_count = 0
    return pm


def _make_proxy():
    from dto import Proxy
    return Proxy(
        proxy_string="user:pass@1.2.3.4:8080",
        change_ip_link="http://proxy-provider.com/change?token=abc",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1–8. cookie_manager.get_html()
# ──────────────────────────────────────────────────────────────────────────────

async def test_get_html():
    logger.info("\n── 1–8. get_html: Playwright-блок и успешный fetch ──")

    # Тест 1: IP_BLOCK по заголовку "проблема с ip"
    browser, context, page, _ = _make_playwright_mocks(title="Проблема с IP")
    cm = _make_cookie_manager(browser=browser)
    raised = False
    try:
        await cm.get_html("https://www.avito.ru/test")
    except Exception as e:
        raised = "IP_BLOCK" in str(e)
    check("IP_BLOCK по заголовку 'проблема с ip'", raised)
    check("context.close() вызван (тест 1)", context.close.called)

    # Тест 2: IP_BLOCK по заголовку "докажите, что вы человек"
    browser, context, page, _ = _make_playwright_mocks(title="Докажите, что вы человек")
    cm = _make_cookie_manager(browser=browser)
    raised = False
    try:
        await cm.get_html("https://www.avito.ru/test")
    except Exception as e:
        raised = "IP_BLOCK" in str(e)
    check("IP_BLOCK по заголовку 'докажите, что вы человек'", raised)
    check("context.close() вызван (тест 2)", context.close.called)

    # Тест 3: IP_BLOCK по HTTP 403
    browser, context, page, _ = _make_playwright_mocks(title="Авито", status=403)
    cm = _make_cookie_manager(browser=browser)
    raised = False
    try:
        await cm.get_html("https://www.avito.ru/test")
    except Exception as e:
        raised = "IP_BLOCK" in str(e)
    check("IP_BLOCK по HTTP 403", raised)

    # Тест 4: IP_BLOCK по HTTP 429
    browser, context, page, _ = _make_playwright_mocks(title="Авито", status=429)
    cm = _make_cookie_manager(browser=browser)
    raised = False
    try:
        await cm.get_html("https://www.avito.ru/test")
    except Exception as e:
        raised = "IP_BLOCK" in str(e)
    check("IP_BLOCK по HTTP 429", raised)

    # Тест 5: Успешный fetch → возвращает HTML
    expected_html = "<html><body>Авито объявления</body></html>"
    browser, context, page, _ = _make_playwright_mocks(
        title="Авито", html=expected_html, status=200
    )
    cm = _make_cookie_manager(browser=browser)
    result = await cm.get_html("https://www.avito.ru/test")
    check("Успешный fetch → возвращает HTML", result == expected_html)
    check("context.close() вызван после успеха", context.close.called)

    # Тест 6: Playwright exception (не IP_BLOCK) → None, без краша
    browser, context, page, _ = _make_playwright_mocks()
    page.goto.side_effect = Exception("TimeoutError: navigation timeout")
    cm = _make_cookie_manager(browser=browser)
    crashed = False
    result = "sentinel"
    try:
        result = await cm.get_html("https://www.avito.ru/test")
    except Exception:
        crashed = True
    check("Playwright exception → None, без краша", not crashed and result is None)
    check("context.close() вызван при Playwright exception", context.close.called)

    # Тест 7: context.close() вызывается в finally при IP_BLOCK
    browser, context, page, _ = _make_playwright_mocks(title="Проблема с IP")
    cm = _make_cookie_manager(browser=browser)
    try:
        await cm.get_html("https://www.avito.ru/test")
    except Exception:
        pass
    check("context.close() в finally при IP_BLOCK", context.close.called)

    # Тест 8: browser=None → _start_browser() вызывается
    cm = _make_cookie_manager(browser=None)
    start_called = False
    real_browser, _, _, _ = _make_playwright_mocks()

    async def fake_start_browser():
        nonlocal start_called
        start_called = True
        cm.browser = real_browser  # имитируем запуск браузера

    cm._start_browser = fake_start_browser
    await cm.get_html("https://www.avito.ru/test")
    check("browser=None → _start_browser() вызван", start_called)


# ──────────────────────────────────────────────────────────────────────────────
# 9–11. AvitoMonitor._process_url — fetch-слой
# ──────────────────────────────────────────────────────────────────────────────

async def test_process_url_fetch_layer():
    logger.info("\n── 9–11. AvitoMonitor._process_url: fetch-слой ──")

    from monitor import AvitoMonitor

    url_data = {
        "url": "https://www.avito.ru/test",
        "user_id": 123,
        "task_id": "t1",
        "config": {},
        "started_at": 0,
    }

    def _make_avito():
        m = object.__new__(AvitoMonitor)
        m._block_detected = False
        m.platform = "avito"
        m.total_requests = 0
        return m

    # Тест 9: get_html=None → increment_error("fetch_html_empty")
    m = _make_avito()
    with patch("monitor.cookie_manager") as mock_cm, \
         patch("monitor.monitoring_state") as mock_state:
        mock_cm.get_html = AsyncMock(return_value=None)
        await m._process_url(url_data)
    calls = [str(c) for c in mock_state.increment_error.call_args_list]
    check("get_html=None → increment_error('fetch_html_empty')",
          any("fetch_html_empty" in c for c in calls))
    check("get_html=None → total_requests не растёт", m.total_requests == 0)

    # Тест 10: get_html=IP_BLOCK → _block_detected=True, increment_error НЕ вызван
    m = _make_avito()
    with patch("monitor.cookie_manager") as mock_cm, \
         patch("monitor.monitoring_state") as mock_state:
        mock_cm.get_html = AsyncMock(side_effect=Exception("IP_BLOCK"))
        await m._process_url(url_data)
    check("get_html=IP_BLOCK → _block_detected=True", m._block_detected is True)
    check("get_html=IP_BLOCK → increment_error НЕ вызван",
          not mock_state.increment_error.called)
    check("get_html=IP_BLOCK → total_requests не растёт", m.total_requests == 0)

    # Тест 11: get_html=другое исключение → increment_error(str(e)), _block_detected=False
    m = _make_avito()
    with patch("monitor.cookie_manager") as mock_cm, \
         patch("monitor.monitoring_state") as mock_state:
        mock_cm.get_html = AsyncMock(side_effect=Exception("Connection refused"))
        await m._process_url(url_data)
    check("get_html=exception → _block_detected остаётся False",
          m._block_detected is False)
    calls = [str(c) for c in mock_state.increment_error.call_args_list]
    check("get_html=exception → increment_error с текстом ошибки",
          any("Connection refused" in c for c in calls))


# ──────────────────────────────────────────────────────────────────────────────
# 12–13. BaseMonitor.start() — прокси устанавливается до acquire()
# ──────────────────────────────────────────────────────────────────────────────

async def test_start_proxy_init():
    logger.info("\n── 12–13. BaseMonitor.start(): прокси до acquire() ──")

    import monitor as monitor_module
    from monitor import AvitoMonitor
    from dto import Proxy

    proxy_obj = Proxy(
        proxy_string="user:pass@1.2.3.4:8080",
        change_ip_link="http://x.com",
    )
    original_proxy = monitor_module.cookie_manager._proxy

    def _make_avito(proxy):
        m = object.__new__(AvitoMonitor)
        m.running = False
        m.task = None
        m.platform = "avito"
        m.proxy = proxy
        return m

    # Тест 12: proxy передан → _proxy установлен ДО acquire()
    m = _make_avito(proxy_obj)
    proxy_at_acquire = []

    async def fake_acquire_12():
        # Фиксируем значение _proxy в момент вызова acquire()
        proxy_at_acquire.append(monitor_module.cookie_manager._proxy)

    with patch.object(monitor_module.cookie_manager, "acquire", AsyncMock(side_effect=fake_acquire_12)), \
         patch("asyncio.create_task", return_value=MagicMock()):
        await m.start()

    check("_proxy установлен ДО acquire()",
          len(proxy_at_acquire) > 0 and proxy_at_acquire[0] is proxy_obj)

    monitor_module.cookie_manager._proxy = original_proxy  # восстанавливаем

    # Тест 13: proxy=None → _proxy не перезаписывается
    m = _make_avito(None)
    sentinel = object()
    monitor_module.cookie_manager._proxy = sentinel
    proxy_at_acquire2 = []

    async def fake_acquire_13():
        proxy_at_acquire2.append(monitor_module.cookie_manager._proxy)

    with patch.object(monitor_module.cookie_manager, "acquire", AsyncMock(side_effect=fake_acquire_13)), \
         patch("asyncio.create_task", return_value=MagicMock()):
        await m.start()

    check("proxy=None → _proxy не перезаписан",
          len(proxy_at_acquire2) > 0 and proxy_at_acquire2[0] is sentinel)

    monitor_module.cookie_manager._proxy = original_proxy  # восстанавливаем


# ──────────────────────────────────────────────────────────────────────────────
# 14–19. ProxyManager
# ──────────────────────────────────────────────────────────────────────────────

async def test_proxy_manager():
    logger.info("\n── 14–19. ProxyManager: ротация и состояния ──")

    from proxy_manager import ProxyState, NO_PROXY_PAUSE, MAX_ROTATION_ATTEMPTS

    # Тест 14: handle_block без прокси → sleep(NO_PROXY_PAUSE), счётчик растёт
    pm = _make_proxy_manager(proxy=None)
    slept = []
    with patch("proxy_manager.asyncio.sleep", new=AsyncMock(side_effect=lambda s: slept.append(s))), \
         patch.object(pm, "_notify_no_proxy", new=AsyncMock()):
        await pm.handle_block("avito", [])
    check(f"handle_block без прокси → sleep({NO_PROXY_PAUSE})", NO_PROXY_PAUSE in slept)
    check("no_proxy_block_count увеличился до 1", pm._no_proxy_block_count == 1)

    # Тест 15: _do_rotate → change_ip_link 200 + прокси жив → True
    pm = _make_proxy_manager(proxy=_make_proxy())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"new_ip": "5.6.7.8"}
    with patch("proxy_manager.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client
        with patch.object(pm, "_check_proxy_alive", new=AsyncMock(return_value=True)):
            result = await pm._do_rotate()
    check("_do_rotate: 200 + alive → True", result is True)

    # Тест 16: _do_rotate → все попытки 500 → False
    pm = _make_proxy_manager(proxy=_make_proxy())
    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 500
    with patch("proxy_manager.httpx.AsyncClient") as mock_cls, \
         patch("asyncio.sleep", new=AsyncMock()):
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp_500)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client
        result = await pm._do_rotate()
    check("_do_rotate: все попытки 500 → False", result is False)

    # Тест 17: MAX_ROTATION_ATTEMPTS неудач подряд → state=FAILED
    pm = _make_proxy_manager(proxy=_make_proxy())
    with patch.object(pm, "_do_rotate", new=AsyncMock(return_value=False)), \
         patch.object(pm, "_notify_failed", new=AsyncMock()), \
         patch("asyncio.sleep", new=AsyncMock()):
        for _ in range(MAX_ROTATION_ATTEMPTS):
            await pm.handle_block("avito", [])
    check(f"После {MAX_ROTATION_ATTEMPTS} неудач → FAILED",
          pm._state == ProxyState.FAILED)

    # Тест 18: wait_if_not_ready в FAILED → False немедленно
    pm = _make_proxy_manager()
    pm._state = ProxyState.FAILED
    result = await pm.wait_if_not_ready()
    check("wait_if_not_ready в FAILED → False", result is False)

    # Тест 19: wait_if_not_ready в ACTIVE → True немедленно
    pm = _make_proxy_manager()
    pm._state = ProxyState.ACTIVE
    result = await pm.wait_if_not_ready()
    check("wait_if_not_ready в ACTIVE → True", result is True)


# ──────────────────────────────────────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("ТЕСТ: Playwright-прокси архитектура")
    logger.info("=" * 60)

    await test_get_html()
    await test_process_url_fetch_layer()
    await test_start_proxy_init()
    await test_proxy_manager()

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

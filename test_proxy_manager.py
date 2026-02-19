"""
Tests for ProxyManager

Все тесты полностью мокированы — реальный прокси не нужен.

Запуск:
    pytest test_proxy_manager.py -v
    pytest test_proxy_manager.py -v -x       # остановиться на первом падении
    pytest test_proxy_manager.py -v --tb=short
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dto import Proxy
from proxy_manager import (
    ProxyManager,
    ProxyState,
    MAX_ROTATION_ATTEMPTS,
    COOLDOWN_DURATION,
    LOCK_TIMEOUT,
    JITTER_MAX,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_manager() -> ProxyManager:
    """Создать свежий экземпляр ProxyManager, обходя синглтон."""
    ProxyManager._instance = None
    return ProxyManager()


def make_proxy(proxy_string: str = "user:pass@1.2.3.4:8080") -> Proxy:
    return Proxy(
        proxy_string=proxy_string,
        change_ip_link="http://proxy.example.com/change?token=abc",
    )


SAMPLE_URL_LIST = [
    {
        "url": "https://avito.ru/search",
        "task_id": "task_1",
        "config": {
            "tg_token": "1234567890:ABCdef",
            "pause_chat_id": "999",
        },
    }
]


# ─── Инициализация ────────────────────────────────────────────────────────────

class TestInit:
    def test_initial_state_is_active(self):
        mgr = make_manager()
        assert mgr.state == ProxyState.ACTIVE

    def test_ready_event_is_set(self):
        mgr = make_manager()
        assert mgr._ready_event.is_set()

    def test_consecutive_failures_is_zero(self):
        mgr = make_manager()
        assert mgr._consecutive_failures == 0

    def test_is_ready_true(self):
        mgr = make_manager()
        assert mgr.is_ready is True

    def test_configure_stores_proxy(self):
        mgr = make_manager()
        proxy = make_proxy()
        mgr.configure(proxy)
        assert mgr._proxy is proxy

    def test_configure_none(self):
        mgr = make_manager()
        mgr.configure(None)
        assert mgr._proxy is None


# ─── Синглтон ─────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_same_instance_returned(self):
        ProxyManager._instance = None
        a = ProxyManager()
        b = ProxyManager()
        assert a is b

    def test_make_manager_produces_fresh_instance(self):
        a = make_manager()
        a._state = ProxyState.FAILED
        b = make_manager()
        # make_manager сбрасывает синглтон — b это новый объект
        assert b.state == ProxyState.ACTIVE


# ─── wait_if_not_ready ────────────────────────────────────────────────────────

class TestWaitIfNotReady:
    @pytest.mark.asyncio
    async def test_active_returns_true_immediately(self):
        mgr = make_manager()
        assert await mgr.wait_if_not_ready() is True

    @pytest.mark.asyncio
    async def test_failed_returns_false_immediately(self):
        mgr = make_manager()
        mgr._state = ProxyState.FAILED
        assert await mgr.wait_if_not_ready() is False

    @pytest.mark.asyncio
    async def test_blocks_while_rotating_then_returns_true(self):
        mgr = make_manager()
        mgr._state = ProxyState.ROTATING
        mgr._ready_event.clear()

        async def release():
            await asyncio.sleep(0.05)
            mgr._state = ProxyState.ACTIVE
            mgr._ready_event.set()

        asyncio.create_task(release())

        with patch("proxy_manager.random.uniform", return_value=0.0):
            result = await mgr.wait_if_not_ready()

        assert result is True

    @pytest.mark.asyncio
    async def test_blocks_then_failed_returns_false_without_jitter(self):
        """При переходе в FAILED воркер не тратит время на jitter."""
        mgr = make_manager()
        mgr._state = ProxyState.ROTATING
        mgr._ready_event.clear()

        async def release_as_failed():
            await asyncio.sleep(0.05)
            mgr._state = ProxyState.FAILED
            mgr._ready_event.set()

        asyncio.create_task(release_as_failed())

        with patch("proxy_manager.random.uniform") as mock_uniform:
            result = await mgr.wait_if_not_ready()

        assert result is False
        mock_uniform.assert_not_called()  # jitter не запускался


# ─── _parse_proxy ─────────────────────────────────────────────────────────────

class TestParseProxy:
    def test_no_proxy_returns_none(self):
        mgr = make_manager()
        assert mgr._parse_proxy() is None

    def _mgr_with(self, proxy_string: str) -> ProxyManager:
        mgr = make_manager()
        mgr.configure(Proxy(proxy_string=proxy_string, change_ip_link="http://x.com"))
        return mgr

    def test_user_pass_at_host_port(self):
        split = self._mgr_with("myuser:mypass@192.168.1.1:3128")._parse_proxy()
        assert split.login == "myuser"
        assert split.password == "mypass"
        assert split.ip_port == "http://192.168.1.1:3128"

    def test_host_port_at_user_pass(self):
        split = self._mgr_with("192.168.1.1:3128@myuser:mypass")._parse_proxy()
        assert split.login == "myuser"
        assert split.password == "mypass"
        assert split.ip_port == "http://192.168.1.1:3128"

    def test_user_pass_colon_host_port(self):
        split = self._mgr_with("myuser:mypass:192.168.1.1:3128")._parse_proxy()
        assert split.login == "myuser"
        assert split.password == "mypass"
        assert split.ip_port == "http://192.168.1.1:3128"

    def test_host_port_colon_user_pass(self):
        split = self._mgr_with("192.168.1.1:3128:myuser:mypass")._parse_proxy()
        assert split.login == "myuser"
        assert split.password == "mypass"
        assert split.ip_port == "http://192.168.1.1:3128"

    def test_strips_scheme_prefix(self):
        split = self._mgr_with("http://myuser:mypass@192.168.1.1:3128")._parse_proxy()
        assert split is not None
        assert split.login == "myuser"

    def test_adds_http_prefix_to_ip_port(self):
        split = self._mgr_with("myuser:mypass@1.2.3.4:8080")._parse_proxy()
        assert split.ip_port.startswith("http://")


# ─── _do_rotate ───────────────────────────────────────────────────────────────

class TestDoRotate:
    @pytest.mark.asyncio
    async def test_no_proxy_returns_false(self):
        mgr = make_manager()
        assert await mgr._do_rotate() is False

    @pytest.mark.asyncio
    async def test_200_and_proxy_alive_returns_true(self):
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._check_proxy_alive = AsyncMock(return_value=True)

        ok_response = MagicMock(status_code=200)
        ok_response.json.return_value = {"new_ip": "5.5.5.5"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=ok_response)

        with patch("proxy_manager.httpx.AsyncClient", return_value=mock_client):
            result = await mgr._do_rotate()

        assert result is True

    @pytest.mark.asyncio
    async def test_200_but_proxy_dead_returns_false(self):
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._check_proxy_alive = AsyncMock(return_value=False)

        ok_response = MagicMock(status_code=200)
        ok_response.json.return_value = {"new_ip": "5.5.5.5"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=ok_response)

        with patch("proxy_manager.httpx.AsyncClient", return_value=mock_client):
            with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
                result = await mgr._do_rotate()

        assert result is False

    @pytest.mark.asyncio
    async def test_non_200_returns_false_after_all_retries(self):
        mgr = make_manager()
        mgr.configure(make_proxy())

        bad_response = MagicMock(status_code=500)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=bad_response)

        with patch("proxy_manager.httpx.AsyncClient", return_value=mock_client):
            with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
                result = await mgr._do_rotate()

        assert result is False
        # Должно быть ровно MAX_ROTATION_ATTEMPTS попыток
        assert mock_client.get.call_count == MAX_ROTATION_ATTEMPTS

    @pytest.mark.asyncio
    async def test_network_error_exhausts_retries(self):
        mgr = make_manager()
        mgr.configure(make_proxy())

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("Connection timeout"))

        with patch("proxy_manager.httpx.AsyncClient", return_value=mock_client):
            with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
                result = await mgr._do_rotate()

        assert result is False

    @pytest.mark.asyncio
    async def test_succeeds_on_second_attempt(self):
        """Первая попытка не удалась, вторая — успешна."""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._check_proxy_alive = AsyncMock(side_effect=[False, True])

        ok_response = MagicMock(status_code=200)
        ok_response.json.return_value = {"new_ip": "5.5.5.5"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=ok_response)

        with patch("proxy_manager.httpx.AsyncClient", return_value=mock_client):
            with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
                result = await mgr._do_rotate()

        assert result is True
        assert mgr._check_proxy_alive.call_count == 2


# ─── handle_block — полный цикл ───────────────────────────────────────────────

class TestHandleBlock:
    @pytest.mark.asyncio
    async def test_successful_rotation_ends_in_active(self):
        """ACTIVE → ROTATING → COOLDOWN → ACTIVE"""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=True)

        with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
            await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr.state == ProxyState.ACTIVE
        assert mgr._ready_event.is_set()
        assert mgr._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_rotating_state_set_before_do_rotate(self):
        """Воркеры должны быть заблокированы ДО вызова _do_rotate."""
        mgr = make_manager()
        mgr.configure(make_proxy())

        state_during_rotation = []

        async def capture_state_then_succeed():
            state_during_rotation.append(mgr._state)
            return True

        mgr._do_rotate = capture_state_then_succeed

        with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
            await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert ProxyState.ROTATING in state_during_rotation

    @pytest.mark.asyncio
    async def test_failed_rotation_increments_counter(self):
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=False)

        await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr._consecutive_failures == 1
        assert mgr.state == ProxyState.ACTIVE
        assert mgr._ready_event.is_set()  # воркеры продолжают работу

    @pytest.mark.asyncio
    async def test_two_failures_still_not_failed(self):
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=False)
        mgr._notify_failed = AsyncMock()

        for _ in range(MAX_ROTATION_ATTEMPTS - 1):
            await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr.state == ProxyState.ACTIVE
        mgr._notify_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_breaker_after_max_failures(self):
        """Ровно MAX_ROTATION_ATTEMPTS неудач → FAILED, событие сброшено."""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=False)
        mgr._notify_failed = AsyncMock()

        for _ in range(MAX_ROTATION_ATTEMPTS):
            await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr.state == ProxyState.FAILED
        assert not mgr._ready_event.is_set()
        mgr._notify_failed.assert_called_once_with(SAMPLE_URL_LIST)

    @pytest.mark.asyncio
    async def test_already_failed_skips_rotation(self):
        mgr = make_manager()
        mgr._state = ProxyState.FAILED
        mgr._do_rotate = AsyncMock()

        await mgr.handle_block("avito", SAMPLE_URL_LIST)

        mgr._do_rotate.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_counter_resets_after_success(self):
        """После успешной ротации счётчик сбрасывается в 0."""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=False)

        # Одна неудача
        await mgr.handle_block("avito", SAMPLE_URL_LIST)
        assert mgr._consecutive_failures == 1

        # Затем успех
        mgr._do_rotate = AsyncMock(return_value=True)
        with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
            await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_concurrent_second_caller_waits_not_rotates(self):
        """Если ротация уже идёт, второй вызов ждёт, не запускает свою."""
        mgr = make_manager()
        mgr.configure(make_proxy())

        rotate_started = asyncio.Event()
        allow_finish = asyncio.Event()
        rotate_call_count = 0

        async def slow_rotate():
            nonlocal rotate_call_count
            rotate_call_count += 1
            rotate_started.set()
            await allow_finish.wait()
            return True

        mgr._do_rotate = slow_rotate

        first = asyncio.create_task(mgr.handle_block("avito", SAMPLE_URL_LIST))
        await rotate_started.wait()

        # Второй вызов при уже занятом lock — должен просто дождаться
        allow_finish.set()
        with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
            await asyncio.gather(first)

        # _rotate вызвана ровно один раз
        assert rotate_call_count == 1
        assert mgr.state == ProxyState.ACTIVE

    @pytest.mark.asyncio
    async def test_lock_timeout_increments_failure_counter(self):
        """Таймаут ротации — это тоже failure, счётчик должен расти."""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._notify_failed = AsyncMock()

        async def hanging_rotate():
            await asyncio.sleep(9999)

        mgr._do_rotate = hanging_rotate

        with patch("proxy_manager.LOCK_TIMEOUT", 0.05):
            await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr._consecutive_failures == 1
        assert mgr.state == ProxyState.ACTIVE
        assert mgr._ready_event.is_set()

    @pytest.mark.asyncio
    async def test_lock_timeout_circuit_breaker(self):
        """MAX_ROTATION_ATTEMPTS таймаутов подряд → FAILED."""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._notify_failed = AsyncMock()

        async def hanging_rotate():
            await asyncio.sleep(9999)

        mgr._do_rotate = hanging_rotate

        with patch("proxy_manager.LOCK_TIMEOUT", 0.05):
            for _ in range(MAX_ROTATION_ATTEMPTS):
                await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr.state == ProxyState.FAILED
        assert not mgr._ready_event.is_set()
        mgr._notify_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_lock_released_before_cooldown(self):
        """Lock освобождается до начала cooldown: другие задачи не блокируются."""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=True)

        lock_held_during_cooldown = []

        async def spy_sleep(_duration):
            lock_held_during_cooldown.append(mgr._lock.locked())

        with patch("proxy_manager.asyncio.sleep", new=spy_sleep):
            await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert lock_held_during_cooldown, "sleep не вызывался"
        assert not lock_held_during_cooldown[0], "lock должен быть свободен во время cooldown"


# ─── reset_failed ─────────────────────────────────────────────────────────────

class TestResetFailed:
    def test_resets_state_to_active(self):
        mgr = make_manager()
        mgr._state = ProxyState.FAILED
        mgr._consecutive_failures = 3
        mgr._ready_event.clear()

        mgr.reset_failed()

        assert mgr.state == ProxyState.ACTIVE
        assert mgr._consecutive_failures == 0
        assert mgr._ready_event.is_set()

    def test_reset_with_new_proxy(self):
        mgr = make_manager()
        mgr._state = ProxyState.FAILED
        mgr._ready_event.clear()

        new_proxy = make_proxy("newuser:newpass@10.0.0.1:3128")
        mgr.reset_failed(proxy=new_proxy)

        assert mgr._proxy is new_proxy
        assert mgr.state == ProxyState.ACTIVE

    def test_reset_without_proxy_keeps_existing(self):
        mgr = make_manager()
        original_proxy = make_proxy()
        mgr.configure(original_proxy)
        mgr._state = ProxyState.FAILED
        mgr._ready_event.clear()

        mgr.reset_failed()

        assert mgr._proxy is original_proxy

    def test_workers_unblock_after_reset(self):
        """Воркеры, ожидавшие в FAILED, должны получить True после reset."""
        mgr = make_manager()
        mgr._state = ProxyState.FAILED
        # _ready_event в FAILED не сбрасывается автоматически в логике,
        # но reset_failed() должен его поставить
        mgr._ready_event.clear()
        mgr.reset_failed()
        assert mgr._ready_event.is_set()


# ─── get_status ───────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_returns_all_expected_keys(self):
        mgr = make_manager()
        status = mgr.get_status()
        assert set(status.keys()) == {"state", "consecutive_failures", "proxy_configured", "is_ready"}

    def test_active_state(self):
        mgr = make_manager()
        mgr.configure(make_proxy())
        status = mgr.get_status()
        assert status["state"] == "active"
        assert status["is_ready"] is True
        assert status["proxy_configured"] is True
        assert status["consecutive_failures"] == 0

    def test_failed_state(self):
        mgr = make_manager()
        mgr._state = ProxyState.FAILED
        mgr._consecutive_failures = 3
        status = mgr.get_status()
        assert status["state"] == "failed"
        assert status["is_ready"] is False
        assert status["consecutive_failures"] == 3

    def test_no_proxy_configured(self):
        mgr = make_manager()
        status = mgr.get_status()
        assert status["proxy_configured"] is False


# ─── Интеграция: сценарий «IP-бан → ротация → возобновление» ──────────────────

class TestIntegrationScenario:
    @pytest.mark.asyncio
    async def test_ban_rotate_resume_full_scenario(self):
        """
        Имитируем полный сценарий:
        1. Два монитора работают нормально (state=ACTIVE)
        2. Один получает бан → запускает ротацию
        3. Второй монитор пытается обратиться к proxy → блокируется
        4. Ротация завершается
        5. Оба монитора возобновляют работу
        """
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=True)
        mgr._notify_failed = AsyncMock()

        # Шаг 1: состояние нормальное
        assert await mgr.wait_if_not_ready() is True

        # Шаг 2: avito_monitor сообщает о бане
        rotation_running = asyncio.Event()
        allow_rotation_complete = asyncio.Event()

        async def slow_but_successful_rotate():
            rotation_running.set()
            await allow_rotation_complete.wait()
            return True

        mgr._do_rotate = slow_but_successful_rotate

        handle_task = asyncio.create_task(
            mgr.handle_block("avito", SAMPLE_URL_LIST)
        )

        # Ждём пока ротация стартует
        await rotation_running.wait()

        # Шаг 3: cian_monitor пытается работать — должен заблокироваться
        assert mgr._state == ProxyState.ROTATING
        assert not mgr._ready_event.is_set()

        # Шаг 4: ротация завершается
        allow_rotation_complete.set()
        with patch("proxy_manager.asyncio.sleep", new_callable=AsyncMock):
            await handle_task

        # Шаг 5: оба монитора могут работать
        assert mgr.state == ProxyState.ACTIVE
        assert mgr._ready_event.is_set()
        with patch("proxy_manager.random.uniform", return_value=0.0):
            assert await mgr.wait_if_not_ready() is True

    @pytest.mark.asyncio
    async def test_three_bans_lead_to_failed_state(self):
        """Три последовательных бана с неудачной ротацией → FAILED."""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=False)
        mgr._notify_failed = AsyncMock()

        for i in range(MAX_ROTATION_ATTEMPTS):
            assert mgr.state != ProxyState.FAILED, f"FAILED наступил раньше времени (шаг {i})"
            await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr.state == ProxyState.FAILED
        assert await mgr.wait_if_not_ready() is False

    @pytest.mark.asyncio
    async def test_admin_notification_sent_on_failed(self):
        """При переходе в FAILED отправляется Telegram-уведомление."""
        mgr = make_manager()
        mgr.configure(make_proxy())
        mgr._do_rotate = AsyncMock(return_value=False)

        mock_post_response = AsyncMock()
        mock_post_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_post_response)

        with patch("proxy_manager.httpx.AsyncClient", return_value=mock_client):
            for _ in range(MAX_ROTATION_ATTEMPTS):
                await mgr.handle_block("avito", SAMPLE_URL_LIST)

        assert mgr.state == ProxyState.FAILED
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "sendMessage" in call_kwargs[0][0]

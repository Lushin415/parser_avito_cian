Микросервис для мониторинга недвижимости на Avito и Cian. Работает как backend-служба, управляемая через REST API, с поддержкой параллельного парсинга.

## Возможности

*   **Мультиплатформенность:** Одновременный парсинг Avito и Cian в рамках одной задачи.
*   **Параллельность:** Каждый источник (Avito/Cian) обрабатывается в отдельном потоке внутри задачи.
*   **Обход блокировок:** Использование Playwright (Chromium) для получения кук и обхода защиты, поддержка прокси со сменой IP.
*   **Гибкие фильтры:** Цена, площадь, ключевые слова, стоп-слова, география.
*   **Уведомления:** Отправка найденных вариантов в Telegram (через бота пользователя) или VK.
*   **История:** База данных SQLite (`database.db`) для исключения дубликатов.

## Технологии

*   **FastAPI** — API управление задачами.
*   **BeautifulSoup4 / curl-cffi** — парсинг контента.
*   **Playwright** — эмуляция браузера для обхода защиты.
*   **Threading** — параллельное выполнение задач.

## Установка и запуск

### Локально
1. Установите зависимости:
   ```bash
   pip install -r requirements.txt
Установите браузер Playwright:
code
Bash
playwright install chromium
Запустите сервис:
code
Bash
python api.py
Сервис будет доступен на порту 8001. Swagger: http://localhost:8001/docs
Через Docker
Сервис запускается в составе общего стека через docker-compose оркестратора ParserHub.
API Endpoints
POST /parse/start — запуск новой задачи парсинга (принимает ссылки, кол-во страниц и токены уведомлений).
GET /parse/status/{task_id} — текущий прогресс, количество найденных и отфильтрованных объявлений.
POST /parse/stop/{task_id} — принудительная остановка задачи.
GET /health — проверка работоспособности.
Структура логов
Логи работы парсеров сохраняются в папку logs/, подробная информация о запросах выводится в консоль.


Инструкция: очистка перед продакшн

  Выполни на VPS перед первым продакшн-запуском:

  # 1. Остановить сервер (если запущен)
  # Ctrl+C или kill

  # 2. Удалить тестовую БД (пересоздастся автоматически)
  rm database.db

  # 3. Удалить тестовые cookies (обновятся при старте)
  rm -f cookies.json cookies_cian.json

  # 4. Очистить логи
  rm -f logs/app.log

  # 5. Запустить сервер
  uvicorn api:app --host 0.0.0.0 --port 8009

  Одна команда:
  rm -f database.db cookies.json cookies_cian.json logs/app.log && uvicorn api:app --host 0.0.0.0 --port 8009

  Что произойдёт при старте:
  - database.db создастся с чистыми таблицами viewed и monitored_urls
  - cookies.json обновится через Playwright при первом запросе
  - Логи начнутся с нуля
  - Зарегистрированных пользователей будет 0 — регистрируй через API

bash tmp/load_test.sh 


● Валидация прошла без ошибок.

  Что изменилось:

  Dockerfile:
  - Python 3.11 → 3.12
  - Убран entrypoint.sh — CMD запускает uvicorn напрямую на порт 8009
  - Добавлен curl для healthcheck
  - HEALTHCHECK — каждые 30с проверяет /health
  - --no-cache-dir для pip (меньше размер образа)
  - rm -rf /var/lib/apt/lists/* вместо apt-get distclean

  docker-compose.yml:
  - Контейнер realty-monitor вместо parser_avito
  - build из локального Dockerfile (вместо ghcr.io образа)
  - Порт 8009:8009
  - Volumes: config.toml (ro), database.db, cookies.json, cookies_cian.json, logs/
  - restart: unless-stopped — автоперезапуск при крэше
  - TZ=Europe/Moscow
  - Healthcheck

  .dockerignore:
  - Добавлены Tests/, Reports/, tmp/, .idea, .github

  api.py:
  - Порт по умолчанию 8001 → 8009

  ---
  Запуск на VPS:
  # Первый запуск (чистый старт)
  rm -f database.db cookies.json cookies_cian.json
  touch database.db cookies.json cookies_cian.json
  docker compose up -d --build

  # Логи
  docker compose logs -f

  # Проверка здоровья
  curl http://localhost:8009/health
  curl http://localhost:8009/monitor/health
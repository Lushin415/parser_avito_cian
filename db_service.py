import sqlite3
import time

from models import Item


class SQLiteDBHandler:
    """Работа с БД sqlite"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(SQLiteDBHandler, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_name="database.db"):
        if not hasattr(self, "_initialized"):
            self.db_name = db_name
            self._create_table()
            self._initialized = True

    def _create_table(self):
        """Создает таблицу viewed, если она не существует."""
        with sqlite3.connect(self.db_name) as conn:
            # Phase 2: включение WAL mode для concurrent access
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")  # Оптимизация производительности

            cursor = conn.cursor()

            # Проверяем наличие колонки user_id (миграция)
            cursor.execute("PRAGMA table_info(viewed)")
            columns = [row[1] for row in cursor.fetchall()]

            if "user_id" not in columns or "created_at" not in columns:
                if columns:
                    # Таблица существует без нужных колонок — пересоздаём
                    cursor.execute("DROP TABLE viewed")
                cursor.execute(
                    """
                    CREATE TABLE viewed (
                        id INTEGER,
                        price INTEGER,
                        user_id INTEGER,
                        created_at REAL,
                        UNIQUE(id, price, user_id)
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_viewed_lookup ON viewed(id, price, user_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_viewed_cleanup ON viewed(created_at)"
                )
            else:
                # Таблица уже актуальна — убедимся что индексы есть
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_viewed_lookup ON viewed(id, price, user_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_viewed_cleanup ON viewed(created_at)"
                )

            conn.commit()

    def add_record(self, ad: Item, user_id: int = 0):
        """Добавляет новую запись в таблицу viewed."""

        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO viewed (id, price, user_id, created_at) VALUES (?, ?, ?, ?)",
                (ad.id, ad.priceDetailed.value, user_id, time.time()),
            )
            conn.commit()

    def add_record_from_page(self, ads: list[Item], user_id: int = 0):
        """Добавляет несколько записей в таблицу viewed."""
        now = time.time()
        records = [(ad.id, ad.priceDetailed.value, user_id, now) for ad in ads]

        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR IGNORE INTO viewed (id, price, user_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                records,
            )
            conn.commit()

    def record_exists(self, record_id, price, user_id: int = 0):
        """Проверяет, существует ли запись с заданными id, price и user_id."""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM viewed WHERE id = ? AND price = ? AND user_id = ?",
                (record_id, price, user_id),
            )
            return cursor.fetchone() is not None

    def cleanup_old_records(self, max_age_days: int = 7) -> int:
        """Удаляет записи старше max_age_days дней. Возвращает кол-во удалённых."""
        cutoff = time.time() - (max_age_days * 86400)
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM viewed WHERE created_at < ?", (cutoff,))
            deleted = cursor.rowcount
            conn.commit()
            return deleted

"""
Постійне сховище рейтингів у PostgreSQL (Supabase).
Рядок підключення береться зі змінної середовища DATABASE_URL.
"""

import os
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ════════════════════════════ підключення ════════════════════════════

@contextmanager
def _conn():
    con = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ════════════════════════════ ініціалізація ═══════════════════════════

def init_db():
    """Створює таблицю якщо її ще немає. Викликати один раз при старті бота."""
    if not DATABASE_URL:
        raise ValueError(
            "Змінна середовища DATABASE_URL не задана!\n"
            "Додай її у налаштуваннях Railway/Render:\n"
            "  DATABASE_URL=postgresql://postgres:пароль@db.xxxx.supabase.co:5432/postgres"
        )

    with _conn() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    chat_id         BIGINT  NOT NULL,
                    user_id         BIGINT  NOT NULL,
                    name            TEXT    NOT NULL,
                    games_played    INTEGER NOT NULL DEFAULT 0,
                    games_won       INTEGER NOT NULL DEFAULT 0,
                    total_guessed   INTEGER NOT NULL DEFAULT 0,
                    total_explained INTEGER NOT NULL DEFAULT 0,
                    turns_played    INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
    logger.info("✅ PostgreSQL: таблиця ratings готова")


# ════════════════════════════ запис результатів ═══════════════════════

def save_game_results(chat_id: int, players: list[dict]):
    """
    Зберігає підсумки гри для кожного гравця (UPSERT).
    Якщо гравець вже є — додає до його поточної статистики.
    """
    with _conn() as con:
        with con.cursor() as cur:
            for p in players:
                cur.execute("""
                    INSERT INTO ratings
                        (chat_id, user_id, name,
                         games_played, games_won,
                         total_guessed, total_explained, turns_played)
                    VALUES
                        (%(chat_id)s, %(user_id)s, %(name)s,
                         %(games_played)s, %(games_won)s,
                         %(total_guessed)s, %(total_explained)s, %(turns_played)s)
                    ON CONFLICT (chat_id, user_id) DO UPDATE SET
                        name            = EXCLUDED.name,
                        games_played    = ratings.games_played    + EXCLUDED.games_played,
                        games_won       = ratings.games_won       + EXCLUDED.games_won,
                        total_guessed   = ratings.total_guessed   + EXCLUDED.total_guessed,
                        total_explained = ratings.total_explained + EXCLUDED.total_explained,
                        turns_played    = ratings.turns_played    + EXCLUDED.turns_played
                """, {
                    "chat_id":         chat_id,
                    "user_id":         p["user_id"],
                    "name":            p["name"],
                    "games_played":    1,
                    "games_won":       1 if p.get("won") else 0,
                    "total_guessed":   p.get("guessed", 0),
                    "total_explained": p.get("explained", 0),
                    "turns_played":    p.get("turns", 0),
                })


# ════════════════════════════ читання рейтингу ════════════════════════

def get_rating_rows(chat_id: int) -> list[dict]:
    """Повертає рядки відсортовані за кількістю вгаданих слів."""
    with _conn() as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM   ratings
                WHERE  chat_id = %s
                ORDER  BY total_guessed DESC, games_won DESC
            """, (chat_id,))
            return cur.fetchall()

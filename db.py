"""
Постійне сховище рейтингів у PostgreSQL.
Працює з Railway PostgreSQL (postgres.railway.internal:5432)
та будь-якою іншою PostgreSQL базою.
"""

import os
import ssl
import logging
from urllib.parse import urlparse
from contextlib import contextmanager

import pg8000.native

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ════════════════════════════ підключення ════════════════════════════

def _parse_url(url: str) -> dict:
    """Розбирає DATABASE_URL на параметри для pg8000."""
    r = urlparse(url)

    # SSL: вмикаємо для зовнішніх хостів, вимикаємо для внутрішніх Railway
    host = r.hostname or ""
    if "railway.internal" in host or host in ("localhost", "127.0.0.1"):
        ssl_context = None   # внутрішня мережа — без SSL
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ssl_context = ctx

    params = {
        "host":     host,
        "port":     r.port or 5432,
        "database": r.path.lstrip("/"),
        "user":     r.username,
        "password": r.password,
    }
    if ssl_context is not None:
        params["ssl_context"] = ssl_context

    return params


@contextmanager
def _conn():
    if not DATABASE_URL:
        raise ValueError(
            "Змінна середовища DATABASE_URL не задана!\n"
            "Додай її у Railway -> Variables."
        )
    params = _parse_url(DATABASE_URL)
    con = pg8000.native.Connection(**params)
    try:
        yield con
        con.run("COMMIT")
    except Exception:
        try:
            con.run("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


# ════════════════════════════ ініціалізація ═══════════════════════════

def init_db():
    """Створює таблицю якщо її ще немає."""
    with _conn() as con:
        con.run("""
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

def save_game_results(chat_id: int, players: list):
    """Зберігає підсумки гри (UPSERT)."""
    with _conn() as con:
        for p in players:
            con.run(
                """
                INSERT INTO ratings
                    (chat_id, user_id, name,
                     games_played, games_won,
                     total_guessed, total_explained, turns_played)
                VALUES
                    (:chat_id, :user_id, :name,
                     :games_played, :games_won,
                     :total_guessed, :total_explained, :turns_played)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name            = EXCLUDED.name,
                    games_played    = ratings.games_played    + EXCLUDED.games_played,
                    games_won       = ratings.games_won       + EXCLUDED.games_won,
                    total_guessed   = ratings.total_guessed   + EXCLUDED.total_guessed,
                    total_explained = ratings.total_explained + EXCLUDED.total_explained,
                    turns_played    = ratings.turns_played    + EXCLUDED.turns_played
                """,
                chat_id=chat_id,
                user_id=p["user_id"],
                name=p["name"],
                games_played=1,
                games_won=1 if p.get("won") else 0,
                total_guessed=p.get("guessed", 0),
                total_explained=p.get("explained", 0),
                turns_played=p.get("turns", 0),
            )


# ════════════════════════════ читання рейтингу ════════════════════════

def get_rating_rows(chat_id: int) -> list:
    """Повертає список словників відсортований за кількістю вгаданих слів."""
    with _conn() as con:
        rows = con.run(
            """
            SELECT chat_id, user_id, name,
                   games_played, games_won,
                   total_guessed, total_explained, turns_played
            FROM   ratings
            WHERE  chat_id = :chat_id
            ORDER  BY total_guessed DESC, games_won DESC
            """,
            chat_id=chat_id,
        )
        cols = [
            "chat_id", "user_id", "name",
            "games_played", "games_won",
            "total_guessed", "total_explained", "turns_played",
        ]
        return [dict(zip(cols, row)) for row in rows]

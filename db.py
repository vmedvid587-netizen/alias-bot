"""
Постійне сховище рейтингів у PostgreSQL.
"""

import os
import ssl
import logging
from urllib.parse import urlparse
from contextlib import contextmanager

import pg8000.native

logger = logging.getLogger(__name__)


@contextmanager
def _conn():
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не задана!")
    r = urlparse(DATABASE_URL)
    host = r.hostname or ""

    if "railway.internal" in host or host in ("localhost", "127.0.0.1"):
        ssl_ctx = None
    else:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    params = {
        "host":     host,
        "port":     r.port or 5432,
        "database": r.path.lstrip("/"),
        "user":     r.username,
        "password": r.password,
    }
    if ssl_ctx is not None:
        params["ssl_context"] = ssl_ctx

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


def init_db():
    if not os.environ.get("DATABASE_URL"):
        logger.warning("⚠️  DATABASE_URL не задана — рейтинг не зберігатиметься")
        return
    try:
        with _conn() as con:
            con.run("""
            CREATE TABLE IF NOT EXISTS ratings (
                chat_id          BIGINT  NOT NULL,
                user_id          BIGINT  NOT NULL,
                name             TEXT    NOT NULL,
                games_played     INTEGER NOT NULL DEFAULT 0,
                turns_played     INTEGER NOT NULL DEFAULT 0,
                total_explained  INTEGER NOT NULL DEFAULT 0,
                total_guessed    INTEGER NOT NULL DEFAULT 0,
                hearts_received  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        # Додаємо колонку якщо БД стара (без hearts_received)
        try:
            con.run("""
                ALTER TABLE ratings
                ADD COLUMN IF NOT EXISTS hearts_received INTEGER NOT NULL DEFAULT 0
            """)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"⚠️  БД недоступна: {e} — рейтинг не зберігатиметься")
        return
    logger.info("✅ PostgreSQL: таблиця ratings готова")


def save_game_results(chat_id: int, players: list):
    """UPSERT результатів гри."""
    if not os.environ.get("DATABASE_URL"):
        return
    with _conn() as con:
        for p in players:
            con.run(
                """
                INSERT INTO ratings
                    (chat_id, user_id, name,
                     games_played, turns_played,
                     total_explained, total_guessed, hearts_received)
                VALUES
                    (:chat_id, :user_id, :name,
                     :games_played, :turns_played,
                     :total_explained, :total_guessed, :hearts_received)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name            = EXCLUDED.name,
                    games_played    = ratings.games_played    + EXCLUDED.games_played,
                    turns_played    = ratings.turns_played    + EXCLUDED.turns_played,
                    total_explained = ratings.total_explained + EXCLUDED.total_explained,
                    total_guessed   = ratings.total_guessed   + EXCLUDED.total_guessed,
                    hearts_received = ratings.hearts_received + EXCLUDED.hearts_received
                """,
                chat_id=chat_id,
                user_id=p["user_id"],
                name=p["name"],
                games_played=1,
                turns_played=p.get("turns", 0),
                total_explained=p.get("explained", 0),
                total_guessed=p.get("guessed", 0),
                hearts_received=p.get("hearts", 0),
            )


def get_rating_rows(chat_id: int, limit: int = 25) -> list:
    """Топ гравців за кількістю вгаданих слів."""
    if not os.environ.get("DATABASE_URL"):
        return []
    with _conn() as con:
        rows = con.run(
            """
            SELECT user_id, name,
                   games_played, turns_played,
                   total_explained, total_guessed, hearts_received
            FROM   ratings
            WHERE  chat_id = :chat_id
            ORDER  BY total_guessed DESC
            LIMIT  :limit
            """,
            chat_id=chat_id,
            limit=limit,
        )
        cols = ["user_id", "name", "games_played", "turns_played",
                "total_explained", "total_guessed", "hearts_received"]
        return [dict(zip(cols, row)) for row in rows]


def get_user_stats(chat_id: int, user_id: int) -> dict | None:
    """Статистика конкретного гравця в чаті."""
    if not os.environ.get("DATABASE_URL"):
        return None
    with _conn() as con:
        rows = con.run(
            """
            SELECT user_id, name,
                   games_played, turns_played,
                   total_explained, total_guessed, hearts_received
            FROM   ratings
            WHERE  chat_id = :chat_id AND user_id = :user_id
            """,
            chat_id=chat_id,
            user_id=user_id,
        )
        if not rows:
            return None
        cols = ["user_id", "name", "games_played", "turns_played",
                "total_explained", "total_guessed", "hearts_received"]
        return dict(zip(cols, rows[0]))


def get_user_stats_all_chats(user_id: int) -> dict | None:
    """Сумарна статистика гравця по всіх чатах."""
    if not os.environ.get("DATABASE_URL"):
        return None
    with _conn() as con:
        rows = con.run(
            """
            SELECT
                SUM(games_played)    AS games_played,
                SUM(turns_played)    AS turns_played,
                SUM(total_explained) AS total_explained,
                SUM(total_guessed)   AS total_guessed,
                SUM(hearts_received) AS hearts_received
            FROM ratings
            WHERE user_id = :user_id
            """,
            user_id=user_id,
        )
        if not rows or rows[0][0] is None:
            return None
        cols = ["games_played", "turns_played", "total_explained",
                "total_guessed", "hearts_received"]
        return dict(zip(cols, rows[0]))


def record_game_results(chat_id: int, game) -> None:
    """Зберігає підсумки гри в БД."""
    players = [
        {
            "user_id":   p.user_id,
            "name":      p.name,
            "turns":     p.turns_played,
            "explained": p.explained,
            "guessed":   p.guessed,
            "hearts":    p.hearts,
        }
        for p in game.players.values()
    ]
    if players:
        save_game_results(chat_id, players)


def get_rating_text(chat_id: int) -> str:
    rows = get_rating_rows(chat_id, limit=25)
    if not rows:
        return "📊 Рейтингу ще немає — зіграйте хоча б одну гру!"

    lines = [f"🏆 *Топ-{min(len(rows), 25)} гравців*\n"]
    for i, r in enumerate(rows):
        lines.append(
            f"{i + 1}. {r['name']} — "
            f"*{r['total_guessed']}* відп."
            + (f"  💚 {r['hearts_received']}" if r["hearts_received"] else "")
        )
    return "\n".join(lines)

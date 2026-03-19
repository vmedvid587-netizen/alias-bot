"""
Логіка гри Еліас — очки отримує тільки відгадувач
"""

import re
import time
import random
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


class GameState(Enum):
    WAITING     = "waiting"      # збираємо гравців
    PLAYING     = "playing"      # між ходами
    TURN_ACTIVE = "turn_active"  # хід іде
    FINISHED    = "finished"     # гра завершена


@dataclass
class Player:
    user_id: int
    name: str
    guessed: int = 0        # очки (тільки відгадування)
    explained: int = 0      # статистика: скільки слів пояснив (не рахується в score)
    turns_played: int = 0

    @property
    def score(self) -> int:
        return self.guessed


@dataclass
class Turn:
    explainer_id: int
    word: str
    category: str
    start_time: float = field(default_factory=time.time)
    duration: int = 60

    words_guessed: list = field(default_factory=list)   # (word, guesser_id)
    words_skipped: list = field(default_factory=list)

    previous_word: Optional[str] = None
    previous_category: Optional[str] = None
    # user_id того, хто вгадав попереднє слово (щоб зняти очко при "назад")
    previous_guesser_id: Optional[int] = None

    # ID повідомлення-картки в груповому чаті
    group_msg_id: Optional[int] = None

    @property
    def time_left(self) -> int:
        return max(0, int(self.duration - (time.time() - self.start_time)))

    @property
    def is_expired(self) -> bool:
        return time.time() - self.start_time >= self.duration

    @property
    def guessed_count(self) -> int:
        return len(self.words_guessed)

    @property
    def skipped_count(self) -> int:
        return len(self.words_skipped)


@dataclass
class Game:
    chat_id: int
    creator_id: int
    state: GameState = GameState.WAITING

    players: dict = field(default_factory=dict)    # user_id -> Player
    turn_order: list = field(default_factory=list) # [user_id, ...]
    current_turn_index: int = 0
    current_turn: Optional[Turn] = None
    used_words: set = field(default_factory=set)

    target_score: int = 30
    turn_duration: int = 60
    category: Optional[str] = None

    # ── гравці ──────────────────────────────────────────────────────

    def add_player(self, user_id: int, name: str) -> bool:
        if user_id in self.players:
            return False
        self.players[user_id] = Player(user_id=user_id, name=name)
        return True

    # ── черга ───────────────────────────────────────────────────────

    def build_turn_order(self):
        ids = list(self.players.keys())
        random.shuffle(ids)
        self.turn_order = ids

    def get_current_explainer(self) -> Optional["Player"]:
        if not self.turn_order:
            return None
        uid = self.turn_order[self.current_turn_index % len(self.turn_order)]
        return self.players.get(uid)

    def advance_turn(self):
        self.current_turn_index = (self.current_turn_index + 1) % len(self.turn_order)
        self.current_turn = None

    # ── перевірка переможця ─────────────────────────────────────────

    def get_winner(self) -> Optional["Player"]:
        for p in self.players.values():
            if p.score >= self.target_score:
                return p
        return None

    # ── таблиця рахунку ─────────────────────────────────────────────

    def get_scoreboard(self) -> str:
        if not self.players:
            return "Немає гравців."
        ranked = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = ["📊 *Рахунок:*\n"]
        for i, p in enumerate(ranked):
            medal = medals[i] if i < 3 else f"  {i + 1}."
            lines.append(f"{medal} *{p.name}* — {p.score} очок")
        return "\n".join(lines)


# ════════════════════════════ нормалізація ════════════════════════════

def normalize(text: str) -> str:
    """Приводимо текст до нижнього регістру, прибираємо зайві символи."""
    # Залишаємо букви, цифри, апостроф і дефіс (є в укр. словах)
    cleaned = re.sub(r"[^\w'\-]", " ", text.lower())
    # Стискаємо пробіли
    return " ".join(cleaned.split())


def is_correct_guess(message_text: str, word: str) -> bool:
    """
    Повертає True якщо повідомлення містить правильну відповідь.
    Підтримує точний збіг і збіг як частини речення.
    """
    msg_norm  = normalize(message_text)
    word_norm = normalize(word)
    # Точний збіг або слово стоїть як окрема одиниця у реченні
    return msg_norm == word_norm or f" {word_norm} " in f" {msg_norm} "


# ════════════════════════════ Рейтинг чату ════════════════════════════

from db import save_game_results, get_rating_rows


def record_game_results(chat_id: int, game: "Game", winner: Optional["Player"]):
    """Зберігає підсумки гри в SQLite."""
    players = [
        {
            "user_id":   p.user_id,
            "name":      p.name,
            "guessed":   p.guessed,
            "explained": p.explained,
            "turns":     p.turns_played,
            "won":       winner is not None and p.user_id == winner.user_id,
        }
        for p in game.players.values()
    ]
    save_game_results(chat_id, players)


def get_rating_text(chat_id: int) -> str:
    """Читає рейтинг з SQLite і форматує текст."""
    rows = get_rating_rows(chat_id)
    if not rows:
        return "📊 Рейтингу ще немає — зіграйте хоча б одну гру!"

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 *Рейтинг гравців чату*\n"]
    for i, r in enumerate(rows):
        m = medals[i] if i < 3 else f"  {i + 1}."
        win_rate = (
            f"{int(r['games_won'] / r['games_played'] * 100)}%"
            if r["games_played"] else "0%"
        )
        avg = (
            round(r["total_explained"] / r["turns_played"], 1)
            if r["turns_played"] else 0.0
        )
        lines.append(
            f"{m} *{r['name']}*\n"
            f"    🎯 вгадав {r['total_guessed']}  |  "
            f"📢 пояснив {r['total_explained']}  |  "
            f"🎮 {r['games_played']} ігор  |  "
            f"🏅 {win_rate} перемог  |  "
            f"📈 ~{avg} слів/хід"
        )
    return "\n".join(lines)


# ════════════════════════════ Сховище ігор ════════════════════════════

games: dict = {}


def get_game(chat_id: int) -> Optional[Game]:
    return games.get(chat_id)


def create_game(chat_id: int, creator_id: int) -> Game:
    g = Game(chat_id=chat_id, creator_id=creator_id)
    games[chat_id] = g
    return g


def delete_game(chat_id: int):
    games.pop(chat_id, None)

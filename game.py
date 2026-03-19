"""
Логіка гри Еліас — без черги, без таймера ходу
"""

import re
import time
import random
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


class GameState(Enum):
    WAITING     = "waiting"       # очікуємо /startgame
    IDLE        = "idle"          # між словами — чекаємо ведучого
    EXPLAINING  = "explaining"    # ведучий пояснює поточне слово
    FINISHED    = "finished"


@dataclass
class Player:
    user_id: int
    name: str
    guessed: int = 0        # вгадав слів
    explained: int = 0      # пояснив слів
    hearts: int = 0         # серця як ведучий
    turns_played: int = 0   # разів був ведучим

    @property
    def score(self) -> int:
        return self.guessed


@dataclass
class ActiveWord:
    """Поточне активне слово — один об'єкт на слово."""
    explainer_id: int
    word: str
    category: str
    started_at: float = field(default_factory=time.time)

    # ID картки в груповому чаті
    card_msg_id: Optional[int] = None

    # Реакції після вгадування
    reaction_msg_id: Optional[int] = None
    hearts_count: int = 0
    hearts_given_by: set = field(default_factory=set)

    # Ексклюзивне вікно 🖐 для того, хто вгадав (30 сек)
    exclusive_uid: Optional[int] = None
    exclusive_until: float = 0.0

    @property
    def elapsed(self) -> int:
        return int(time.time() - self.started_at)


@dataclass
class Game:
    chat_id: int
    creator_id: int
    state: GameState = GameState.WAITING

    players: dict = field(default_factory=dict)   # user_id -> Player
    active_word: Optional[ActiveWord] = None
    used_words: set = field(default_factory=set)
    category: Optional[str] = None

    # ── гравці ──────────────────────────────────────────────────────

    def ensure_player(self, user_id: int, name: str) -> "Player":
        """Повертає гравця, додає якщо немає."""
        if user_id not in self.players:
            self.players[user_id] = Player(user_id=user_id, name=name)
        else:
            self.players[user_id].name = name   # оновлюємо ім'я
        return self.players[user_id]

    # ── нове слово ──────────────────────────────────────────────────

    def start_word(self, explainer_id: int) -> Optional["ActiveWord"]:
        """Вибирає нове слово і створює ActiveWord. None якщо слова скінчились."""
        word, category = _pick_word(self.used_words, self.category)
        if not word:
            return None
        self.used_words.add(word)
        self.active_word = ActiveWord(
            explainer_id=explainer_id,
            word=word,
            category=category,
        )
        self.state = GameState.EXPLAINING
        p = self.players.get(explainer_id)
        if p:
            p.turns_played += 1
        return self.active_word

    # ── таблиця ─────────────────────────────────────────────────────

    def get_scoreboard(self) -> str:
        if not self.players:
            return "Немає гравців."
        ranked = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = ["📊 *Рахунок:*\n"]
        for i, p in enumerate(ranked):
            medal = medals[i] if i < 3 else f"  {i + 1}."
            hearts = f"  💚 {p.hearts}" if p.hearts else ""
            lines.append(f"{medal} *{p.name}* — {p.score} відп.{hearts}")
        return "\n".join(lines)


# ════════════════════════════ нормалізація ════════════════════════════

def normalize(text: str) -> str:
    cleaned = re.sub(r"[^\w'\-]", " ", text.lower())
    return " ".join(cleaned.split())


def is_correct_guess(message_text: str, word: str) -> bool:
    msg_norm  = normalize(message_text)
    word_norm = normalize(word)
    return msg_norm == word_norm or f" {word_norm} " in f" {msg_norm} "


def _pick_word(used: set, category: Optional[str]) -> tuple:
    from words import WORDS, ALL_WORDS
    if category and category in WORDS:
        pool = [(w, category) for w in WORDS[category] if w not in used]
    else:
        pool = [(w, c) for w, c in ALL_WORDS if w not in used]
    if not pool:
        return None, None
    return random.choice(pool)


# ════════════════════════════ Сховище ════════════════════════════════

games: dict = {}


def get_game(chat_id: int) -> Optional[Game]:
    return games.get(chat_id)


def create_game(chat_id: int, creator_id: int) -> Game:
    g = Game(chat_id=chat_id, creator_id=creator_id)
    games[chat_id] = g
    return g


def delete_game(chat_id: int):
    games.pop(chat_id, None)

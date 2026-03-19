#!/usr/bin/env python3
"""
🗣 Еліас-бот — авто-детекція відповіді по повідомленнях у чаті
"""

import asyncio
import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

from game import (
    Game, GameState, Turn, Player,
    get_game, create_game, delete_game,
    is_correct_guess,
    record_game_results, get_rating_text,
)
from words import get_random_word, WORDS
from db import init_db

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════ helpers ════════════════════════════

def uname(user) -> str:
    name = (user.first_name or "")
    if user.last_name:
        name += " " + user.last_name
    return name.strip() or user.username or str(user.id)


def _cancel_jobs(context: ContextTypes.DEFAULT_TYPE, name: str):
    for job in context.application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


async def _safe_edit(context: ContextTypes.DEFAULT_TYPE,
                     chat_id: int, msg_id: int,
                     text: str, kb=None):
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
    except BadRequest:
        pass


# ═══════════════════════════ клавіатури ═════════════════════════════

def _turn_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Кнопки пояснювача під час ходу — ✅ немає, бот сам детектує відповідь."""
    c = chat_id
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👁 Моє слово",     callback_data=f"T_view_{c}"),
            InlineKeyboardButton("◀️ Попереднє",     callback_data=f"T_prev_{c}"),
        ],
        [
            InlineKeyboardButton("⏭ Нове слово",     callback_data=f"T_skip_{c}"),
            InlineKeyboardButton("⏹ Завершити хід",  callback_data=f"T_end_{c}"),
        ],
    ])


def _next_turn_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Почати хід!", callback_data=f"NEXT_{chat_id}")
    ]])


# ═══════════════════════════ тексти ═════════════════════════════════

def _card_text(game: Game, turn: Turn, note: str = "") -> str:
    """Публічна картка ходу в груповому чаті."""
    explainer = game.players.get(turn.explainer_id)
    lines = []
    if note:
        lines.append(note + "\n")
    lines += [
        f"🎙 Пояснює: *{explainer.name if explainer else '?'}*",
        f"⏱ Залишилось: *{turn.time_left} сек*",
        f"✅ Вгадано: *{turn.guessed_count}*  |  ⏭ Пропущено: *{turn.skipped_count}*",
        "",
        "💬 _Пишіть варіанти в чат — бот зарахує автоматично!_",
        "🔘 _Пояснювач: 👁 щоб побачити слово_",
    ]
    return "\n".join(lines)


# ═════════════════════════ /start  /help ════════════════════════════

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "👋 Привіт! Я бот для гри *Еліас* 🗣\n\n"
        "Додай мене до групового чату і там пиши /newgame!\n\n"
        "📋 *Команди:*\n"
        "/newgame — нова гра\n"
        "/startgame — почати (засновник)\n"
        "/score — рахунок\n"
        "/rating — рейтинг чату\n"
        "/endgame — завершити гру",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Правила Еліас*\n\n"
        "Один гравець *пояснює* слово своїми словами — не можна називати "
        "однокореневих. Всі інші пишуть варіанти прямо в чат.\n\n"
        "Бот *автоматично* розпізнає правильну відповідь і зараховує "
        "+1 очко тому хто вгадав.\n\n"
        "🏆 Перемагає перший хто набере ціль очок.\n\n"
        "🎮 *Кнопки пояснювача:*\n"
        "• *👁 Моє слово* — показує слово тільки тобі\n"
        "• *◀️ Попереднє* — повернутись до попереднього слова\n"
        "• *⏭ Нове слово* — пропустити поточне\n"
        "• *⏹ Завершити хід* — дострокове завершення\n\n"
        "💡 Щоб грати — просто пишіть варіанти в чат під час ходу. "
        "Бот автоматично додасть вас до гри як тільки ви щось вгадаєте!",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═════════════════════════ /newgame ══════════════════════════════════

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text(
            "⚠️ Додай мене до групового чату і запусти /newgame там!"
        )
        return

    existing = get_game(chat.id)
    if existing and existing.state != GameState.FINISHED:
        await update.message.reply_text("⚠️ Гра вже є! Спочатку заверши: /endgame")
        return

    game = create_game(chat.id, user.id)
    game.add_player(user.id, uname(user))

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎯 15 очок", callback_data="S_15"),
        InlineKeyboardButton("🎯 30 очок", callback_data="S_30"),
        InlineKeyboardButton("🎯 50 очок", callback_data="S_50"),
    ]])
    await update.message.reply_text(
        f"🎮 *{uname(user)} створює гру Еліас!*\n\n"
        "Скільки очок для перемоги?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_score_target(update: Update, _: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    game = get_game(q.message.chat_id)
    if not game:
        return
    if q.from_user.id != game.creator_id:
        await q.answer("Тільки засновник обирає!", show_alert=True)
        return

    game.target_score = int(q.data.split("_")[1])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏱ 30 сек", callback_data="D_30"),
        InlineKeyboardButton("⏱ 60 сек", callback_data="D_60"),
        InlineKeyboardButton("⏱ 90 сек", callback_data="D_90"),
    ]])
    await q.edit_message_text(
        f"✅ Ціль: *{game.target_score} очок*\n\nСкільки секунд на хід?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    game = get_game(chat_id)
    if not game:
        return
    if q.from_user.id != game.creator_id:
        await q.answer("Тільки засновник обирає!", show_alert=True)
        return

    game.turn_duration = int(q.data.split("_")[1])

    cats = list(WORDS.keys())
    context.bot_data[f"cats_{chat_id}"] = cats + [None]
    rows, row = [], []
    for i, c in enumerate(cats):
        row.append(InlineKeyboardButton(c, callback_data=f"C_{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔀 Всі категорії", callback_data=f"C_{len(cats)}")])

    await q.edit_message_text(
        f"✅ Ціль: *{game.target_score} очок*\n"
        f"✅ Час: *{game.turn_duration} сек*\n\n"
        "Обери категорію:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    game = get_game(chat_id)
    if not game:
        return
    if q.from_user.id != game.creator_id:
        await q.answer("Тільки засновник обирає!", show_alert=True)
        return

    idx = int(q.data.split("_", 1)[1])
    cats = context.bot_data.get(f"cats_{chat_id}", [])
    game.category = cats[idx] if idx < len(cats) else None
    cat_label = game.category or "🔀 Всі категорії"

    await q.edit_message_text(
        f"🗣 *Гра Еліас готується!*\n\n"
        f"🏆 Ціль: *{game.target_score} очок*\n"
        f"⏱ Час ходу: *{game.turn_duration} сек*\n"
        f"📚 Категорія: *{cat_label}*\n\n"
        f"Засновник починає: /startgame\n"
        "_Всі охочі зможуть приєднатись просто вгадуючи слова під час гри!_",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═════════════════════════ /startgame ═══════════════════════════════

async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = get_game(chat_id)

    if not game:
        await update.message.reply_text("⚠️ Немає гри.")
        return
    if user.id != game.creator_id:
        await update.message.reply_text("⚠️ Тільки засновник може почати!")
        return
    if game.state != GameState.WAITING:
        await update.message.reply_text("⚠️ Гра вже розпочата!")
        return
    if len(game.players) < 1:
        await update.message.reply_text("⚠️ Щось пішло не так — гравців немає.")
        return

    game.build_turn_order()
    game.state = GameState.PLAYING

    await update.message.reply_text(
        f"🚀 *Гра Еліас починається!*\n\n"
        f"🏆 Ціль: *{game.target_score} очок*  |  ⏱ *{game.turn_duration} сек/хід*\n"
        f"📚 Категорія: *{game.category or 'Всі'}*\n\n"
        "Пишіть варіанти в чат — бот зарахує сам!\n"
        "_Нові гравці приєднуються автоматично, як тільки вгадають перше слово_ 🎉",
        parse_mode=ParseMode.MARKDOWN,
    )
    await asyncio.sleep(1)
    await _announce_next_turn(context, chat_id)


# ═══════════════════════ логіка ходів ═══════════════════════════════

async def _announce_next_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Оголошує наступний хід — тільки пояснювач може натиснути ▶️."""
    game = get_game(chat_id)
    if not game or game.state == GameState.FINISHED:
        return

    explainer = game.get_current_explainer()
    if not explainer:
        return

    await context.bot.send_message(
        chat_id,
        f"🎯 Наступний пояснює: *{explainer.name}*\n\n"
        f"_{explainer.name}, натисни ▶️ коли будеш готовий!_",
        reply_markup=_next_turn_keyboard(chat_id),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_next_turn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пояснювач натиснув ▶️ Почати хід!"""
    q = update.callback_query
    chat_id = int(q.data.split("_")[1])
    game = get_game(chat_id)

    if not game or game.state != GameState.PLAYING:
        await q.answer()
        return

    explainer = game.get_current_explainer()
    if not explainer:
        await q.answer()
        return

    if q.from_user.id != explainer.user_id:
        await q.answer(
            f"Це хід {explainer.name}! Зачекай свого 😊",
            show_alert=True,
        )
        return

    await q.edit_message_reply_markup(None)
    await _start_turn(context, chat_id)


async def _start_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Починає активний хід: публікує картку в групу."""
    game = get_game(chat_id)
    if not game:
        return

    explainer = game.get_current_explainer()
    if not explainer:
        return

    word, category = get_random_word(game.used_words, game.category)
    if not word:
        await context.bot.send_message(
            chat_id,
            "😱 Слова скінчились! Гра завершена!\n\n" + game.get_scoreboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        game.state = GameState.FINISHED
        return

    game.used_words.add(word)
    turn = Turn(
        explainer_id=explainer.user_id,
        word=word,
        category=category,
        duration=game.turn_duration,
    )
    game.current_turn = turn
    game.state = GameState.TURN_ACTIVE
    explainer.turns_played += 1

    msg = await context.bot.send_message(
        chat_id,
        _card_text(game, turn),
        reply_markup=_turn_keyboard(chat_id),
        parse_mode=ParseMode.MARKDOWN,
    )
    turn.group_msg_id = msg.message_id

    _cancel_jobs(context, f"turn_{chat_id}")
    context.application.job_queue.run_once(
        _timeout_cb,
        game.turn_duration,
        data={"chat_id": chat_id, "explainer_id": explainer.user_id},
        name=f"turn_{chat_id}",
    )


async def _timeout_cb(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    game = get_game(d["chat_id"])
    if not game or game.state != GameState.TURN_ACTIVE:
        return
    t = game.current_turn
    if not t or t.explainer_id != d["explainer_id"]:
        return
    await _finish_turn(context, d["chat_id"], timed_out=True)


async def _finish_turn(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    timed_out: bool = False,
):
    game = get_game(chat_id)
    if not game:
        return
    turn = game.current_turn
    if not turn:
        return

    _cancel_jobs(context, f"turn_{chat_id}")

    explainer = game.players.get(turn.explainer_id)
    if explainer:
        explainer.explained += turn.guessed_count

    # Прибираємо кнопки з картки
    if turn.group_msg_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id, turn.group_msg_id, reply_markup=None
            )
        except BadRequest:
            pass

    summary = (
        f"{'⏰ Час вийшов!' if timed_out else '⏹ Хід завершено!'}\n\n"
        f"👤 Пояснював: *{explainer.name if explainer else '?'}*\n"
        f"✅ Вгадано: *{turn.guessed_count}*"
    )
    if turn.words_guessed:
        summary += "\n" + "\n".join(
            f"  • {w}  ← {game.players[uid].name}"
            for w, uid in turn.words_guessed
            if uid in game.players
        )
    if turn.skipped_count:
        summary += f"\n⏭ Пропущено: *{turn.skipped_count}*"

    await context.bot.send_message(chat_id, summary, parse_mode=ParseMode.MARKDOWN)

    game.advance_turn()
    game.state = GameState.PLAYING

    winner = game.get_winner()
    if winner:
        record_game_results(chat_id, game, winner)
        await context.bot.send_message(
            chat_id,
            f"🏆 *{winner.name} переміг!* 🎉\n\n" + game.get_scoreboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        game.state = GameState.FINISHED
        return

    await asyncio.sleep(2)
    await _announce_next_turn(context, chat_id)


# ═══════════════════ Авто-детекція відповіді ════════════════════════

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Слухаємо кожне текстове повідомлення у групі.
    Якщо гра активна і текст збігається зі словом — зараховуємо очко.
    """
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat_id
    user = msg.from_user
    game = get_game(chat_id)

    if not game or game.state != GameState.TURN_ACTIVE:
        return

    turn = game.current_turn
    if not turn:
        return

    # Пояснювач не може вгадувати своє слово
    if user.id == turn.explainer_id:
        return

    # Перевіряємо чи текст — правильна відповідь
    if not is_correct_guess(msg.text, turn.word):
        return

    # ── Правильна відповідь! ─────────────────────────────────────

    guesser_name = uname(user)

    # Автоматично додаємо гравця якщо його ще немає в грі
    if user.id not in game.players:
        game.add_player(user.id, guesser_name)

    game.players[user.id].guessed += 1

    guessed_word = turn.word
    turn.words_guessed.append((guessed_word, user.id))

    # Зберігаємо для кнопки "◀️ Попереднє"
    prev_w, prev_c = turn.word, turn.category
    prev_guesser = user.id

    # Беремо нове слово
    word, category = get_random_word(game.used_words, game.category)

    await context.bot.send_message(
        chat_id,
        f"🎉 *{guesser_name}* вгадав — *{guessed_word.upper()}*! +1 очко",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Перевіряємо переможця
    winner = game.get_winner()
    if winner:
        _cancel_jobs(context, f"turn_{chat_id}")
        explainer = game.players.get(turn.explainer_id)
        if explainer:
            explainer.explained += turn.guessed_count
        if turn.group_msg_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id, turn.group_msg_id, reply_markup=None
                )
            except BadRequest:
                pass
        record_game_results(chat_id, game, winner)
        await context.bot.send_message(
            chat_id,
            f"🏆 *{winner.name} переміг!* 🎉\n\n" + game.get_scoreboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        game.state = GameState.FINISHED
        return

    if not word:
        _cancel_jobs(context, f"turn_{chat_id}")
        await _finish_turn(context, chat_id)
        return

    game.used_words.add(word)
    turn.previous_word     = prev_w
    turn.previous_category = prev_c
    turn.previous_guesser_id = prev_guesser
    turn.word     = word
    turn.category = category

    # Оновлюємо картку
    if turn.group_msg_id:
        await _safe_edit(
            context, chat_id, turn.group_msg_id,
            _card_text(game, turn, "✅ *Вгадали! Наступне слово:*"),
            _turn_keyboard(chat_id),
        )


# ════════════════════ Кнопки пояснювача (T_*) ═══════════════════════

async def cb_turn_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, action, cid_str = q.data.split("_", 2)
    chat_id = int(cid_str)
    game = get_game(chat_id)

    if not game or game.state != GameState.TURN_ACTIVE:
        await q.answer("Хід не активний!", show_alert=True)
        return

    turn = game.current_turn
    if not turn:
        await q.answer()
        return

    # Тільки пояснювач може натискати кнопки
    if q.from_user.id != turn.explainer_id:
        explainer = game.players.get(turn.explainer_id)
        await q.answer(
            f"Ці кнопки для {explainer.name if explainer else 'пояснювача'}. "
            "Ти вгадуй — просто пиши в чат 😊",
            show_alert=True,
        )
        return

    # ── VIEW ──────────────────────────────────────────────────────
    if action == "view":
        await q.answer(
            f"🔤 {turn.word.upper()}\n📚 {turn.category}",
            show_alert=True,
        )
        return

    # ── PREV ──────────────────────────────────────────────────────
    if action == "prev":
        if not turn.previous_word:
            await q.answer("Попереднього слова немає!", show_alert=True)
            return

        # Знімаємо очко з того, хто вгадав попереднє слово
        if turn.previous_guesser_id and turn.previous_guesser_id in game.players:
            game.players[turn.previous_guesser_id].guessed = max(
                0, game.players[turn.previous_guesser_id].guessed - 1
            )
            # Прибираємо запис з words_guessed
            turn.words_guessed = [
                (w, uid) for w, uid in turn.words_guessed
                if w != turn.previous_word
            ]
            await context.bot.send_message(
                chat_id,
                f"◀️ Повернулись до *{turn.previous_word.upper()}* — "
                f"очко у *{game.players[turn.previous_guesser_id].name}* знято",
                parse_mode=ParseMode.MARKDOWN,
            )

        # Свопаємо
        curr_w, curr_c, curr_g = turn.word, turn.category, None
        turn.word     = turn.previous_word
        turn.category = turn.previous_category
        turn.previous_word     = curr_w
        turn.previous_category = curr_c
        turn.previous_guesser_id = None

        await q.answer(f"◀️ Повернулись до: {turn.word.upper()}")
        await _safe_edit(
            context, chat_id, turn.group_msg_id,
            _card_text(game, turn, "◀️ *Повернулись до попереднього слова*"),
            _turn_keyboard(chat_id),
        )
        return

    # ── SKIP ──────────────────────────────────────────────────────
    if action == "skip":
        await q.answer("⏭ Пропущено")
        turn.words_skipped.append(turn.word)
        prev_w, prev_c = turn.word, turn.category

        word, cat = get_random_word(game.used_words, game.category)
        if not word:
            _cancel_jobs(context, f"turn_{chat_id}")
            await _safe_edit(
                context, chat_id, turn.group_msg_id,
                "⏭ Слова скінчились, хід завершується...",
            )
            await _finish_turn(context, chat_id)
            return

        game.used_words.add(word)
        turn.previous_word     = prev_w
        turn.previous_category = prev_c
        turn.previous_guesser_id = None
        turn.word     = word
        turn.category = cat

        await _safe_edit(
            context, chat_id, turn.group_msg_id,
            _card_text(game, turn, "⏭ *Пропущено*"),
            _turn_keyboard(chat_id),
        )
        return

    # ── END ───────────────────────────────────────────────────────
    if action == "end":
        await q.answer("⏹ Хід завершено!")
        _cancel_jobs(context, f"turn_{chat_id}")
        await _safe_edit(
            context, chat_id, turn.group_msg_id,
            _card_text(game, turn, "⏹ *Пояснювач завершив хід*"),
        )
        await _finish_turn(context, chat_id, timed_out=False)


# ══════════════════ /score  /rating  /endgame ════════════════════════

async def cmd_score(update: Update, _: ContextTypes.DEFAULT_TYPE):
    game = get_game(update.effective_chat.id)
    if not game or game.state == GameState.WAITING:
        await update.message.reply_text("⚠️ Немає активної гри.")
        return
    text = game.get_scoreboard()
    if game.current_turn:
        exp = game.players.get(game.current_turn.explainer_id)
        if exp:
            text += f"\n\n⏱ Зараз пояснює: *{exp.name}*"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_rating(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("📊 Рейтинг є тільки в групових чатах!")
        return
    await update.message.reply_text(
        get_rating_text(update.effective_chat.id),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = get_game(chat_id)

    if not game:
        await update.message.reply_text("⚠️ Немає активної гри.")
        return
    if user.id != game.creator_id:
        await update.message.reply_text("⚠️ Тільки засновник може завершити гру!")
        return

    _cancel_jobs(context, f"turn_{chat_id}")
    winner = game.get_winner()
    record_game_results(chat_id, game, winner)
    await update.message.reply_text(
        "🛑 *Гру завершено!*\n\n" + game.get_scoreboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    delete_game(chat_id)


# ═══════════════════════════════ main ═══════════════════════════════

def main():
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise ValueError("Постав BOT_TOKEN у змінні середовища!")

    init_db()   # створює ratings.db і таблицю якщо їх ще немає
    logger.info("✅ База даних ініціалізована: ratings.db")

    app = Application.builder().token(token).build()

    # Команди
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("newgame",   cmd_newgame))
    app.add_handler(CommandHandler("startgame", cmd_startgame))
    app.add_handler(CommandHandler("score",     cmd_score))
    app.add_handler(CommandHandler("rating",    cmd_rating))
    app.add_handler(CommandHandler("endgame",   cmd_endgame))

    # Налаштування гри
    app.add_handler(CallbackQueryHandler(cb_score_target, pattern=r"^S_"))
    app.add_handler(CallbackQueryHandler(cb_duration,     pattern=r"^D_"))
    app.add_handler(CallbackQueryHandler(cb_category,     pattern=r"^C_"))

    # Ходи
    app.add_handler(CallbackQueryHandler(cb_next_turn,   pattern=r"^NEXT_"))
    app.add_handler(CallbackQueryHandler(cb_turn_action, pattern=r"^T_"))

    # ⬇️ Авто-детекція відповіді — текстові повідомлення в групах
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        on_message,
    ))

    async def post_init(a):
        await a.bot.set_my_commands([
            BotCommand("newgame",   "Нова гра"),
            BotCommand("startgame", "Почати гру"),
            BotCommand("score",     "Рахунок"),
            BotCommand("rating",    "Рейтинг гравців"),
            BotCommand("endgame",   "Завершити гру"),
            BotCommand("help",      "Правила"),
        ])

    app.post_init = post_init

    logger.info("🗣 Еліас-бот запущено!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

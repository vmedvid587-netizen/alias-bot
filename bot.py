#!/usr/bin/env python3
"""
🗣 Еліас-бот — без черги, без таймера ходу
Слово активне 10 хвилин, потім скипується автоматично.
"""

import asyncio
import logging
import os
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

from game import (
    Game, GameState, ActiveWord,
    get_game, create_game, delete_game,
    is_correct_guess,
)
from db import (
    init_db, record_game_results,
    get_rating_text, get_user_stats, get_user_stats_all_chats,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WORD_TIMEOUT      = 10 * 60   # 10 хвилин без вгаданого слова — сесія завершується
NO_EXPLAINER_TIMEOUT = 5 * 60 # 5 хвилин без ведучого — сесія завершується
VOLUNTEER_EXCL_SEC = 30        # секунд ексклюзивного вікна для відгадувача


# ═══════════════════════════════ helpers ════════════════════════════

def uname(user) -> str:
    name = (user.first_name or "")
    if user.last_name:
        name += " " + user.last_name
    return name.strip() or user.username or str(user.id)


def _cancel_jobs(context: ContextTypes.DEFAULT_TYPE, name: str):
    for job in context.application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()




def _start_word_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, word: str):
    """Запускає таймер на 10 хвилин для поточного слова."""
    _cancel_jobs(context, f"word_{chat_id}")
    context.application.job_queue.run_once(
        _word_timeout_cb,
        WORD_TIMEOUT,
        data={"chat_id": chat_id, "word": word},
        name=f"word_{chat_id}",
    )


async def _safe_edit(context, chat_id, msg_id, text, kb=None):
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=text, reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
    except BadRequest:
        pass


async def _remove_kb(context, chat_id, msg_id):
    try:
        await context.bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
    except BadRequest:
        pass


# ═══════════════════════════ таймер-колбеки ═════════════════════════

async def _word_timeout_cb(context: ContextTypes.DEFAULT_TYPE):
    """10 хвилин минуло — слово скипується, чекаємо нового ведучого."""
    d = context.job.data
    chat_id, expired_word = d["chat_id"], d["word"]
    game = get_game(chat_id)
    if not game or game.state != GameState.EXPLAINING:
        return
    aw = game.active_word
    if not aw or aw.word != expired_word:
        return

    explainer = game.players.get(aw.explainer_id)
    exp_name = explainer.name if explainer else "?"

    if aw.card_msg_id:
        await _remove_kb(context, chat_id, aw.card_msg_id)
    if aw.reaction_msg_id:
        await _remove_kb(context, chat_id, aw.reaction_msg_id)

    await context.bot.send_message(
        chat_id,
        f"⏰ *10 хвилин без вгаданих слів — сесію завершено!*\n\n"
        f"Останнє слово *{aw.word.upper()}* так і не вгадали 🤷\n"
        f"Пояснював: *{exp_name}*\n\n"
        + game.get_scoreboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    game.state = GameState.FINISHED
    delete_game(chat_id)




async def _no_explainer_cb(context: ContextTypes.DEFAULT_TYPE):
    """5 хвилин ніхто не взяв слово — завершуємо сесію."""
    chat_id = context.job.data["chat_id"]
    game = get_game(chat_id)
    if not game or game.state != GameState.IDLE:
        return

    await context.bot.send_message(
        chat_id,
        "😴 *Ніхто не взяв слово протягом 5 хвилин — сесію завершено!*\n\n"
        + game.get_scoreboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    game.state = GameState.FINISHED
    delete_game(chat_id)


# ══════════════════════════ клавіатури ══════════════════════════════

def _explainer_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Кнопки ведучого: переглянути слово, нове слово."""
    c = chat_id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Моє слово",     callback_data=f"E_view_{c}")],
        [InlineKeyboardButton("⏭ Нове слово",     callback_data=f"E_skip_{c}")],
    ])


def _reaction_keyboard(chat_id: int, hearts: int, exp_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"💚 {hearts}  — дякую {exp_name}!",
            callback_data=f"R_heart_{chat_id}",
        )],
        [InlineKeyboardButton(
            "🖐 Хочу пояснювати!",
            callback_data=f"R_volunteer_{chat_id}",
        )],
    ])


def _volunteer_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🖐 Хочу пояснювати!", callback_data=f"V_take_{chat_id}")
    ]])


# ══════════════════════════ тексти ══════════════════════════════════

def _card_text(game: Game, aw: ActiveWord, note: str = "") -> str:
    explainer = game.players.get(aw.explainer_id)
    lines = []
    if note:
        lines.append(note + "\n")
    lines += [
        f"🎙 Пояснює: *{explainer.name if explainer else '?'}*",
        "",
        "💬 _Пишіть варіанти в чат — бот зарахує автоматично!_",
        "_Ведучий: натисни 👁 щоб переглянути слово_",
    ]
    return "\n".join(lines)


# ═════════════════════════ /start /help ═════════════════════════════

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "👋 Привіт! Я бот для гри *Еліас* 🗣\n\n"
        "Додай мене до групового чату і пиши /newgame!\n\n"
        "📋 *Команди:*\n"
        "/newgame — нова гра\n"
        "/score — рахунок\n"
        "/rating — топ-25 гравців\n"
        "/mystats — моя статистика\n"
        "/endgame — завершити гру",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Правила Еліас*\n\n"
        "Хтось бере слово → пояснює своїми словами → "
        "інші пишуть варіанти прямо в чат.\n\n"
        "Бот *автоматично* розпізнає правильну відповідь.\n\n"
        "🖐 Після вгадування — натисни *«Хочу пояснювати!»* щоб взяти наступне слово.\n"
        "💚 Постав серце ведучому якщо пояснення сподобалось.\n\n"
        "⏰ Якщо слово не вгадали за *10 хвилин* — воно скипується.\n"
        "😴 Сесія завершується після *20 хвилин* без вгаданих слів "
        "або *5 хвилин* без ведучого.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═════════════════════════ /newgame ═════════════════════════════════

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("⚠️ Запусти /newgame у груповому чаті!")
        return

    existing = get_game(chat.id)
    if existing and existing.state != GameState.FINISHED:
        await update.message.reply_text("⚠️ Гра вже є! Спочатку заверши: /endgame")
        return

    game = create_game(chat.id, user.id)
    game.ensure_player(user.id, uname(user))
    game.state = GameState.IDLE

    await update.message.reply_text(
        f"🚀 *Гра Еліас починається!*\n\n"
        f"Перше слово пояснює *{uname(user)}*!",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _give_word(context, chat.id, user.id)






# ══════════════════════════ ведучий ═════════════════════════════════

async def _ask_for_volunteer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Публікує або оновлює кнопку 'Хочу пояснювати'."""
    game = get_game(chat_id)
    if not game or game.state == GameState.FINISHED:
        return

    text = (
        "🎯 *Хто буде пояснювати наступне слово?*\n"
        "_Натисни кнопку нижче!_"
    )
    vol_key = f"volunteer_msg_{chat_id}"
    existing_id = context.bot_data.get(vol_key)

    if existing_id:
        # Оновлюємо існуюче повідомлення замість нового
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_id,
                text=text,
                reply_markup=_volunteer_keyboard(chat_id),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            existing_id = None  # не вдалось — публікуємо нове

    if not existing_id:
        msg = await context.bot.send_message(
            chat_id,
            text,
            reply_markup=_volunteer_keyboard(chat_id),
            parse_mode=ParseMode.MARKDOWN,
        )
        context.bot_data[vol_key] = msg.message_id

    # Таймер: якщо ніхто не взяв — завершити
    _cancel_jobs(context, f"noexplainer_{chat_id}")
    context.application.job_queue.run_once(
        _no_explainer_cb,
        NO_EXPLAINER_TIMEOUT,
        data={"chat_id": chat_id},
        name=f"noexplainer_{chat_id}",
    )


async def cb_volunteer_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Хтось натиснув 'Хочу пояснювати!'"""
    q = update.callback_query
    chat_id = int(q.data.split("_")[2])
    game = get_game(chat_id)

    if not game or game.state != GameState.IDLE:
        await q.answer("Зараз це недоступно!", show_alert=True)
        return

    user = q.from_user
    game.ensure_player(user.id, uname(user))

    # Блокуємо стан одразу щоб інші натискання не пройшли
    game.state = GameState.EXPLAINING

    # Скасовуємо таймер очікування ведучого
    _cancel_jobs(context, f"noexplainer_{chat_id}")
    context.bot_data.pop(f"volunteer_msg_{chat_id}", None)

    await q.edit_message_reply_markup(None)
    await _give_word(context, chat_id, user.id)


async def _give_word(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    explainer_id: int,
):
    """Видає нове слово ведучому і публікує картку в чат."""
    game = get_game(chat_id)
    if not game:
        return

    aw = game.start_word(explainer_id)
    if not aw:
        await context.bot.send_message(
            chat_id,
            "😱 Слова скінчились! Гра завершена!\n\n" + game.get_scoreboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        game.state = GameState.FINISHED
        delete_game(chat_id)
        return

    explainer = game.players.get(explainer_id)

    # Картка в груповому чаті (слово НЕ показуємо)
    card_msg = await context.bot.send_message(
        chat_id,
        _card_text(game, aw),
        reply_markup=_explainer_keyboard(chat_id),
        parse_mode=ParseMode.MARKDOWN,
    )
    aw.card_msg_id = card_msg.message_id

    # Слово ведучий бачить через кнопку 👁 у груповому чаті

    # Таймер 10 хвилин на слово
    _start_word_timer(context, chat_id, aw.word)


# ═══════════════════════ Авто-детекція відповіді ════════════════════

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat_id
    user = msg.from_user
    game = get_game(chat_id)

    if not game or game.state != GameState.EXPLAINING:
        return

    aw = game.active_word
    if not aw:
        return

    # Ведучий не вгадує своє слово
    if user.id == aw.explainer_id:
        return

    if not is_correct_guess(msg.text, aw.word):
        return

    # ── Правильна відповідь! ─────────────────────────────────────────
    guesser_name = uname(user)
    player = game.ensure_player(user.id, guesser_name)
    player.guessed += 1

    guessed_word = aw.word
    explainer = game.players.get(aw.explainer_id)
    exp_name = explainer.name if explainer else "ведучий"
    if explainer:
        explainer.explained += 1

    # Скасовуємо таймер слова
    _cancel_jobs(context, f"word_{chat_id}")

    # Прибираємо кнопки з картки
    if aw.card_msg_id:
        await _remove_kb(context, chat_id, aw.card_msg_id)

    # Ексклюзивне вікно 🖐 для відгадувача
    aw.exclusive_uid = user.id
    aw.exclusive_until = time.time() + VOLUNTEER_EXCL_SEC

    # Зберігаємо дані реакції — потрібні для кнопки 💚
    explainer_id_for_hearts = aw.explainer_id
    context.bot_data[f"last_reaction_{chat_id}"] = {
        "explainer_id":  explainer_id_for_hearts,
        "exp_name":      exp_name,
        "hearts_count":  0,
        "exclusive_uid": user.id,
        "exclusive_until": time.time() + VOLUNTEER_EXCL_SEC,
    }

    # Зберігаємо очко відразу в БД (рейтинг оновлюється після кожного слова)
    from db import save_game_results as _save
    _save(chat_id, [{
        "user_id":   user.id,
        "name":      guesser_name,
        "turns":     0,
        "explained": 0,
        "guessed":   1,
        "hearts":    0,
    }])
    if explainer:
        _save(chat_id, [{
            "user_id":   explainer.user_id,
            "name":      explainer.name,
            "turns":     0,
            "explained": 1,
            "guessed":   0,
            "hearts":    0,
        }])

    # Реакція
    reaction_msg = await context.bot.send_message(
        chat_id,
        f"🎉 *{guesser_name}* відгадав(-ла) слово *{guessed_word.upper()}*\n\n"
        f"Постав 💚 ведучому *{exp_name}*, якщо пояснення сподобалось",
        reply_markup=_reaction_keyboard(chat_id, 0, exp_name),
        parse_mode=ParseMode.MARKDOWN,
    )
    aw.reaction_msg_id = reaction_msg.message_id

    # Завершуємо активне слово
    game.active_word = None
    game.state = GameState.IDLE

    # Питаємо хто пояснюватиме далі
    await asyncio.sleep(1)
    await _ask_for_volunteer(context, chat_id)


# ═══════════════════════ Кнопки ведучого (E_*) ══════════════════════

async def cb_explainer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, action, cid_str = q.data.split("_", 2)
    chat_id = int(cid_str)
    game = get_game(chat_id)

    if not game or game.state != GameState.EXPLAINING:
        await q.answer("Слово вже неактивне!", show_alert=True)
        return

    aw = game.active_word
    if not aw:
        await q.answer()
        return

    if q.from_user.id != aw.explainer_id:
        explainer = game.players.get(aw.explainer_id)
        await q.answer(
            f"Ці кнопки для {explainer.name if explainer else 'ведучого'}. "
            "Ти вгадуй — просто пиши в чат 😊",
            show_alert=True,
        )
        return

    # VIEW
    if action == "view":
        await q.answer(f"🔤 {aw.word.upper()}", show_alert=True)
        return

    # SKIP
    if action == "skip":
        await q.answer("⏭ Беремо нове слово")
        _cancel_jobs(context, f"word_{chat_id}")

        if aw.card_msg_id:
            await _remove_kb(context, chat_id, aw.card_msg_id)

        exp_id = aw.explainer_id
        game.active_word = None
        game.state = GameState.IDLE

        # Тихо беремо наступне слово — чат не бачить що слово скіпнули
        await _give_word(context, chat_id, exp_id)


# ════════════════════════ Кнопки реакцій (R_*) ══════════════════════

async def cb_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split("_")
    action  = parts[1]
    chat_id = int(parts[2])
    game = get_game(chat_id)

    if not game:
        await q.answer()
        return

    # Реакційне повідомлення належить до попереднього слова —
    # шукаємо його в active_word (може бути None якщо вже нове слово)
    # Серця і volunteer можуть спрацювати навіть після зміни слова
    # тому зберігаємо стан реакції в context.bot_data

    key = f"last_reaction_{chat_id}"

    if action == "heart":
        rdata = context.bot_data.get(key, {})
        explainer_id = rdata.get("explainer_id")
        hearts_key   = rdata.get("hearts_key", f"hearts_{chat_id}_{q.message.message_id}")

        if not explainer_id:
            await q.answer("Хід вже завершено!", show_alert=True)
            return

        if q.from_user.id == explainer_id:
            await q.answer("Собі серце не ставлять 😄", show_alert=True)
            return

        given_set_key = f"hearts_given_{chat_id}_{q.message.message_id}"
        given_set = context.bot_data.setdefault(given_set_key, set())

        if q.from_user.id in given_set:
            await q.answer("Ти вже поставив серце! 💚", show_alert=True)
            return

        given_set.add(q.from_user.id)

        # Нараховуємо серце ведучому в пам'яті
        exp_player = game.players.get(explainer_id)
        if exp_player:
            exp_player.hearts += 1

        count = rdata.get("hearts_count", 0) + 1
        rdata["hearts_count"] = count
        context.bot_data[key] = rdata

        # Зберігаємо серце в БД одразу
        from db import save_game_results as _save
        exp_name_for_save = rdata.get("exp_name", "ведучий")
        _save(chat_id, [{
            "user_id":   explainer_id,
            "name":      exp_name_for_save,
            "turns":     0,
            "explained": 0,
            "guessed":   0,
            "hearts":    1,
        }])

        await q.answer("💚 Дякуємо!")
        try:
            await q.edit_message_reply_markup(
                reply_markup=_reaction_keyboard(chat_id, count, exp_name_for_save)
            )
        except BadRequest:
            pass
        return

    if action == "volunteer":
        user = q.from_user
        uid = user.id

        game.ensure_player(uid, uname(user))

        # Якщо зараз вже хтось пояснює
        if game.state == GameState.EXPLAINING:
            aw = game.active_word
            if aw and uid == aw.explainer_id:
                await q.answer("Ти зараз ведучий! 😄", show_alert=True)
                return

        # Перевірка ексклюзивного вікна
        rdata = context.bot_data.get(key, {})
        excl_uid   = rdata.get("exclusive_uid")
        excl_until = rdata.get("exclusive_until", 0)

        if excl_uid and time.time() < excl_until and uid != excl_uid:
            secs = int(excl_until - time.time())
            guesser = game.players.get(excl_uid)
            gname = guesser.name if guesser else "відгадувач"
            await q.answer(
                f"Зачекай {secs} сек — зараз черга {gname}, який щойно вгадав!",
                show_alert=True,
            )
            return

        if game.state != GameState.IDLE:
            await q.answer("Зараз хтось пояснює — зачекай!", show_alert=True)
            return

        # Блокуємо стан одразу щоб інші натискання не пройшли
        game.state = GameState.EXPLAINING

        # Скасовуємо таймер очікування ведучого
        _cancel_jobs(context, f"noexplainer_{chat_id}")
        context.bot_data.pop(f"volunteer_msg_{chat_id}", None)
        await q.edit_message_reply_markup(None)
        await _give_word(context, chat_id, uid)


# ════════════════════════════ /score ════════════════════════════════

async def cmd_score(update: Update, _: ContextTypes.DEFAULT_TYPE):
    game = get_game(update.effective_chat.id)
    if not game or game.state == GameState.WAITING:
        await update.message.reply_text("⚠️ Немає активної гри.")
        return
    text = game.get_scoreboard()
    if game.active_word:
        exp = game.players.get(game.active_word.explainer_id)
        if exp:
            text += f"\n\n🎙 Зараз пояснює: *{exp.name}*"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════ /rating ═══════════════════════════════

async def cmd_rating(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("📊 Рейтинг доступний тільки в групових чатах!")
        return
    await update.message.reply_text(
        get_rating_text(update.effective_chat.id),
        parse_mode=ParseMode.MARKDOWN,
    )


# ════════════════════════════ /mystats ══════════════════════════════

async def cmd_mystats(update: Update, _: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    chat_stats = (
        get_user_stats(chat_id, user.id)
        if update.effective_chat.type != "private"
        else None
    )
    all_stats = get_user_stats_all_chats(user.id)

    if not all_stats:
        await update.message.reply_text(
            f"📊 *Статистика {uname(user)}*\n\nЩе немає зіграних ігор!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"📊 *СТАТИСТИКА КОРИСТУВАЧА {uname(user)}*\n"]
    lines.append(f"Рейтинг: {all_stats['hearts_received']} 💚\n")

    if chat_stats and update.effective_chat.type != "private":
        lines.append("*В цьому чаті*")
        lines.append(f"Був ведучим: {chat_stats['turns_played']}")
        lines.append(f"Успішно пояснив: {chat_stats['total_explained']}")
        lines.append(f"Відгадав: {chat_stats['total_guessed']}")
        lines.append("")

    lines.append("*В усіх чатах*")
    lines.append(f"Був ведучим: {all_stats['turns_played']}")
    lines.append(f"Успішно пояснив: {all_stats['total_explained']}")
    lines.append(f"Відгадав: {all_stats['total_guessed']}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════ /endgame ══════════════════════════════

async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = get_game(chat_id)

    if not game:
        await update.message.reply_text("⚠️ Немає активної гри.")
        return

    for job_name in [f"word_{chat_id}", f"inactive_{chat_id}", f"noexplainer_{chat_id}"]:
        _cancel_jobs(context, job_name)

    aw = game.active_word
    if aw and aw.card_msg_id:
        await _remove_kb(context, chat_id, aw.card_msg_id)

    await update.message.reply_text(
        "🛑 *Гру завершено!*\n\n" + game.get_scoreboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    delete_game(chat_id)


# ════════════════════════════ main ══════════════════════════════════

def main():
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise ValueError("Постав BOT_TOKEN у змінні середовища!")

    init_db()
    logger.info("✅ База даних готова")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("newgame",   cmd_newgame))
    app.add_handler(CommandHandler("score",     cmd_score))
    app.add_handler(CommandHandler("rating",    cmd_rating))
    app.add_handler(CommandHandler("mystats",   cmd_mystats))
    app.add_handler(CommandHandler("endgame",   cmd_endgame))

    app.add_handler(CallbackQueryHandler(cb_volunteer_take, pattern=r"^V_take_"))
    app.add_handler(CallbackQueryHandler(cb_explainer,     pattern=r"^E_"))
    app.add_handler(CallbackQueryHandler(cb_reaction,      pattern=r"^R_"))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        on_message,
    ))

    async def post_init(a):
        await a.bot.set_my_commands([
            BotCommand("newgame",   "Нова гра"),
            BotCommand("score",     "Рахунок"),
            BotCommand("rating",    "Топ-25 гравців"),
            BotCommand("mystats",   "Моя статистика"),
            BotCommand("endgame",   "Завершити гру"),
            BotCommand("help",      "Правила"),
        ])

    app.post_init = post_init

    logger.info("🗣 Еліас-бот запущено!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

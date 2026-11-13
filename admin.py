"""
Адмін-команди: керування ботом з особистого чату.

  /myid   — показує твій Telegram ID (доступно всім)
  /chats  — список груп, у які додано бота
  /say    — надіслати повідомлення в групу від імені бота

Адміни задаються змінною середовища ADMIN_IDS (ID через кому),
наприклад: ADMIN_IDS=123456789,987654321
"""

import asyncio
import logging
import os
import re

from telegram import Update, BotCommand, BotCommandScopeChat
from telegram.constants import ChatMemberStatus
from telegram.error import Forbidden, BadRequest, TelegramError
from telegram.ext import (
    Application, CommandHandler, ChatMemberHandler, TypeHandler,
    ContextTypes,
)

from db import upsert_chat, delete_chat, list_chats

logger = logging.getLogger(__name__)

ADMIN_IDS: set[int] = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}

CHATS_KEY = "known_chats"   # bot_data: chat_id → назва групи

SAY_USAGE = (
    "✍️ *Як надіслати повідомлення:*\n\n"
    "`/say Текст` — якщо бот лише в одній групі\n"
    "`/say 2 Текст` — у групу №2 зі списку /chats\n\n"
    "Можна також *відповісти* командою `/say` або `/say 2` на будь-яке "
    "своє повідомлення (фото, стікер, голосове…) — бот скопіює його в групу."
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _chats(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault(CHATS_KEY, {})


async def _db(fn, *args):
    """Виклик БД у фоновому потоці; помилка БД не ламає бота."""
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception:
        logger.exception("Адмін: помилка БД")
        return None


async def _forget_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    _chats(context).pop(chat_id, None)
    await _db(delete_chat, chat_id)


# ═════════════════════ облік груп (працює для всіх) ════════════════════

async def track_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запам'ятовує кожну групу, з якої приходить будь-яке оновлення.
    Працює у власній групі обробників, тож не заважає грі."""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    title = chat.title or str(chat.id)
    known = _chats(context)
    if known.get(chat.id) != title:   # пишемо в БД лише нову групу чи нову назву
        known[chat.id] = title
        await _db(upsert_chat, chat.id, title)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Бота додали в групу або видалили з неї."""
    upd = update.my_chat_member
    chat = upd.chat
    if chat.type not in ("group", "supergroup"):
        return
    status = upd.new_chat_member.status
    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        await _forget_chat(context, chat.id)
        logger.info(f"➖ Бота видалено з групи {chat.title} ({chat.id})")
    else:
        title = chat.title or str(chat.id)
        _chats(context)[chat.id] = title
        await _db(upsert_chat, chat.id, title)
        logger.info(f"➕ Бота додано в групу {title} ({chat.id})")


# ═════════════════════════════ команди ═════════════════════════════════

async def cmd_myid(update: Update, _: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = f"🆔 Твій Telegram ID: `{user.id}`"
    if update.effective_chat.type != "private":
        text += f"\n💬 ID цього чату: `{update.effective_chat.id}`"
    if is_admin(user.id):
        text += "\n\n👑 Ти адмін бота: доступні /chats і /say"
    await update.message.reply_text(text, parse_mode="Markdown")


async def _check_admin(update: Update) -> bool:
    """Адмін-команди — тільки для адмінів і тільки в особистому чаті."""
    if update.effective_chat.type != "private":
        return False   # у групі мовчимо, щоб не світити адмінку
    if not ADMIN_IDS:
        await update.message.reply_text(
            "⚙️ Адміна ще не налаштовано.\n\n"
            f"1. Твій ID: {update.effective_user.id}\n"
            "2. Додай на Railway змінну ADMIN_IDS з цим числом\n"
            "3. Перезапусти бота"
        )
        return False
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Ця команда тільки для адміна бота.")
        return False
    return True


async def _chat_list(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[int, str]]:
    """Групи з БД + ті, що бачили після запуску (на випадок без БД)."""
    known = _chats(context)
    rows = await _db(list_chats) or []
    for cid, title in rows:
        known.setdefault(cid, title)
    order = [cid for cid, _ in rows] + [cid for cid in known if cid not in dict(rows)]
    return [(cid, known[cid]) for cid in order]


async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_admin(update):
        return
    chats = await _chat_list(context)
    if not chats:
        await update.message.reply_text(
            "📭 Поки не знаю жодної групи.\n\n"
            "Бот запам'ятовує групу, щойно там з'являється будь-яке "
            "повідомлення — напиши щось у групі й повтори /chats."
        )
        return
    lines = ["💬 Групи з ботом:\n"]
    for i, (cid, title) in enumerate(chats, 1):
        lines.append(f"{i}. {title}  ({cid})")
    lines.append("\nНадіслати: /say <номер> <текст>")
    await update.message.reply_text("\n".join(lines))


async def cmd_say(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_admin(update):
        return
    msg = update.message
    # Текст після команди, з усіма переносами рядків і пробілами як є
    body = re.sub(r"^/\S+\s?", "", msg.text or "", count=1)
    # Необов'язковий номер групи (або ID чату) на початку
    m = re.match(r"(-?\d+)(?:\s+|$)", body)

    chats = await _chat_list(context)
    target = None
    if m:
        n = int(m.group(1))
        if 1 <= n <= len(chats):
            target = chats[n - 1]
        elif n < 0:   # можна вказати і сам ID чату
            target = next((c for c in chats if c[0] == n), (n, str(n)))
        if target:
            body = body[m.end():]
    if target is None:
        if len(chats) == 1:
            target = chats[0]
        elif not chats:
            await msg.reply_text("📭 Бот ще не знає жодної групи — див. /chats")
            return
        else:
            await msg.reply_text(
                "🔢 Вкажи номер групи зі списку /chats, наприклад: /say 1 Привіт!")
            return

    chat_id, title = target
    text = body
    reply = msg.reply_to_message

    if not text.strip() and not reply:
        await msg.reply_text(SAY_USAGE, parse_mode="Markdown")
        return

    try:
        if reply and not text.strip():
            await context.bot.copy_message(chat_id, msg.chat_id, reply.message_id)
        else:
            await context.bot.send_message(chat_id, text)
    except Forbidden:
        await _forget_chat(context, chat_id)
        await msg.reply_text(f"🚫 Бота більше немає в «{title}» — прибрав її зі списку.")
        return
    except (BadRequest, TelegramError) as e:
        await msg.reply_text(f"⚠️ Не вдалося надіслати в «{title}»: {e}")
        return

    await msg.reply_text(f"✅ Надіслано в «{title}»")


# ═════════════════════════════ підключення ═════════════════════════════

def register(app: Application):
    # group=-1: облік груп бачить усі оновлення раніше за гру і не блокує її
    app.add_handler(TypeHandler(Update, track_chat), group=-1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("myid",  cmd_myid))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("say",   cmd_say))


async def post_init(app: Application, public_commands: list[BotCommand]):
    """Завантажує відомі групи з БД і показує адмінам їхні команди в меню."""
    rows = await _db(list_chats) or []
    app.bot_data.setdefault(CHATS_KEY, {}).update(dict(rows))
    if not ADMIN_IDS:
        logger.warning("⚠️  ADMIN_IDS не задано — адмін-команди вимкнені")
        return
    admin_cmds = public_commands + [
        BotCommand("chats", "👑 Групи з ботом"),
        BotCommand("say",   "👑 Написати в групу від імені бота"),
        BotCommand("myid",  "Мій Telegram ID"),
    ]
    for uid in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(uid))
        except TelegramError:
            # адмін ще не писав боту — меню з'явиться після перезапуску
            logger.info(f"Не вдалося задати меню адміну {uid} (ще не писав боту?)")
    logger.info(f"👑 Адмінів: {len(ADMIN_IDS)}, відомих груп: {len(rows)}")

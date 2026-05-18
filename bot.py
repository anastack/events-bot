import html
import logging
from datetime import datetime, time as dt_time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

import config
import database

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
NAME, DATE, LOCATION, LINK, DESCRIPTION, PHOTO, EVENT_TYPE, TOPIC, CONFIRM = range(9)
EDIT_FIELD, EDIT_TEXT, EDIT_PHOTO, EDIT_SELECT = 9, 10, 11, 12
EVENT_TYPE_CUSTOM, TOPIC_CUSTOM = 13, 14

FIELD_NAMES = {
    "name": "Название", "date": "Дата", "location": "Место",
    "link": "Ссылка", "description": "Описание", "photo": "Фото",
    "event_type": "Тип", "topic": "Тема",
}

EVENT_TYPES = ["Концерт", "Лекция", "Форум", "Вечеринка", "Выставка", "Спектакль", "Другое"]
TOPICS = ["Бизнес", "Образование", "Студенческие", "Культура", "Спорт", "Технологии", "Другое"]

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}
SKIP = ("нет", "-", "no", "пропустить", "skip", ".")


# ── helpers ───────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return not config.ADMIN_IDS or user_id in config.ADMIN_IDS


def e(text: str) -> str:
    return html.escape(str(text))


def link_html(link: str) -> str:
    if link.startswith(("http://", "https://")):
        return f'<a href="{e(link)}">Перейти →</a>'
    return e(link)


# ── formatters ────────────────────────────────────────────────────────────────

def fmt_event_post(name, date_display, location, link, description,
                   event_type=None, topic=None) -> str:
    lines = [f"🎉 <b>{e(name)}</b>"]
    if event_type or topic:
        meta = []
        if event_type:
            meta.append(f"🎪 {e(event_type)}")
        if topic:
            meta.append(f"🏷 {e(topic)}")
        lines += ["", "  •  ".join(meta)]
    if description:
        lines += ["", e(description)]
    lines += [
        "",
        f"📅 <b>Дата:</b> {e(date_display)}",
        f"📍 <b>Место:</b> {e(location)}",
    ]
    if link:
        lines.append(f"🎟 <b>Ссылка на билеты/регистрацию:</b> {link_html(link)}")
    return "\n".join(lines)


def fmt_event_detail(event_row: tuple, attendee_count: int, user_going: bool) -> str:
    _, name, date_display, location, link, description, photo_file_id = event_row[:7]
    event_type = event_row[7] if len(event_row) > 7 else None
    topic = event_row[8] if len(event_row) > 8 else None

    lines = [f"🎉 <b>{e(name)}</b>"]
    if event_type or topic:
        meta = []
        if event_type:
            meta.append(f"🎪 {e(event_type)}")
        if topic:
            meta.append(f"🏷 {e(topic)}")
        lines += ["", "  •  ".join(meta)]
    if description:
        lines += ["", f"<i>{e(description)}</i>"]
    lines += [
        "",
        f"📅 <b>Дата:</b> {e(date_display)}",
        f"📍 <b>Место:</b> {e(location)}",
    ]
    if link:
        lines.append(f"🎟 <b>Ссылка на билеты/регистрацию:</b> {link_html(link)}")
    if photo_file_id:
        lines.append("📸 <i>Фото есть в посте канала</i>")
    lines += ["", f"👥 <b>Идут:</b> {attendee_count} чел."]
    if user_going:
        lines.append("✅ <i>Ты записан на это мероприятие</i>")
    return "\n".join(lines)


def fmt_attendees(event_name: str, attendees: list) -> str:
    if not attendees:
        return f"👥 <b>«{e(event_name)}»</b>\n\nПока никто не записался. Будь первым! 🙌"
    lines = [f"👥 <b>Идут на «{e(event_name)}»:</b>", ""]
    for _, username, first_name in attendees:
        if username:
            lines.append(f'• <a href="https://t.me/{e(username)}">@{e(username)}</a> ({e(first_name)})')
        else:
            lines.append(f"• {e(first_name)}")
    lines += ["", "<i>Нажми на имя, чтобы написать человеку и договориться встретиться!</i>"]
    return "\n".join(lines)


def fmt_digest(events: list) -> str:
    if not events:
        return "📭 <b>Ближайших мероприятий пока нет.</b>"
    lines = ["📅 <b>ДАЙДЖЕСТ МЕРОПРИЯТИЙ</b>", "━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    for i, row in enumerate(events, 1):
        name, date_display, location, link = row[1], row[2], row[3], row[4]
        lines.append(f"<b>{i}. {e(name)}</b>")
        lines.append(f"    📅 {e(date_display)}")
        lines.append(f"    📍 {e(location)}")
        if link:
            lines.append(f"    🎟 {link_html(link)}")
        lines.append("")
    lines += ["━━━━━━━━━━━━━━━━━━━━━━━━", "<i>Следующий дайджест — через 2 дня</i>"]
    return "\n".join(lines)


def _preview_text(d: dict) -> str:
    lines = [
        "<b>Предпросмотр:</b>\n",
        fmt_event_post(
            d["name"], d["date_display"], d["location"],
            d.get("link"), d.get("description"),
            d.get("event_type"), d.get("topic"),
        ),
    ]
    if d.get("photo_file_id"):
        lines.append("\n📸 <i>Фото прикреплено</i>")
    return "\n".join(lines)


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📅 Мероприятия"), KeyboardButton("➕ Добавить")],
            [KeyboardButton("🔧 Управление"),  KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def kb_confirm(is_admin_user: bool = True) -> InlineKeyboardMarkup:
    pub_label = "✅ Опубликовать" if is_admin_user else "✅ Отправить на проверку"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(pub_label, callback_data="confirm:pub"),
        InlineKeyboardButton("❌ Отмена",     callback_data="confirm:cancel"),
    ]])


def kb_event_types() -> InlineKeyboardMarkup:
    rows, row = [], []
    for t in EVENT_TYPES:
        row.append(InlineKeyboardButton(t, callback_data=f"add:type:{t}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def kb_topics() -> InlineKeyboardMarkup:
    rows, row = [], []
    for t in TOPICS:
        row.append(InlineKeyboardButton(t, callback_data=f"add:topic:{t}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def kb_approval(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить",   callback_data=f"appr:ok:{event_id}"),
        InlineKeyboardButton("❌ Отклонить",  callback_data=f"appr:no:{event_id}"),
    ]])


def kb_event_list(events: list) -> InlineKeyboardMarkup:
    rows = []
    for row in events:
        event_id, name, date_display, *_ = row
        parts = date_display.replace(",", "").split()
        short_date = " ".join(parts[:2]) if len(parts) >= 2 else date_display
        label = f"🎉 {name} — {short_date}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"ev:show:{event_id}")])
    return InlineKeyboardMarkup(rows)


def kb_event_actions(event_id: int, user_going: bool, count: int) -> InlineKeyboardMarkup:
    go_btn = (
        InlineKeyboardButton("❌ Больше не иду", callback_data=f"ev:go:{event_id}")
        if user_going
        else InlineKeyboardButton("✅ Иду", callback_data=f"ev:go:{event_id}")
    )
    return InlineKeyboardMarkup([
        [go_btn, InlineKeyboardButton(f"👥 Кто идёт ({count})", callback_data=f"ev:att:{event_id}")],
        [
            InlineKeyboardButton("◀ К списку", callback_data="ev:list"),
            InlineKeyboardButton("✖ Закрыть",  callback_data="ev:close"),
        ],
    ])


def kb_back_to_event(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀ Назад к мероприятию", callback_data=f"ev:show:{event_id}")
    ]])


def kb_admin_list(events: list) -> InlineKeyboardMarkup:
    from datetime import date as _date
    today = _date.today().isoformat()
    rows = []
    for event_id, name, date_display, date_sort in events:
        icon = "🟢" if date_sort >= today else "⚫"
        label = f"{icon} {name} — {date_display}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"adm:ev:{event_id}")])
    return InlineKeyboardMarkup(rows)


def kb_admin_event(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{event_id}"),
            InlineKeyboardButton("🗑 Удалить",  callback_data=f"adm:del:{event_id}"),
        ],
        [InlineKeyboardButton("◀ К списку", callback_data="adm:list")],
    ])


def kb_edit_fields(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Название",  callback_data=f"edf:name:{event_id}"),
            InlineKeyboardButton("📅 Дата",      callback_data=f"edf:date:{event_id}"),
        ],
        [
            InlineKeyboardButton("📍 Место",     callback_data=f"edf:location:{event_id}"),
            InlineKeyboardButton("🎟 Ссылка",    callback_data=f"edf:link:{event_id}"),
        ],
        [
            InlineKeyboardButton("📋 Описание",  callback_data=f"edf:description:{event_id}"),
            InlineKeyboardButton("📸 Фото",      callback_data=f"edf:photo:{event_id}"),
        ],
        [
            InlineKeyboardButton("🎪 Тип",       callback_data=f"edf:event_type:{event_id}"),
            InlineKeyboardButton("🏷 Тема",      callback_data=f"edf:topic:{event_id}"),
        ],
        [InlineKeyboardButton("◀ К мероприятию", callback_data=f"adm:ev:{event_id}")],
    ])


def kb_delete_confirm(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"adm:delok:{event_id}"),
        InlineKeyboardButton("❌ Отмена",      callback_data=f"adm:ev:{event_id}"),
    ]])


def kb_edit_event_types(event_id: int) -> InlineKeyboardMarkup:
    rows, row = [], []
    for t in EVENT_TYPES:
        row.append(InlineKeyboardButton(t, callback_data=f"edf_sel:event_type:{t}:{event_id}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ Назад", callback_data=f"edit:{event_id}")])
    return InlineKeyboardMarkup(rows)


def kb_edit_topics(event_id: int) -> InlineKeyboardMarkup:
    rows, row = [], []
    for t in TOPICS:
        row.append(InlineKeyboardButton(t, callback_data=f"edf_sel:topic:{t}:{event_id}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ Назад", callback_data=f"edit:{event_id}")])
    return InlineKeyboardMarkup(rows)


# ── conversation: /add ────────────────────────────────────────────────────────

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📝 <b>Добавление мероприятия</b>\n\nШаг <b>1 / 8</b>: Название мероприятия?",
        parse_mode="HTML",
    )
    return NAME


async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        "📅 Шаг <b>2 / 8</b>: Дата <i>(и время — по желанию)</i>\n\n"
        "Примеры:\n"
        "<code>25.05.2025 19:00</code>  — с временем\n"
        "<code>25.05.2025</code>  — только дата",
        parse_mode="HTML",
    )
    return DATE


async def step_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    has_time = ":" in text
    try:
        if has_time:
            parsed = datetime.strptime(text, "%d.%m.%Y %H:%M")
            display = f"{parsed.day} {MONTHS_RU[parsed.month]} {parsed.year}, {parsed.strftime('%H:%M')}"
        else:
            parsed = datetime.strptime(text, "%d.%m.%Y")
            display = f"{parsed.day} {MONTHS_RU[parsed.month]} {parsed.year}"
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат.\n"
            "С временем: <code>25.05.2025 19:00</code>\n"
            "Без времени: <code>25.05.2025</code>",
            parse_mode="HTML",
        )
        return DATE
    context.user_data["date_display"] = display
    context.user_data["date_sort"] = parsed.strftime("%Y-%m-%d")
    await update.message.reply_text(
        "📍 Шаг <b>3 / 8</b>: Место проведения? <i>(адрес или название)</i>",
        parse_mode="HTML",
    )
    return LOCATION


async def step_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["location"] = update.message.text.strip()
    await update.message.reply_text(
        "🎟 Шаг <b>4 / 8</b>: Ссылка на билеты / регистрацию\n"
        "<i>Если нет — напиши</i> <b>нет</b>",
        parse_mode="HTML",
    )
    return LINK


async def step_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    context.user_data["link"] = None if raw.lower() in SKIP else raw
    await update.message.reply_text(
        "📝 Шаг <b>5 / 8</b>: Описание мероприятия\n"
        "<i>Если нет — напиши</i> <b>нет</b>",
        parse_mode="HTML",
    )
    return DESCRIPTION


async def step_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    context.user_data["description"] = None if raw.lower() in SKIP else raw
    await update.message.reply_text(
        "📸 Шаг <b>6 / 8</b>: Фото мероприятия\n"
        "<i>Отправь фото или напиши</i> <b>нет</b>",
        parse_mode="HTML",
    )
    return PHOTO


async def step_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.photo:
        context.user_data["photo_file_id"] = update.message.photo[-1].file_id
    else:
        context.user_data["photo_file_id"] = None

    await update.message.reply_text(
        "🎪 Шаг <b>7 / 8</b>: Тип мероприятия",
        parse_mode="HTML",
        reply_markup=kb_event_types(),
    )
    return EVENT_TYPE


async def step_event_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    selected = query.data.split(":", 2)[2]
    if selected == "Другое":
        await query.edit_message_text(
            "🎪 Шаг <b>7 / 8</b>: Введи свой тип мероприятия:",
            parse_mode="HTML",
        )
        return EVENT_TYPE_CUSTOM
    context.user_data["event_type"] = selected
    await query.edit_message_text(
        "🏷 Шаг <b>8 / 8</b>: Тема мероприятия",
        parse_mode="HTML",
        reply_markup=kb_topics(),
    )
    return TOPIC


async def step_event_type_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["event_type"] = update.message.text.strip()
    await update.message.reply_text(
        "🏷 Шаг <b>8 / 8</b>: Тема мероприятия",
        parse_mode="HTML",
        reply_markup=kb_topics(),
    )
    return TOPIC


async def step_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    selected = query.data.split(":", 2)[2]
    if selected == "Другое":
        await query.edit_message_text(
            "🏷 Шаг <b>8 / 8</b>: Введи свою тему мероприятия:",
            parse_mode="HTML",
        )
        return TOPIC_CUSTOM
    context.user_data["topic"] = selected
    return await _show_confirm(query, context)


async def step_topic_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["topic"] = update.message.text.strip()
    d = context.user_data
    direct = not config.ADMIN_IDS
    text = _preview_text(d) + "\n\n<i>Всё верно?</i>"
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=kb_confirm(direct),
        disable_web_page_preview=True,
    )
    return CONFIRM


async def _show_confirm(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data
    direct = not config.ADMIN_IDS
    text = _preview_text(d) + "\n\n<i>Всё верно?</i>"
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=kb_confirm(direct),
        disable_web_page_preview=True,
    )
    return CONFIRM


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "confirm:cancel":
        await query.edit_message_text("❌ Публикация отменена.")
        context.user_data.clear()
        return ConversationHandler.END

    d = context.user_data

    if not config.ADMIN_IDS:
        # No admins configured — publish immediately
        event_id = database.add_event(
            name=d["name"], date_display=d["date_display"], date_sort=d["date_sort"],
            location=d["location"], link=d.get("link"), description=d.get("description"),
            photo_file_id=d.get("photo_file_id"), user_id=query.from_user.id,
            event_type=d.get("event_type"), topic=d.get("topic"), status="approved",
        )
        try:
            await _publish_event_to_channel(context, d, event_id)
            await query.edit_message_text(
                "✅ <b>Мероприятие опубликовано в канале!</b> 🎉", parse_mode="HTML"
            )
        except Exception:
            logger.exception("Ошибка публикации в канал")
            await query.edit_message_text(
                "⚠️ Мероприятие сохранено, но опубликовать в канал не удалось.\n"
                "Проверь права бота в канале.", parse_mode="HTML"
            )
    else:
        # Always send for admin approval when ADMIN_IDS is configured
        event_id = database.add_event(
            name=d["name"], date_display=d["date_display"], date_sort=d["date_sort"],
            location=d["location"], link=d.get("link"), description=d.get("description"),
            photo_file_id=d.get("photo_file_id"), user_id=query.from_user.id,
            event_type=d.get("event_type"), topic=d.get("topic"), status="pending",
        )

        submitter = query.from_user
        submitter_info = (
            f'<a href="tg://user?id={submitter.id}">{e(submitter.first_name)}</a>'
            + (f" (@{e(submitter.username)})" if submitter.username else "")
        )
        header = (
            f"🔔 <b>Новое мероприятие на проверку</b>\n"
            f"👤 Отправитель: {submitter_info}\n\n"
        )
        post_text = fmt_event_post(
            d["name"], d["date_display"], d["location"],
            d.get("link"), d.get("description"),
            d.get("event_type"), d.get("topic"),
        )
        photo = d.get("photo_file_id")

        for admin_id in config.ADMIN_IDS:
            try:
                if photo:
                    caption = header + post_text
                    if len(caption) <= 1024:
                        await context.bot.send_photo(
                            chat_id=admin_id, photo=photo, caption=caption,
                            parse_mode="HTML", reply_markup=kb_approval(event_id),
                        )
                    else:
                        await context.bot.send_photo(chat_id=admin_id, photo=photo)
                        await context.bot.send_message(
                            chat_id=admin_id, text=header + post_text,
                            parse_mode="HTML", disable_web_page_preview=True,
                            reply_markup=kb_approval(event_id),
                        )
                else:
                    await context.bot.send_message(
                        chat_id=admin_id, text=header + post_text,
                        parse_mode="HTML", disable_web_page_preview=True,
                        reply_markup=kb_approval(event_id),
                    )
            except Exception:
                logger.exception("Не удалось отправить запрос на проверку админу %d", admin_id)

        await query.edit_message_text(
            "✅ <b>Заявка отправлена на проверку модератору!</b>\n\n"
            "Если всё хорошо, твоё мероприятие скоро появится в канале. 🎉",
            parse_mode="HTML",
        )

    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.", reply_markup=kb_main_menu())
    return ConversationHandler.END


# ── admin approval callback ───────────────────────────────────────────────────

async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    _, action, event_id_str = query.data.split(":")
    event_id = int(event_id_str)
    event = database.get_event_by_id(event_id)

    if event is None:
        await query.edit_message_text("⚠️ Мероприятие не найдено (уже удалено?).")
        return

    name = event[1]
    submitter_id = database.get_event_submitter_id(event_id)

    if action == "ok":
        database.update_event_field(event_id, "status", "approved")
        d = {
            "name": event[1], "date_display": event[2], "location": event[3],
            "link": event[4], "description": event[5], "photo_file_id": event[6],
            "event_type": event[7] if len(event) > 7 else None,
            "topic": event[8] if len(event) > 8 else None,
        }
        await _publish_event_to_channel(context, d, event_id)
        await query.edit_message_text(
            f"✅ <b>«{e(name)}»</b> одобрено и опубликовано в канале.", parse_mode="HTML"
        )
        if submitter_id:
            try:
                await context.bot.send_message(
                    chat_id=submitter_id,
                    text=f"✅ Твоё мероприятие <b>«{e(name)}»</b> одобрено и опубликовано в канале! 🎉",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    elif action == "no":
        database.update_event_field(event_id, "status", "rejected")
        await query.edit_message_text(
            f"❌ <b>«{e(name)}»</b> отклонено.", parse_mode="HTML"
        )
        if submitter_id:
            try:
                await context.bot.send_message(
                    chat_id=submitter_id,
                    text=f"❌ Твоё мероприятие <b>«{e(name)}»</b> было отклонено администратором.",
                    parse_mode="HTML",
                )
            except Exception:
                pass


async def _publish_event_to_channel(
    context: ContextTypes.DEFAULT_TYPE, d: dict, _event_id: int
) -> None:
    post = fmt_event_post(
        d["name"], d["date_display"], d["location"],
        d.get("link"), d.get("description"),
        d.get("event_type"), d.get("topic"),
    )
    photo = d.get("photo_file_id")
    if photo:
        if len(post) <= 1024:
            await context.bot.send_photo(
                chat_id=config.CHANNEL_ID, photo=photo, caption=post, parse_mode="HTML"
            )
        else:
            await context.bot.send_photo(chat_id=config.CHANNEL_ID, photo=photo)
            await context.bot.send_message(
                chat_id=config.CHANNEL_ID, text=post,
                parse_mode="HTML", disable_web_page_preview=True,
            )
    else:
        await context.bot.send_message(
            chat_id=config.CHANNEL_ID, text=post,
            parse_mode="HTML", disable_web_page_preview=True,
        )


# ── event browsing ────────────────────────────────────────────────────────────

async def cmd_events(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    events = database.get_upcoming_events()
    if not events:
        await update.message.reply_text("📭 <b>Ближайших мероприятий нет.</b>", parse_mode="HTML")
        return
    await update.message.reply_text(
        "📅 <b>Ближайшие мероприятия:</b>\n"
        "<i>Нажми на мероприятие для подробностей</i>",
        parse_mode="HTML",
        reply_markup=kb_event_list(events),
    )


async def event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data: str = query.data

    if data == "ev:list":
        events = database.get_upcoming_events()
        if not events:
            await query.edit_message_text("📭 <b>Ближайших мероприятий нет.</b>", parse_mode="HTML")
            return
        await query.edit_message_text(
            "📅 <b>Ближайшие мероприятия:</b>\n<i>Нажми на мероприятие для подробностей</i>",
            parse_mode="HTML",
            reply_markup=kb_event_list(events),
        )
        return

    if data == "ev:close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    parts = data.split(":")
    if len(parts) != 3:
        return
    action, event_id = parts[1], int(parts[2])

    event = database.get_event_by_id(event_id)
    if event is None:
        await query.edit_message_text("⚠️ Мероприятие не найдено.")
        return

    user_id = query.from_user.id

    if action == "show":
        count = database.get_attendee_count(event_id)
        going = database.is_attending(event_id, user_id)
        await query.edit_message_text(
            fmt_event_detail(event, count, going),
            parse_mode="HTML",
            reply_markup=kb_event_actions(event_id, going, count),
            disable_web_page_preview=True,
        )

    elif action == "go":
        if database.is_attending(event_id, user_id):
            database.remove_attendee(event_id, user_id)
        else:
            database.add_attendee(event_id, user_id, query.from_user.username, query.from_user.first_name)
        count = database.get_attendee_count(event_id)
        going = database.is_attending(event_id, user_id)
        await query.edit_message_text(
            fmt_event_detail(event, count, going),
            parse_mode="HTML",
            reply_markup=kb_event_actions(event_id, going, count),
            disable_web_page_preview=True,
        )

    elif action == "att":
        attendees = database.get_attendees(event_id)
        _, name, *_ = event
        await query.edit_message_text(
            fmt_attendees(name, attendees),
            parse_mode="HTML",
            reply_markup=kb_back_to_event(event_id),
            disable_web_page_preview=True,
        )


# ── other commands ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Привет!</b> Я публикую анонсы мероприятий в канал.\n\n"
        "Используй кнопки меню внизу или команды:\n"
        "/events — список ближайших мероприятий\n"
        "/add — добавить мероприятие\n"
        "/cancel — отменить текущее действие",
        parse_mode="HTML",
        reply_markup=kb_main_menu(),
    )


async def cmd_help(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ <b>Помощь</b>\n\n"
        "📅 <b>Мероприятия</b> — посмотреть ближайшие события, записаться на них\n"
        "➕ <b>Добавить</b> — предложить новое мероприятие\n"
        "🔧 <b>Управление</b> — редактирование и удаление (только для админов)\n\n"
        "При добавлении мероприятие сначала проходит проверку у администратора.",
        parse_mode="HTML",
        reply_markup=kb_main_menu(),
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if config.ADMIN_IDS and update.effective_user.id not in config.ADMIN_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await _send_digest(context)
    await update.message.reply_text("✅ Дайджест отправлен в канал.")


# ── admin command & callback ──────────────────────────────────────────────────

async def cmd_admin(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    events = database.get_all_events()
    if not events:
        await update.message.reply_text("📭 <b>Мероприятий пока нет.</b>", parse_mode="HTML")
        return
    await update.message.reply_text(
        "🔧 <b>Управление мероприятиями:</b>\n"
        "<i>🟢 — предстоящее  ⚫ — прошедшее</i>",
        parse_mode="HTML",
        reply_markup=kb_admin_list(events),
    )


async def admin_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    data = query.data

    if data == "adm:list":
        events = database.get_all_events()
        await query.edit_message_text(
            "🔧 <b>Управление мероприятиями:</b>\n"
            "<i>🟢 — предстоящее  ⚫ — прошедшее</i>",
            parse_mode="HTML",
            reply_markup=kb_admin_list(events) if events else None,
        )
        return

    parts = data.split(":")
    action, event_id = parts[1], int(parts[2])
    event = database.get_event_by_id(event_id)

    if action == "ev":
        if not event:
            await query.edit_message_text("⚠️ Мероприятие не найдено.")
            return
        _, name, date_display, location, link, description, _, event_type, topic = (
            event + (None,) * (9 - len(event))
        )
        lines = [f"🎉 <b>{e(name)}</b>"]
        if event_type:
            lines.append(f"🎪 {e(event_type)}")
        if topic:
            lines.append(f"🏷 {e(topic)}")
        lines += [f"📅 {e(date_display)}", f"📍 {e(location)}"]
        if link:
            lines.append(f"🎟 {e(link)}")
        if description:
            lines.append(f"📝 {e(description[:80])}{'…' if len(description) > 80 else ''}")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb_admin_event(event_id)
        )

    elif action == "del":
        if not event:
            await query.edit_message_text("⚠️ Мероприятие не найдено.")
            return
        await query.edit_message_text(
            f"⚠️ Удалить <b>«{e(event[1])}»</b>?\n\n<i>Все записи участников тоже удалятся.</i>",
            parse_mode="HTML",
            reply_markup=kb_delete_confirm(event_id),
        )

    elif action == "delok":
        name = event[1] if event else "?"
        database.delete_event(event_id)
        events = database.get_all_events()
        await query.edit_message_text(
            f"🗑 <b>«{e(name)}»</b> удалено.\n\n"
            "🔧 <b>Управление мероприятиями:</b>",
            parse_mode="HTML",
            reply_markup=kb_admin_list(events) if events else None,
        )


# ── edit conversation ─────────────────────────────────────────────────────────

EDIT_PROMPTS = {
    "name":        "Введи новое название:",
    "date":        "Введи новую дату <i>(время необязательно)</i>:\n<code>25.05.2025 19:00</code>  или  <code>25.05.2025</code>",
    "location":    "Введи новое место:",
    "link":        "Введи новую ссылку (или <b>нет</b>, чтобы убрать):",
    "description": "Введи новое описание (или <b>нет</b>, чтобы убрать):",
    "photo":       "Отправь новое фото (или <b>нет</b>, чтобы убрать):",
}


async def start_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END

    event_id = int(query.data.split(":")[1])
    event = database.get_event_by_id(event_id)
    if not event:
        await query.edit_message_text("⚠️ Мероприятие не найдено.")
        return ConversationHandler.END

    context.user_data["edit_event_id"] = event_id
    await query.edit_message_text(
        f"✏️ <b>Редактирование «{e(event[1])}»</b>\n\nЧто изменить?",
        parse_mode="HTML",
        reply_markup=kb_edit_fields(event_id),
    )
    return EDIT_FIELD


async def choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    _, field, event_id_str = query.data.split(":")
    event_id = int(event_id_str)
    context.user_data["edit_event_id"] = event_id
    context.user_data["edit_field"] = field

    if field == "event_type":
        await query.edit_message_text(
            "🎪 Выбери новый тип мероприятия:",
            reply_markup=kb_edit_event_types(event_id),
        )
        return EDIT_SELECT

    if field == "topic":
        await query.edit_message_text(
            "🏷 Выбери новую тему мероприятия:",
            reply_markup=kb_edit_topics(event_id),
        )
        return EDIT_SELECT

    await query.edit_message_text(
        f"✏️ {EDIT_PROMPTS[field]}\n\n<i>Отправь /cancel для отмены</i>",
        parse_mode="HTML",
    )
    return EDIT_PHOTO if field == "photo" else EDIT_TEXT


async def process_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 3)
    _, field, value, event_id_str = parts
    event_id = int(event_id_str)

    if value == "Другое":
        context.user_data["edit_event_id"] = event_id
        context.user_data["edit_field"] = field
        label = "тип мероприятия" if field == "event_type" else "тему"
        await query.edit_message_text(
            f"✏️ Введи свой {label}:\n\n<i>/cancel для отмены</i>",
            parse_mode="HTML",
        )
        return EDIT_TEXT

    database.update_event_field(event_id, field, value)
    event = database.get_event_by_id(event_id)
    name = event[1] if event else "?"
    await query.edit_message_text(
        f"✅ <b>{FIELD_NAMES[field]}</b> обновлено!\n\n"
        f"✏️ <b>Редактирование «{e(name)}»</b>\n\nЧто ещё изменить?",
        parse_mode="HTML",
        reply_markup=kb_edit_fields(event_id),
    )
    return EDIT_FIELD


async def process_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    event_id: int = context.user_data["edit_event_id"]
    field: str = context.user_data["edit_field"]
    text = update.message.text.strip()

    if field == "date":
        has_time = ":" in text
        try:
            if has_time:
                parsed = datetime.strptime(text, "%d.%m.%Y %H:%M")
                display = f"{parsed.day} {MONTHS_RU[parsed.month]} {parsed.year}, {parsed.strftime('%H:%M')}"
            else:
                parsed = datetime.strptime(text, "%d.%m.%Y")
                display = f"{parsed.day} {MONTHS_RU[parsed.month]} {parsed.year}"
            database.update_event_field(event_id, "date_display", display)
            database.update_event_field(event_id, "date_sort", parsed.strftime("%Y-%m-%d"))
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат.\n"
                "С временем: <code>25.05.2025 19:00</code>\n"
                "Без времени: <code>25.05.2025</code>\n"
                "<i>Попробуй ещё или /cancel для отмены</i>",
                parse_mode="HTML",
            )
            return EDIT_TEXT
    elif field in ("link", "description"):
        database.update_event_field(event_id, field, None if text.lower() in SKIP else text)
    else:
        database.update_event_field(event_id, field, text)

    return await _back_to_edit_menu(update, context, event_id, field)


async def process_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    event_id: int = context.user_data["edit_event_id"]
    if update.message.photo:
        value = update.message.photo[-1].file_id
    else:
        raw = update.message.text.strip()
        value = None if raw.lower() in SKIP else raw
    database.update_event_field(event_id, "photo_file_id", value)
    return await _back_to_edit_menu(update, context, event_id, "photo")


async def _back_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              event_id: int, field: str) -> int:
    event = database.get_event_by_id(event_id)
    name = event[1] if event else "?"
    await update.message.reply_text(
        f"✅ <b>{FIELD_NAMES.get(field, field)}</b> обновлено!\n\n"
        f"✏️ <b>Редактирование «{e(name)}»</b>\n\nЧто ещё изменить?",
        parse_mode="HTML",
        reply_markup=kb_edit_fields(event_id),
    )
    return EDIT_FIELD


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("edit_event_id", None)
    context.user_data.pop("edit_field", None)
    await update.message.reply_text("❌ Редактирование отменено.", reply_markup=kb_main_menu())
    return ConversationHandler.END


# ── scheduler ─────────────────────────────────────────────────────────────────

async def _send_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    events = database.get_upcoming_events()
    try:
        await context.bot.send_message(
            chat_id=config.CHANNEL_ID,
            text=fmt_digest(events),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Дайджест отправлен: %d мероприятий", len(events))
    except Exception:
        logger.exception("Ошибка при отправке дайджеста")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    database.init_db()

    defaults = Defaults(tzinfo=config.TIMEZONE)
    app = Application.builder().token(config.BOT_TOKEN).defaults(defaults).build()

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", cmd_add),
            MessageHandler(filters.Regex(r"^➕ Добавить$") & filters.TEXT, cmd_add),
        ],
        states={
            NAME:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_name)],
            DATE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_date)],
            LOCATION:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_location)],
            LINK:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_link)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_description)],
            PHOTO: [
                MessageHandler(filters.PHOTO, step_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_photo),
            ],
            EVENT_TYPE: [CallbackQueryHandler(step_event_type, pattern="^add:type:")],
            EVENT_TYPE_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_event_type_custom)],
            TOPIC:      [CallbackQueryHandler(step_topic,      pattern="^add:topic:")],
            TOPIC_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_topic_custom)],
            CONFIRM:    [CallbackQueryHandler(step_confirm,    pattern="^confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_edit, pattern=r"^edit:\d+$")],
        states={
            EDIT_FIELD:  [CallbackQueryHandler(choose_field,        pattern="^edf:")],
            EDIT_TEXT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_text)],
            EDIT_PHOTO: [
                MessageHandler(filters.PHOTO,                       process_edit_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND,     process_edit_photo),
            ],
            EDIT_SELECT: [CallbackQueryHandler(process_edit_select, pattern="^edf_sel:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_edit)],
        per_message=False,
    )

    # Menu button text handlers (work when user is not in a conversation)
    app.add_handler(MessageHandler(filters.Regex(r"^📅 Мероприятия$"), cmd_events))
    app.add_handler(MessageHandler(filters.Regex(r"^🔧 Управление$"),  cmd_admin))
    app.add_handler(MessageHandler(filters.Regex(r"^ℹ️ Помощь$"),     cmd_help))

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(edit_conv)
    app.add_handler(add_conv)
    app.add_handler(CallbackQueryHandler(approval_callback, pattern="^appr:"))
    app.add_handler(CallbackQueryHandler(admin_callback,    pattern="^adm:"))
    app.add_handler(CallbackQueryHandler(event_callback,    pattern="^ev:"))

    first_run = dt_time(
        hour=config.DIGEST_HOUR, minute=config.DIGEST_MINUTE, tzinfo=config.TIMEZONE
    )
    app.job_queue.run_repeating(_send_digest, interval=2 * 24 * 60 * 60, first=first_run)

    logger.info(
        "Бот запущен. Дайджест в %02d:%02d (%s) каждые 2 дня.",
        config.DIGEST_HOUR, config.DIGEST_MINUTE, config.TIMEZONE,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

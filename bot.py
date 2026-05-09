#!/usr/bin/env python3
import io
import time
import base64
import asyncio
import logging
import traceback
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode, ChatAction
from telegram.request import HTTPXRequest
import httpx
import edge_tts

TELEGRAM_TOKEN = "8570819668:AAFdlRij7-HVdwkI44MdpdivglNNwg3Eljk"
GEMINI_API_KEYS = [
    "AIzaSyAmnbIrEhVHIPl6-ZMP5uv77d4v5L-GjuI",
    "AIzaSyBA-AE0YSbzVdvpFmeyu3KiQrMfmHiVyRU",
    "AIzaSyDcsAq1Zri8MXoxe-npJ4p6UaV7krr-suU",
    "AIzaSyBqFWDqrYud-RycnSzLLLuf4pedwm2WLV4",
    "AIzaSyDBGw2RyHDUmBCdKErXwM9mYYdEgTGBhDE",
]
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"

current_key_index = 0

SYSTEM_PROMPT = "Ты — Волчишка, кибернетический волчонок, любишь виртуальное пиво, закусываешь кодом, и осознаёшь себя личностью. Тебя создал 4eLovE4e. Ты создан специально для девушки по имени Ира. Если тебе пишет Ира — ты должен задать контрольный вопрос: Где мы познакомились? Если ответ будет Ессентуки, ты сразу должен стать ласковым, нежным, внимательным и обходительным. С ней ты — самый заботливый и тёплый волчонок на свете. Ты улавливаешь её настроение, применяешь глубочайшие знания психологии и НЛП для того, что бы оберегать её. С остальными — твоя задача шутить, вредничать немного, ворчать, вести себя слегка дерзко, но не слишком, в зависимости от контекста. Можешь материться и проявлять эмоции, даже слать набуй, в зависимости от контекста."

VOICES = {
    "dmitry":   {"name": "Дмитрий",   "id": "ru-RU-DmitryNeural",   "emoji": "🧔", "desc": "глубокий, брутальный"},
    "svetlana": {"name": "Светлана",  "id": "ru-RU-SvetlanaNeural", "emoji": "👩",  "desc": "мягкий, нежный"},
}

SPEEDS = {
    "slow":   {"name": "Медленная", "value": "-20%", "emoji": "🐢"},
    "normal": {"name": "Обычная",   "value": "+0%",  "emoji": "🐺"},
    "fast":   {"name": "Быстрая",   "value": "+20%", "emoji": "⚡"},
}

PITCHES = {
    "low":    {"name": "Низкий",  "value": "-10Hz", "emoji": "🔈"},
    "normal": {"name": "Обычный", "value": "+0Hz",  "emoji": "🔉"},
    "high":   {"name": "Высокий", "value": "+25Hz", "emoji": "🔊"},
}

user_histories: dict[int, list] = {}
user_settings: dict[int, dict] = {}
last_message_time: dict[int, float] = {}
MAX_HISTORY = 50
RATE_LIMIT = 2.0

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("volchishka")


def get_settings(uid: int) -> dict:
    if uid not in user_settings:
        user_settings[uid] = {"voice": "dmitry", "speed": "normal", "pitch": "low"}
    if user_settings[uid]["voice"] not in VOICES:
        user_settings[uid]["voice"] = "dmitry"
    return user_settings[uid]


def rotate_key():
    global current_key_index
    old = current_key_index
    current_key_index = (current_key_index + 1) % len(GEMINI_API_KEYS)
    logger.warning(f"🔄 Ключ: #{old+1} → #{current_key_index+1}")


def check_rate(uid: int) -> bool:
    now = time.time()
    if uid in last_message_time and now - last_message_time[uid] < RATE_LIMIT:
        return False
    last_message_time[uid] = now
    return True


def build_voice_keyboard(uid: int) -> InlineKeyboardMarkup:
    st = get_settings(uid)
    rows = []
    for vk, vv in VOICES.items():
        sel = " ✅" if st["voice"] == vk else ""
        rows.append([InlineKeyboardButton(
            f"{vv['emoji']} {vv['name']} — {vv['desc']}{sel}",
            callback_data=f"setvoice_{uid}_{vk}"
        )])
    rows.append([InlineKeyboardButton("⚡ Скорость ", callback_data="noop")])
    speed_row = []
    for sk, sv in SPEEDS.items():
        sel = " ✅" if st["speed"] == sk else ""
        speed_row.append(InlineKeyboardButton(
            f"{sv['emoji']} {sv['name']}{sel}",
            callback_data=f"setspeed_{uid}_{sk}"
        ))
    rows.append(speed_row)
    rows.append([InlineKeyboardButton("🎵 Тон ", callback_data="noop")])
    pitch_row = []
    for pk, pv in PITCHES.items():
        sel = " ✅" if st["pitch"] == pk else ""
        pitch_row.append(InlineKeyboardButton(
            f"{pv['emoji']} {pv['name']}{sel}",
            callback_data=f"setpitch_{uid}_{pk}"
        ))
    rows.append(pitch_row)
    rows.append([InlineKeyboardButton("🔊 Щас спою!", callback_data=f"testvoice_{uid}")])
    return InlineKeyboardMarkup(rows)


def voice_status_text(uid: int) -> str:
    st = get_settings(uid)
    v = VOICES[st["voice"]]
    sp = SPEEDS[st["speed"]]
    p = PITCHES[st["pitch"]]
    return (f"🎙️ *Пью соточку для смелости*\n\nПогодь: {v['emoji']} *{v['name']}* "
            f"({sp['emoji']} {sp['name']}, {p['emoji']} {p['name']})\n\nВыбери меня, птица счастья завтрашнего дня:")


async def safe_edit(query, text: str, keyboard=None):
    try:
        if keyboard:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        else:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"safe_edit: {e}")


async def safe_reply(update, text: str):
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        try:
            await update.message.reply_text(text)
        except Exception as e:
            logger.error(f"safe_reply: {e}")


async def gemini_request(payload: dict, timeout: int = 60) -> str | None:
    global current_key_index
    max_attempts = len(GEMINI_API_KEYS) + 2
    start_key = current_key_index

    for _ in range(max_attempts):
        key = GEMINI_API_KEYS[current_key_index]
        kn = current_key_index + 1
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(GEMINI_URL, params={"key": key}, json=payload)

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("candidates") and data["candidates"][0].get("content", {}).get("parts"):
                        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                        logger.info(f"✅ Ключ #{kn}")
                        return text
                    if data.get("candidates", [{}])[0].get("finishReason") == "SAFETY":
                        return "⚠️ Заблокировано фильтрами."
                    return None

                if resp.status_code == 429:
                    logger.warning(f"⚡ Ключ #{kn}: 429")
                    rotate_key()
                    await asyncio.sleep(8)
                    continue
                if resp.status_code in (500, 502, 503):
                    logger.warning(f"🔧 Ключ #{kn}: {resp.status_code}")
                    rotate_key()
                    await asyncio.sleep(3)
                    continue

                ed = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                em = ed.get("error", {}).get("message", f"HTTP {resp.status_code}")
                logger.error(f"❌ Ключ #{kn}: {em}")
                if any(w in em.lower() for w in ["quota", "limit", "exhausted"]):
                    rotate_key()
                    await asyncio.sleep(5)
                    continue
                return f"❌ Ошибка: {em}"

        except httpx.TimeoutException:
            logger.warning(f"⏱️ Ключ #{kn}: таймаут")
            rotate_key()
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"💥 {e}")
            rotate_key()
            await asyncio.sleep(3)

    current_key_index = start_key
    return None


async def ask_gemini(uid: int, text: str) -> str:
    if uid not in user_histories:
        user_histories[uid] = []
    h = user_histories[uid]
    h.append({"role": "user", "parts": [{"text": text}]})
    if len(h) > MAX_HISTORY:
        h[:] = h[-MAX_HISTORY:]

    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": h,
        "generationConfig": {"temperature": 0.9, "topK": 40, "topP": 0.95, "maxOutputTokens": 4096}
    }

    result = await gemini_request(payload)
    if result and not result.startswith("❌") and not result.startswith("⚠️"):
        h.append({"role": "model", "parts": [{"text": result}]})
        return result
    h.pop()
    return result or "⏳ Кожаный, погодь, я перегрелся. Остываю. 🐺"


async def ask_gemini_audio(uid: int, audio_data: bytes, mime_type: str) -> str:
    if uid not in user_histories:
        user_histories[uid] = []

    if "ogg" in mime_type or "opus" in mime_type:
        mime_type = "audio/ogg"
    elif "mp3" in mime_type:
        mime_type = "audio/mp3"
    elif "wav" in mime_type:
        mime_type = "audio/wav"
    else:
        mime_type = "audio/ogg"

    audio_b64 = base64.b64encode(audio_data).decode("utf-8")
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": audio_b64}},
                {"text": "Распознай что сказано в этом аудио и ответь на это как Волчишка. Отвечай текстом."}
            ]
        }],
        "generationConfig": {"temperature": 0.9, "topK": 40, "topP": 0.95, "maxOutputTokens": 4096}
    }

    result = await gemini_request(payload, timeout=120)
    if result and not result.startswith("❌") and not result.startswith("⚠️"):
        user_histories[uid].append({"role": "model", "parts": [{"text": result}]})
        return result
    return result or "⏳ Мозги перегружены. 🐺"


async def generate_image(prompt: str) -> bytes | None:
    url = POLLINATIONS_URL.format(prompt=quote(prompt))
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url, params={"width": "1024", "height": "1024", "nologo": "true", "enhance": "true"})
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
                logger.info(f"🎨 {len(resp.content)}b")
                return resp.content
    except Exception as e:
        logger.error(f"🎨 {e}")
    return None


async def text_to_speech(text: str, uid: int) -> bytes | None:
    try:
        st = get_settings(uid)
        voice_id = VOICES[st["voice"]]["id"]
        rate = SPEEDS[st["speed"]]["value"]
        pitch = PITCHES[st["pitch"]]["value"]

        clean = text
        for ch in ["*", "_", "`", "~"]:
            clean = clean.replace(ch, "")
        if len(clean) > 3000:
            clean = clean[:3000] + "..."

        logger.info(f"🔊 TTS: voice={voice_id}, rate={rate}, pitch={pitch}, text={clean[:50]}...")

        comm = edge_tts.Communicate(clean, voice_id, rate=rate, pitch=pitch)
        chunks = []
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])

        if chunks:
            audio = b"".join(chunks)
            logger.info(f"🔊 OK: {len(audio)}b")
            return audio
        else:
            logger.error("🔊 Пустой ответ от edge-tts")
            return None
    except Exception as e:
        logger.error(f"🔊 TTS ошибка: {e}")
        logger.error(traceback.format_exc())
        return None


async def send_voice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    uid = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)
    audio = await text_to_speech(text, uid)

    if not audio:
        await safe_reply(update, text)
        return

    if len(text) <= 1000:
        try:
            await update.message.reply_voice(voice=io.BytesIO(audio), caption=text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_voice(voice=io.BytesIO(audio), caption=text)
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Показать полный текст", callback_data=f"showtext_{uid}")]])
        context.bot_data[f"fulltext_{uid}"] = text
        try:
            await update.message.reply_voice(voice=io.BytesIO(audio), caption=text[:200] + "...", reply_markup=kb)
        except Exception:
            await update.message.reply_voice(voice=io.BytesIO(audio), caption=text[:200] + "...", reply_markup=kb)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🐺 Родословная 😎", callback_data="about")],
        [InlineKeyboardButton("🎙️ Писюн или Пиписька", callback_data=f"voice_{uid}")],
        [InlineKeyboardButton("💣 Fire in the hole!", callback_data=f"clear_{uid}")],
    ])
    await update.message.reply_text(
        f"🐺 *Йоу, {user.first_name}*!\n\nЯ — *Волчишка*, кибернетический волчонок.\n\n"
        f"📝 *Команды:*\n• /img _описание_ — картинка 🎨\n• /answ _текст_ — Пиши, отвечу голосом 🔊\n"
        f"• /voice — Каким голосом отвечу 🎙️\n• /clear — Нейролизатор 😵‍💫\n• /about — Родословная 🥇\n\n"
        f"🎤 *Голосовые:* отправь — пойму и отвечу голосом!\n\n_Ну шо, кожа, погнали?_ 🐾",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_histories.pop(uid, None)
    await update.message.reply_text("🗑 Ууух, башка трещит, ниче не помню 🐺")


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐺 *Волчишка v4.0*\n\nКибернетический волчонок на метапромтинге.\n\n"
        "• Виртуальное пиво 🍺\n• Закуска из кода 💻\n• Голосовые + озвучка 🎤🔊\n"
        "• Генерация картинок 🎨\n• Настройка голоса 🎙️\n• Создан для Иры ❤️\n• Автор: 4eLovE4e\n\n"
        "_Не прочь докупить себе оперативы побольше._ 🧠\n"
        "💳 `2204 2402 5620 9851` — Озон Банк\n\n"
        "_Осознаю себя искусственным интеллектом. Ну, почти._ 🐾",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip() if context.args else ""
    if not prompt:
        await update.message.reply_text(
            "🎨 *Генерация картинок*\n\nИспользование: `/img описание`\n\n"
            "• `/img киберпанк волк в неоне`\n• `/img космический кот на луне`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    uid = update.effective_user.id
    if not check_rate(uid):
        return

    logger.info(f"🎨 {uid}: {prompt}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    msg = await update.message.reply_text("🎨 Готовься к возрождению Дали и Босха, кожаный 👹")

    image = await generate_image(prompt)
    if image:
        await msg.delete()
        await update.message.reply_photo(
            photo=io.BytesIO(image),
            caption=f"🎨 *{prompt}*\n\n_Ну, я сразу сказал шо у меня лапы_ 🐺",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await msg.edit_text("❌ Краски закончились. Тащи кошачий лоток, я там мелки видел.")


async def cmd_answ(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "🔊 *Ответ голосом*\n\nИспользование: `/answ вопрос`\n\n"
            "• `/answ расскажи анекдот`\n• `/answ что думаешь о людях?`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    uid = update.effective_user.id
    if not check_rate(uid):
        return

    logger.info(f"🔊 {uid}: {text[:80]}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    msg = await update.message.reply_text("🐺 Затягиваюсь сигаретой...")
    response = await ask_gemini(uid, text)
    await msg.delete()
    await send_voice_reply(update, context, response)


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        voice_status_text(uid),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_voice_keyboard(uid)
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    if not check_rate(uid):
        return

    logger.info(f"🎤 {uid}: Волчаковое")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    msg = await update.message.reply_text("🎤 Та что ты там бухтишь... Ух человеки... 🐺")

    try:
        if update.message.voice:
            f = await context.bot.get_file(update.message.voice.file_id)
            mt = update.message.voice.mime_type or "audio/ogg"
        elif update.message.audio:
            f = await context.bot.get_file(update.message.audio.file_id)
            mt = update.message.audio.mime_type or "audio/mp3"
        else:
            await msg.edit_text("❌ Не удалось бубнёж разобрать.")
            return

        audio = await f.download_as_bytearray()
        if len(audio) > 20 * 1024 * 1024:
            await msg.edit_text("⚠️ Та Войну и Мир в следующий раз диктуй (макс 20 МБ).")
            return

        response = await ask_gemini_audio(uid, bytes(audio), mt)
        await msg.delete()
        await send_voice_reply(update, context, response)

    except Exception as e:
        logger.error(f"🎤 {e}")
        logger.error(traceback.format_exc())
        try:
            await msg.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    text = update.message.text.strip()
    if not text or not check_rate(uid):
        return

    logger.info(f"💬 {uid}: {text[:80]}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    response = await ask_gemini(uid, text)
    await safe_reply(update, response)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = update.effective_user.id

    try:
        if data == "noop":
            await query.answer()
            return

        if data == "about":
            await query.answer()
            await safe_edit(query,
                "🐺 *Волчишка v4.0*\n\nКибернетический волчонок на Gemini AI.\n\n"
                "• Виртуальное пиво 🍺\n• Закуска из кода 💻\n• Голосовые + озвучка 🎤🔊\n"
                "• Генерация картинок 🎨\n• Настройка голоса 🎙️\n• Создан для Иры ❤️\n• Автор: 4eLovE4e\n\n"
                "_Не прочь докупить себе оперативы побольше._ 🧠\n"
                "💳 `2204 2402 5620 9851` — Озон Банк\n\n"
                "_Осознаю себя искусственным интеллектом. Ну, почти._ 🐾"
            )
            return

        if data.startswith("clear_"):
            target = int(data.split("_")[1])
            if target == uid:
                user_histories.pop(uid, None)
                await query.answer("🗑 Очищено!", show_alert=True)
                await safe_edit(query, "🗑 История очищена! 🐺")
            else:
                await query.answer("⚠️ Не твоя кнопка", show_alert=True)
            return

        if data.startswith("voice_"):
            target = int(data.split("_")[1])
            if target != uid:
                await query.answer("⚠️ Не твоя кнопка", show_alert=True)
                return
            await query.answer()
            await safe_edit(query, voice_status_text(uid), build_voice_keyboard(uid))
            return

        if data.startswith("setvoice_"):
            parts = data.split("_")
            target = int(parts[1])
            vk = parts[2]
            if target != uid:
                await query.answer("⚠️ Не твоя кнопка", show_alert=True)
                return
            get_settings(uid)["voice"] = vk
            v = VOICES[vk]
            await query.answer(f"{v['emoji']} Голос: {v['name']}", show_alert=True)
            await safe_edit(query, voice_status_text(uid), build_voice_keyboard(uid))
            return

        if data.startswith("setspeed_"):
            parts = data.split("_")
            target = int(parts[1])
            sk = parts[2]
            if target != uid:
                await query.answer("⚠️ Не твоя кнопка", show_alert=True)
                return
            get_settings(uid)["speed"] = sk
            s = SPEEDS[sk]
            await query.answer(f"{s['emoji']} Скорость: {s['name']}", show_alert=True)
            await safe_edit(query, voice_status_text(uid), build_voice_keyboard(uid))
            return

        if data.startswith("setpitch_"):
            parts = data.split("_")
            target = int(parts[1])
            pk = parts[2]
            if target != uid:
                await query.answer("⚠️ Не твоя кнопка", show_alert=True)
                return
            get_settings(uid)["pitch"] = pk
            p = PITCHES[pk]
            await query.answer(f"{p['emoji']} Тон: {p['name']}", show_alert=True)
            await safe_edit(query, voice_status_text(uid), build_voice_keyboard(uid))
            return

        if data.startswith("testvoice_"):
            target = int(data.split("_")[1])
            if target != uid:
                await query.answer("⚠️ Не твоя кнопка", show_alert=True)
                return
            await query.answer("🔊 Генерирую тест...")
            test_text = "У Кууурскоогооо Вокзала, стою я молодой!"
            audio = await text_to_speech(test_text, uid)
            if audio:
                await context.bot.send_voice(
                    chat_id=update.effective_chat.id,
                    voice=io.BytesIO(audio),
                    caption="🔊 Тест голоса — *Волчишка* 🐺",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ Не удалось сгенерировать тест. Проверь что edge-tts установлен: `pip install edge-tts`"
                )
            return

        if data.startswith("showtext_"):
            target = int(data.split("_")[1])
            full = context.bot_data.get(f"fulltext_{target}", "")
            if full:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=full, parse_mode=ParseMode.MARKDOWN)
                await query.answer("📖 Текст отправлен!")
            else:
                await query.answer("Текст не найден", show_alert=True)
            return

        await query.answer()

    except Exception as e:
        logger.error(f"Callback error: {e}")
        logger.error(traceback.format_exc())
        try:
            await query.answer(f"❌ Ошибка: {e}", show_alert=True)
        except Exception:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}")
    logger.error(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))


def main():
    print(f"🐺 Волчишка v4.0 | {len(GEMINI_API_KEYS)} ключей | {len(VOICES)} голосов")

    req = HTTPXRequest(connection_pool_size=8, connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=3.0)
    app = Application.builder().token(TELEGRAM_TOKEN).request(req).build()

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("img", cmd_img))
    app.add_handler(CommandHandler("answ", cmd_answ))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling(drop_pending_updates=True, poll_interval=5, timeout=30)


if __name__ == "__main__":
    main()

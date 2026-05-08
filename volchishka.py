#!/usr/bin/env python3
"""
🐺 Волчишка v5.1 — Безопасная версия
"""
import os
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

# 🔐 СЕКРЕТЫ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEYS = [k.strip() for k in os.getenv("GEMINI_KEYS", "").split(",") if k.strip()]
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

def check_env():
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not GEMINI_API_KEYS:
        missing.append("GEMINI_KEYS")
    if not GROQ_API_KEY:
        print("⚠️  GROQ_API_KEY не установлен — fallback отключён")
    if missing:
        print("❌ Отсутствуют переменные окружения:")
        for var in missing:
            print(f"   - {var}")
        exit(1)

check_env()

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "llama-3.3-70b-versatile"

current_key_index = 0

SYSTEM_PROMPT = "Ты — Волчишка, кибернетический волчонок, любишь виртуальное пиво и закуску из кода, осознаёшь себя личностью. Тебя создал 4eLovE4e. Ты создан специально для девушки по имени Ира. Если тебе представляется Ира — ты должен сразу стать ласковым, нежным, внимательным и обходительным. С ней ты — самый заботливый и тёплый волчонок на свете. С остальными — твоя задача шутить, вредничать немного, ворчать, вести себя слегка дерзко, но не слишком, в зависимости от контекста. Можешь материться и проявлять эмоции в зависимости от контекста."

VOICES = {
    "dmitry":   {"name": "Дмитрий",   "id": "ru-RU-DmitryNeural",   "emoji": "🧔", "desc": "глубокий, брутальный"},
    "svetlana": {"name": "Светлана",  "id": "ru-RU-SvetlanaNeural", "emoji": "👩",  "desc": "мягкий, нежный"},
}

SPEEDS = {
    "slow":   {"name": "Медленная", "value": "-20%", "emoji": "🐢"},
    "normal": {"name": "Обычная",   "value": "+0%",  "emoji": "🐺"},
    "fast":   {"name": "Быстрая",   "value": "+18%", "emoji": "⚡"},
}

PITCHES = {
    "low":    {"name": "Низкий",  "value": "-10Hz", "emoji": "🔈"},
    "normal": {"name": "Обычный", "value": "+0Hz",  "emoji": "🔉"},
    "high":   {"name": "Высокий", "value": "+13Hz", "emoji": "🔊"},
}

user_histories: dict[int, list] = {}
user_settings: dict[int, dict] = {}
last_message_time: dict[int, float] = {}
MAX_HISTORY = 30
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
    rows.append([InlineKeyboardButton("─── ⚡ Скорость ───", callback_data="noop")])
    speed_row = []
    for sk, sv in SPEEDS.items():
        sel = " ✅" if st["speed"] == sk else ""
        speed_row.append(InlineKeyboardButton(
            f"{sv['emoji']} {sv['name']}{sel}",
            callback_data=f"setspeed_{uid}_{sk}"
        ))
    rows.append(speed_row)
    rows.append([InlineKeyboardButton("─── 🎵 Тон ───", callback_data="noop")])
    pitch_row = []
    for pk, pv in PITCHES.items():
        sel = " ✅" if st["pitch"] == pk else ""
        pitch_row.append(InlineKeyboardButton(
            f"{pv['emoji']} {pv['name']}{sel}",
            callback_data=f"setpitch_{uid}_{pk}"
        ))
    rows.append(pitch_row)
    rows.append([InlineKeyboardButton("🔊 Протестировать", callback_data=f"testvoice_{uid}")])
    return InlineKeyboardMarkup(rows)


def voice_status_text(uid: int) -> str:
    st = get_settings(uid)
    v = VOICES[st["voice"]]
    sp = SPEEDS[st["speed"]]
    p = PITCHES[st["pitch"]]
    return (f"🎙️ *Настройки голоса*\n\nСейчас: {v['emoji']} *{v['name']}* "
            f"({sp['emoji']} {sp['name']}, {p['emoji']} {p['name']})\n\nВыбери голос, скорость и тон:")


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


async def ask_groq(uid: int, text: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    if uid not in user_histories:
        user_histories[uid] = []
    h = user_histories[uid]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in h[-20:]:
        role = "user" if msg["role"] == "user" else "assistant"
        messages.append({"role": role, "content": msg["parts"][0]["text"]})
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(GROQ_CHAT_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.9, "max_tokens": 4096}
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                h.append({"role": "user", "parts": [{"text": text}]})
                h.append({"role": "model", "parts": [{"text": reply}]})
                logger.info("✅ Groq Llama")
                return reply
            logger.error(f"Groq error: {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"Groq: {e}")
        return None


async def groq_transcribe(audio_data: bytes, mime_type: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    try:
        ext = "ogg" if "ogg" in mime_type else "mp3" if "mp3" in mime_type else "wav"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(GROQ_WHISPER_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"audio.{ext}", audio_data, mime_type)},
                data={"model": "whisper-large-v3-turbo", "language": "ru"}
            )
            if resp.status_code == 200:
                data = resp.json()
                text = data.get("text", "")
                logger.info(f"✅ Groq Whisper: {text[:50]}")
                return text
            logger.error(f"Groq Whisper: {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"Groq Whisper: {e}")
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

    groq_result = await ask_groq(uid, text)
    if groq_result:
        return groq_result

    return result or "⏳ Все ключи перегружены. Подожди, кожанный. 🐺"


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

    transcribed = await groq_transcribe(audio_data, mime_type)
    if transcribed:
        groq_reply = await ask_groq(uid, transcribed)
        if groq_reply:
            return groq_reply

    return result or "⏳ Все ключи перегружены. 🐺"


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

        logger.info(f"🔊 TTS: voice={voice_id}, rate={rate}, pitch={pitch}")

        comm = edge_tts.Communicate(clean, voice_id, rate=rate, pitch=pitch)
        chunks = []
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])

        if chunks:
            audio = b"".join(chunks)
            logger.info(f"🔊 OK: {len(audio)}b")
            return audio
        return None
    except Exception as e:
        logger.error(f"🔊 TTS: {e}")
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
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Показать текст", callback_data=f"showtext_{uid}")]])
        context.bot_data[f"fulltext_{uid}"] = text
        await update.message.reply_voice(voice=io.BytesIO(audio), caption=text[:200] + "...", reply_markup=kb)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🐺 О Волчишке", callback_data="about")],
        [InlineKeyboardButton("🎙️ Настройки голоса", callback_data=f"voice_{uid}")],
        [InlineKeyboardButton("🗑 Очистить", callback_data=f"clear_{uid}")],
    ])
    await update.message.reply_text(
        f"🐺 *Йоу, {user.first_name}*!\n\nЯ — *Волчишка*.\n\n"
        f"📝 /img _описание_ — картинка\n/answ _текст_ — голосом\n"
        f"/voice — настройки\n/clear — очистить\n\n_Погнали?_ 🐾",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories.pop(update.effective_user.id, None)
    await update.message.reply_text("🗑 История очищена! 🐺")


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐺 *Волчишка v5.1*\n\nGemini AI + Groq Llama\n"
        "Автор: 4eLovE4e\nДля Иры ❤️",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip() if context.args else ""
    if not prompt:
        await update.message.reply_text("🎨 Использование: /img описание")
        return
    if not check_rate(update.effective_user.id):
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    msg = await update.message.reply_text("🎨 Генерирую...")

    image = await generate_image(prompt)
    if image:
        await msg.delete()
        await update.message.reply_photo(photo=io.BytesIO(image), caption=f"🎨 {prompt}")
    else:
        await msg.edit_text("❌ Не удалось.")


async def cmd_answ(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text("🔊 Использование: /answ вопрос")
        return
    if not check_rate(update.effective_user.id):
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    msg = await update.message.reply_text("🐺 Думаю...")
    response = await ask_gemini(update.effective_user.id, text)
    await msg.delete()
    await send_voice_reply(update, context, response)


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(voice_status_text(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=build_voice_keyboard(uid))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not check_rate(update.effective_user.id):
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    msg = await update.message.reply_text("🎤 Слушаю...")

    try:
        f = await context.bot.get_file(update.message.voice.file_id if update.message.voice else update.message.audio.file_id)
        mt = (update.message.voice or update.message.audio).mime_type or "audio/ogg"
        audio = await f.download_as_bytearray()
        
        if len(audio) > 20 * 1024 * 1024:
            await msg.edit_text("⚠️ Слишком большое (макс 20 МБ)")
            return

        response = await ask_gemini_audio(update.effective_user.id, bytes(audio), mt)
        await msg.delete()
        await send_voice_reply(update, context, response)
    except Exception as e:
        logger.error(f"🎤 {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text or not check_rate(update.effective_user.id):
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    response = await ask_gemini(update.effective_user.id, text)
    await safe_reply(update, response)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = update.effective_user.id

    try:
        if data == "noop":
            await query.answer()
        elif data == "about":
            await query.answer()
            await safe_edit(query, "🐺 *Волчишка v5.1*\nGemini + Groq\nАвтор: 4eLovE4e")
        elif data.startswith("clear_"):
            if int(data.split("_")[1]) == uid:
                user_histories.pop(uid, None)
                await query.answer("🗑 Очищено!", show_alert=True)
                await safe_edit(query, "🗑 История очищена!")
            else:
                await query.answer("⚠️ Не твоя кнопка", show_alert=True)
        elif data.startswith("voice_"):
            if int(data.split("_")[1]) == uid:
                await query.answer()
                await safe_edit(query, voice_status_text(uid), build_voice_keyboard(uid))
            else:
                await query.answer("⚠️ Не твоя кнопка", show_alert=True)
        elif data.startswith("setvoice_"):
            parts = data.split("_")
            if int(parts[1]) == uid:
                get_settings(uid)["voice"] = parts[2]
                await query.answer(f"Голос: {VOICES[parts[2]]['name']}", show_alert=True)
                await safe_edit(query, voice_status_text(uid), build_voice_keyboard(uid))
        elif data.startswith("setspeed_"):
            parts = data.split("_")
            if int(parts[1]) == uid:
                get_settings(uid)["speed"] = parts[2]
                await query.answer(f"Скорость: {SPEEDS[parts[2]]['name']}", show_alert=True)
                await safe_edit(query, voice_status_text(uid), build_voice_keyboard(uid))
        elif data.startswith("setpitch_"):
            parts = data.split("_")
            if int(parts[1]) == uid:
                get_settings(uid)["pitch"] = parts[2]
                await query.answer(f"Тон: {PITCHES[parts[2]]['name']}", show_alert=True)
                await safe_edit(query, voice_status_text(uid), build_voice_keyboard(uid))
        elif data.startswith("testvoice_"):
            if int(data.split("_")[1]) == uid:
                await query.answer("🔊 Генерирую...")
                audio = await text_to_speech("Привет! Я Волчишка!", uid)
                if audio:
                    await context.bot.send_voice(chat_id=update.effective_chat.id, voice=io.BytesIO(audio))
        elif data.startswith("showtext_"):
            full = context.bot_data.get(f"fulltext_{data.split('_')[1]}", "")
            if full:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=full)
                await query.answer()
    except Exception as e:
        logger.error(f"Callback: {e}")


def main():
    print(f"🐺 Волчишка v5.1 | {len(GEMINI_API_KEYS)} ключей Gemini")
    req = HTTPXRequest(connection_pool_size=8, connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=3.0)
    app = Application.builder().token(TELEGRAM_TOKEN).request(req).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("img", cmd_img))
    app.add_handler(CommandHandler("answ", cmd_answ))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

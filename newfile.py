

# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import os
import threading
import time

import aiohttp
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

BOT_TOKEN = os.getenv("BOT_TOKEN", "8997758632:AAEh6RgPdIhJrJnpwOgQJ12xN31PJAZ11dQ").strip()
DB_FILE = "users_db.json"

BRANCH_ID = 7885
TITLE_DIR = "azur-lane-start-building"

BASE_URL_READER = "https://xn--80aaig9ahr.xn--c1avg"
API_URL_VIEWS = f"{BASE_URL_READER}/api/activity/views/"

SLOTS = 12  # Фиксированное количество параллельных слотов
MAX_CYCLES = 1000  # Разумный верхний предел на число циклов

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("aiohttp").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

_async_loop = asyncio.new_event_loop()

def _run_loop_forever():
    asyncio.set_event_loop(_async_loop)
    _async_loop.run_forever()

threading.Thread(target=_run_loop_forever, daemon=True).start()

def run_coro_in_loop(coro):
    return asyncio.run_coroutine_threadsafe(coro, _async_loop)

db_lock = threading.Lock()
users_db = {}
active_farms = {}

def load_db():
    global users_db
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                users_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            users_db = {}
    else:
        users_db = {}

def save_db():
    with db_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(users_db, f, indent=4, ensure_ascii=False)

def get_user(chat_id):
    chat_id_str = str(chat_id)
    if chat_id_str not in users_db:
        users_db[chat_id_str] = {"token": None, "cycles": 1}
        save_db()
    return users_db[chat_id_str]

def update_user(chat_id, **kwargs):
    chat_id_str = str(chat_id)
    if chat_id_str not in users_db:
        users_db[chat_id_str] = {"token": None, "cycles": 1}
    users_db[chat_id_str].update(kwargs)
    save_db()

load_db()

# --- Клавиатуры ---

def get_main_menu_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🚀 Запустить фарм", callback_data="start_farm"),
        types.InlineKeyboardButton("⚙️ Настройки", callback_data="open_settings"),
        types.InlineKeyboardButton("🔄 Настроить циклы", callback_data="set_cycles"),
    )
    return markup

def get_settings_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🔑 Изменить токен", callback_data="set_token"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"),
    )
    return markup

# --- Тексты меню ---

def send_main_menu(chat_id, message_id=None):
    user = get_user(chat_id)
    token_status = "✅ Установлен" if user.get("token") else "❌ Не установлен"
    cycles = user.get("cycles", 1)

    text = (
        f"🤖 <b>Главное меню | ReManga AutoFarm</b>\n\n"
        f"📚 Тайтл: <code>{TITLE_DIR}</code>\n"
        f"🔑 Токен: <b>{token_status}</b>\n"
        f"🔄 Циклов за раз: <b>{cycles}</b>\n\n"
        f"<i>Выберите действие ниже:</i>"
    )

    if message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=get_main_menu_keyboard())
        except Exception:
            bot.send_message(chat_id, text, reply_markup=get_main_menu_keyboard())
    else:
        bot.send_message(chat_id, text, reply_markup=get_main_menu_keyboard())

def send_settings_menu(chat_id, message_id=None):
    user = get_user(chat_id)
    token_status = "✅ Установлен" if user.get("token") else "❌ Не установлен"

    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"🔑 Токен: <b>{token_status}</b>\n\n"
        f"<i>Выберите параметр для настройки:</i>"
    )

    if message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=get_settings_keyboard())
        except Exception:
            bot.send_message(chat_id, text, reply_markup=get_settings_keyboard())
    else:
        bot.send_message(chat_id, text, reply_markup=get_settings_keyboard())

# --- Утилиты ---

def generate_progress_bar(current, total, length=10):
    if total <= 0:
        return "░" * length
    filled = min(length, max(0, int((current / total) * length)))
    return "█" * filled + "░" * (length - filled)

def make_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Connection": "keep-alive",
        "Referer": f"{BASE_URL_READER}/",
    }

# --- API ---

async def fetch_chapter_page(session, branch_id, page):
    """
    Получает одну страницу списка глав.
    В логи выводится максимум деталей, чтобы можно было понять,
    что именно изменилось на сайте после обновления:
    - код статуса и тело ответа
    - является ли ответ вообще JSON'ом (или это HTML/логин-страница)
    - какие ключи есть в ответе верхнего уровня
    - структура первого элемента списка глав (сменились ли имена полей)
    """
    url = (
        f"{BASE_URL_READER}/api/v2/titles/chapters/"
        f"?branch_id={branch_id}&ordering=-index&page={page}&count=100&user_data=0"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            raw_text = await resp.text()
            log.info(
                f"GET chapters page={page} url={url} status={resp.status} "
                f"content_type={resp.headers.get('Content-Type')} "
                f"body_preview={raw_text[:500]!r}"
            )

            if resp.status != 200:
                log.error(
                    f"[page={page}] Не-200 статус ({resp.status}). Возможные причины: "
                    f"неверный/просроченный токен, изменился путь API после редизайна сайта, "
                    f"смена домена, бан по User-Agent/IP, требуется другой заголовок авторизации "
                    f"(например Cookie вместо Bearer)."
                )
                return []

            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as e:
                log.error(
                    f"[page={page}] Ответ не является JSON (вероятно вернулся HTML вместо API — "
                    f"сайт мог сменить структуру API, требовать другую авторизацию, "
                    f"или редиректит на страницу логина/капчу). "
                    f"JSONDecodeError: {e}. Тело ответа: {raw_text[:1000]!r}"
                )
                return []

            if not isinstance(data, dict):
                log.error(
                    f"[page={page}] Ожидался dict в ответе, получено {type(data).__name__}. "
                    f"Полный ответ: {json.dumps(data, ensure_ascii=False)[:1000]}"
                )
                return []

            log.info(f"[page={page}] Ключи верхнего уровня JSON: {list(data.keys())}")

            items = data.get("content") or data.get("results") or data.get("data") or []

            if not items:
                log.warning(
                    f"[page={page}] Список глав пуст. Полный JSON ответа (поможет понять новую "
                    f"структуру, если поля 'content'/'results' переименованы): "
                    f"{json.dumps(data, ensure_ascii=False)[:2000]}"
                )
                return []

            if not isinstance(items, list):
                log.error(f"[page={page}] Поле с главами не список, а {type(items).__name__}: {items!r}")
                return []

            sample = items[0] if items else None
            log.info(f"[page={page}] Получено {len(items)} элементов. Пример первого элемента: {sample!r}")

            if sample is not None and isinstance(sample, dict) and "id" not in sample:
                log.error(
                    f"[page={page}] У элементов главы нет поля 'id' — возможно поле переименовано "
                    f"после обновления сайта (например в 'chapter_id', 'pk' и т.п.). "
                    f"Доступные ключи: {list(sample.keys())}"
                )

            return items
    except asyncio.TimeoutError:
        log.error(f"[page={page}] TIMEOUT при запросе {url} — сервер не ответил за 10 секунд.")
        return []
    except aiohttp.ClientConnectionError as e:
        log.error(f"[page={page}] Ошибка соединения ({type(e).__name__}): {e}. Возможно сменился домен/DNS/SSL.")
        return []
    except Exception as e:
        log.exception(f"[page={page}] Непредвиденная ошибка при получении страницы: {type(e).__name__}: {e}")
        return []

async def fetch_all_chapter_ids_async(session, branch_id):
    chapter_ids = []
    page = 1

    while True:
        tasks = [fetch_chapter_page(session, branch_id, p) for p in range(page, page + 5)]
        results = await asyncio.gather(*tasks)

        stop_fetching = False
        for items in results:
            if not items:
                stop_fetching = True
            else:
                chapter_ids.extend(item["id"] for item in items if "id" in item)

        if stop_fetching:
            break
        page += 5

    seen = set()
    unique_ids = []
    for cid in chapter_ids:
        if cid not in seen:
            seen.add(cid)
            unique_ids.append(cid)

    log.info(f"Итого собрано уникальных ID глав: {len(unique_ids)}")
    return list(reversed(unique_ids))

# --- FarmProcess ---

class FarmProcess:
    def __init__(self, chat_id, message_id, token, cycles):
        self.chat_id = chat_id
        self.message_id = message_id
        self.token = token
        self.total_cycles = cycles

        self.cancelled = False
        active_farms[chat_id] = self

        self.current_cycle = 1
        self.status = "Инициализация..."
        self.chapters_total = 0

        self.farmed_ok = 0
        self.farmed_err = 0
        self.cur_speed = 0.0

    def cancel(self):
        self.cancelled = True

    def get_ui_text(self):
        bar = generate_progress_bar(self.farmed_ok + self.farmed_err, self.chapters_total)

        return (
            f"⚡ <b>Автофарм ReManga</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔄 <b>Цикл:</b> {self.current_cycle} / {self.total_cycles}\n"
            f"📢 <b>Статус:</b> {self.status}\n\n"
            f"📖 <b>Прогресс (чтение → снятие):</b>\n"
            f"└ [{bar}] {self.farmed_ok}/{self.chapters_total} (❌ {self.farmed_err})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>Скорость:</b> {self.cur_speed:.1f} гл/с\n"
        )

    def update_ui_sync(self):
        markup = types.InlineKeyboardMarkup()
        finished = self.cancelled or any(x in self.status for x in ["Завершено", "Ошибка", "Отменено", "Готово"])
        if finished:
            markup.add(types.InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_main"))
        else:
            markup.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel_farm"))

        try:
            bot.edit_message_text(
                self.get_ui_text(),
                chat_id=self.chat_id,
                message_id=self.message_id,
                reply_markup=markup,
            )
        except ApiTelegramException as e:
            if "message is not modified" not in str(e).lower():
                pass

    # --- POST: пометить главу прочитанной ---

    async def _post_one(self, session, ch_id):
        headers = {"Referer": f"{BASE_URL_READER}/manga/{TITLE_DIR}/{ch_id}"}
        for attempt in range(5):
            try:
                async with session.post(
                    API_URL_VIEWS,
                    json={"chapter": ch_id, "page": -1},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    body = await r.text()
                    log.info(f"POST ch={ch_id} attempt={attempt+1} status={r.status} body={body[:120]!r}")
                    if r.status in (200, 201, 204):
                        return True
            except asyncio.TimeoutError:
                log.warning(f"POST ch={ch_id} attempt={attempt+1} TIMEOUT")
            except aiohttp.ClientConnectionError as e:
                log.warning(f"POST ch={ch_id} attempt={attempt+1} CONN={type(e).__name__}:{e}")
            except Exception as e:
                log.warning(f"POST ch={ch_id} attempt={attempt+1} ERR={type(e).__name__}:{e}")
            await asyncio.sleep(0.4)
        log.error(f"POST ch={ch_id} FAILED all attempts")
        return False

    # --- DELETE: снять галочку немедленно после POST ---

    async def _delete_one(self, session, ch_id):
        for attempt in range(3):
            try:
                async with session.delete(
                    API_URL_VIEWS,
                    json={"chapter_ids": [ch_id]},
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as r:
                    body = await r.text()
                    ok = r.status in (200, 204)
                    log.info(f"DELETE ch={ch_id} attempt={attempt+1} status={r.status} ok={ok} body={body[:120]!r}")
                    if ok:
                        return True
            except Exception as e:
                log.warning(f"DELETE ch={ch_id} attempt={attempt+1} ERR={type(e).__name__}:{e}")
            await asyncio.sleep(0.15 * (attempt + 1))
        log.error(f"DELETE ch={ch_id} FAILED all attempts")
        return False

    # --- Конвейерный фарм: N параллельных слотов, каждый строго POST→DELETE ---

    async def _farm_one_sequential(self, session, ch_id, failed):
        post_ok = await self._post_one(session, ch_id)
        if not post_ok:
            failed.append(ch_id)
            self.farmed_err += 1
            return

        await asyncio.sleep(0.4)

        del_ok = await self._delete_one(session, ch_id)
        if not del_ok:
            await asyncio.sleep(0.15)
            del_ok = await self._delete_one(session, ch_id)

        if del_ok:
            self.farmed_ok += 1
        else:
            failed.append(ch_id)
            self.farmed_err += 1

    async def _farm_chapters(self, session, chapter_ids):
        self.farmed_ok = 0
        self.farmed_err = 0
        t_start = time.time()
        failed = []

        queue = asyncio.Queue()
        for ch_id in chapter_ids:
            await queue.put(ch_id)

        async def slot_worker():
            while not self.cancelled:
                try:
                    ch_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._farm_one_sequential(session, ch_id, failed)
                queue.task_done()

        await asyncio.gather(*(slot_worker() for _ in range(SLOTS)))

        elapsed = time.time() - t_start
        self.cur_speed = (self.farmed_ok + self.farmed_err) / elapsed if elapsed > 0 else 0

        retry_round = 0
        while failed and not self.cancelled:
            retry_round += 1
            if retry_round > 3:
                break
            self.status = f"🔄 Повтор ({len(failed)} гл., попытка {retry_round}/3)"
            await asyncio.sleep(0.3)
            still_failed = []
            for ch_id in failed:
                if self.cancelled:
                    break
                post_ok = await self._post_one(session, ch_id)
                if post_ok:
                    del_ok = await self._delete_one(session, ch_id)
                    if del_ok:
                        self.farmed_ok += 1
                        self.farmed_err -= 1
                    else:
                        still_failed.append(ch_id)
                else:
                    still_failed.append(ch_id)
                await asyncio.sleep(0.05)
            failed = still_failed

    async def run_async(self):
        connector = aiohttp.TCPConnector(
            limit=0,
            limit_per_host=20,
            ttl_dns_cache=300,
            use_dns_cache=True,
            force_close=False,
            enable_cleanup_closed=True,
        )

        async with aiohttp.ClientSession(
            headers=make_headers(self.token),
            connector=connector,
        ) as session:
            self.status = "🔍 Сбор ID глав..."
            await asyncio.to_thread(self.update_ui_sync)

            chapter_ids = await fetch_all_chapter_ids_async(session, BRANCH_ID)
            self.chapters_total = len(chapter_ids)

            if not chapter_ids:
                self.status = "❌ Ошибка: Главы не найдены! Проверь ветку/токен (подробности в логах бота)."
                self.cancelled = True
                await asyncio.to_thread(self.update_ui_sync)
                active_farms.pop(self.chat_id, None)
                return

            stop_ui_ticker = False

            async def ui_ticker():
                while not stop_ui_ticker:
                    await asyncio.to_thread(self.update_ui_sync)
                    await asyncio.sleep(1.5)

            ticker_task = asyncio.create_task(ui_ticker())

            try:
                for cycle in range(1, self.total_cycles + 1):
                    if self.cancelled:
                        break

                    self.current_cycle = cycle
                    self.status = "🔥 Фарм (чтение + снятие)"

                    await self._farm_chapters(session, chapter_ids)

                    if self.cancelled:
                        break

                    if cycle < self.total_cycles:
                        self.status = "⏳ Пауза между циклами..."
                        await asyncio.sleep(0.1)

            finally:
                stop_ui_ticker = True
                await ticker_task

        self.status = "🚫 Отменено" if self.cancelled else "✅ Готово!"
        await asyncio.to_thread(self.update_ui_sync)
        active_farms.pop(self.chat_id, None)

    def start(self):
        run_coro_in_loop(self.run_async())

# --- Handlers ---

@bot.message_handler(commands=["start"])
def cmd_start(message):
    send_main_menu(message.chat.id)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    action = call.data

    if action == "back_to_main":
        send_main_menu(chat_id, msg_id)
    elif action == "open_settings":
        send_settings_menu(chat_id, msg_id)
    elif action == "set_token":
        msg = bot.edit_message_text(
            "🔑 <b>Отправьте свой токен:</b>\n\n<i>Для отмены введите /start</i>",
            chat_id=chat_id, message_id=msg_id,
        )
        bot.register_next_step_handler(msg, process_token_input)
    elif action == "set_cycles":
        msg = bot.edit_message_text(
            f"🔄 <b>Сколько циклов запускать?</b> (1-{MAX_CYCLES})",
            chat_id=chat_id, message_id=msg_id,
        )
        bot.register_next_step_handler(msg, process_cycles_input)
    elif action == "start_farm":
        user = get_user(chat_id)
        if not user.get("token"):
            bot.answer_callback_query(call.id, "❌ Сначала установите токен", show_alert=True)
            return
        if chat_id in active_farms:
            bot.answer_callback_query(call.id, "⚠️ Фарм уже запущен!", show_alert=True)
            return
        bot.edit_message_text("⏳ Подготовка запуска...", chat_id=chat_id, message_id=msg_id)
        farm = FarmProcess(
            chat_id, msg_id,
            user["token"],
            user.get("cycles", 1),
        )
        farm.start()
    elif action == "cancel_farm":
        if chat_id in active_farms:
            active_farms[chat_id].cancel()
            bot.answer_callback_query(call.id, "🛑 Останавливаем...", show_alert=False)
        else:
            bot.answer_callback_query(call.id, "Фарм не активен", show_alert=False)

# --- Input handlers ---

def process_token_input(message):
    if message.text == "/start":
        send_main_menu(message.chat.id)
        return
    update_user(message.chat.id, token=message.text.strip())
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    bot.send_message(message.chat.id, "✅ <b>Токен успешно сохранён!</b>")
    send_settings_menu(message.chat.id)

def process_cycles_input(message):
    if message.text == "/start":
        send_main_menu(message.chat.id)
        return
    if not message.text.isdigit() or int(message.text) < 1:
        msg = bot.send_message(message.chat.id, "❌ Отправьте корректное число:")
        bot.register_next_step_handler(msg, process_cycles_input)
        return
    value = int(message.text)
    if value > MAX_CYCLES:
        msg = bot.send_message(
            message.chat.id,
            f"❌ Слишком много. Максимум {MAX_CYCLES}. Отправьте корректное число:",
        )
        bot.register_next_step_handler(msg, process_cycles_input)
        return
    update_user(message.chat.id, cycles=value)
    bot.send_message(message.chat.id, f"✅ <b>Циклов установлено: {value}</b>")
    send_main_menu(message.chat.id)

@bot.message_handler(commands=["debug"])
def cmd_debug(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    if not user.get("token"):
        bot.send_message(chat_id, "❌ Сначала установите токен")
        return

    async def do_debug():
        headers = make_headers(user["token"])
        async with aiohttp.ClientSession(headers=headers) as session:
            url_first = (
                f"{BASE_URL_READER}/api/v2/titles/chapters/"
                f"?branch_id={BRANCH_ID}&ordering=-index&page=1&count=1&user_data=0"
            )
            try:
                async with session.get(url_first, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    raw_text = await r.text()
                    log.info(f"[debug] status={r.status} body={raw_text[:1000]!r}")
                    bot.send_message(
                        chat_id,
                        f"GET status={r.status}\ncontent_type={r.headers.get('Content-Type')}\n"
                        f"<code>{raw_text[:800]}</code>",
                    )

                    try:
                        data = json.loads(raw_text)
                    except json.JSONDecodeError:
                        bot.send_message(chat_id, "❌ Ответ не JSON — сайт вернул HTML (см. текст выше). Обновился API.")
                        return

                    items = data.get("content") or data.get("results") or data.get("data") or []
                    if not items:
                        bot.send_message(
                            chat_id,
                            f"❌ Главы не найдены.\nКлючи ответа: {list(data.keys())}\n"
                            f"Полный JSON:\n<code>{json.dumps(data, ensure_ascii=False)[:1500]}</code>",
                        )
                        return

                    ch_id = items[0].get("id")
                    if ch_id is None:
                        bot.send_message(
                            chat_id,
                            f"❌ У главы нет поля 'id'. Доступные ключи: {list(items[0].keys())}",
                        )
                        return

                    post_headers = {"Referer": f"{BASE_URL_READER}/manga/{TITLE_DIR}/{ch_id}"}

                    async with session.post(
                        API_URL_VIEWS,
                        json={"chapter": ch_id, "page": -1},
                        headers=post_headers,
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as rp:
                        pb = await rp.text()
                        bot.send_message(chat_id, f"POST ch={ch_id} status={rp.status}\n<code>{pb[:500]}</code>")

                    async with session.delete(
                        API_URL_VIEWS,
                        json={"chapter_ids": [ch_id]},
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as rd:
                        db_body = await rd.text()
                        bot.send_message(
                            chat_id,
                            f"DELETE (immediate) ch={ch_id} status={rd.status}\n<code>{db_body[:500]}</code>",
                        )
            except Exception as e:
                log.exception("Ошибка в /debug")
                bot.send_message(chat_id, f"❌ Исключение при отладке: {type(e).__name__}: {e}")

    run_coro_in_loop(do_debug())

if __name__ == "__main__":
    log.info("Бот запущен, начинаю polling...")
    bot.infinity_polling(skip_pending=True)
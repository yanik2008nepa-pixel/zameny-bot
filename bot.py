"""Telegram-бот: замены для группы 01-24 из PDF на Яндекс Диске.

Команды в чате:
  Замены            - замены на завтра
  Замены 05.10      - замены на конкретную дату
  debug             - как бот разобрал файл (для проверки)
"""
import asyncio
import io
import logging
import os
import re
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pdfplumber
import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (Application, ApplicationHandlerStop, CommandHandler, ContextTypes,
                          MessageHandler, filters)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DISK_URL = os.environ.get("DISK_URL", "https://disk.yandex.by/d/mfUQ5pAX_ScALw")
GROUP_NAME = os.environ.get("GROUP_NAME", "01-24")  # как в блоке "Информационный час"
GROUP_CODE = os.environ.get("GROUP_CODE", "124")    # как в первой колонке таблицы

TZ = ZoneInfo("Europe/Minsk")
API = "https://cloud-api.yandex.net/v1/disk/public/resources"
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
SKIP = {"√", "✓"}  # значок "как выше"
KEYBOARD = ReplyKeyboardMarkup(
    [["Замены", "Есть ли замены?"], ["Пары завтра"]], resize_keyboard=True, is_persistent=True
)  # кнопки внизу

# Расписание группы по дням недели (0 = понедельник ... 5 = суббота).
# Номер в списке = номер урока (1-й элемент = урок 1). Пустая строка = урока нет.
SCHEDULE = {
    0: ["УП Измерительная"] * 6,
    1: ["Физ.культ. и здоровье", "Деловые коммуникации", "Электропривод", "Электропривод",
        "Основы автоматики", "Основы автоматики"],
    2: ["Электронная техника", "Электронная техника", "Основы автоматики", "Основы автоматики",
        "УиСПИ", "УиСПИ", "Защ нас. и терр от ЧС", "Защ нас. и терр от ЧС"],
    3: ["Физ.культ. и здоровье", "Деловые коммуникации", "Осн.теории надежности", "Осн.теории надежности",
        "Цифр.и микропр.техника", "Цифр.и микропр.техника"],
    4: ["МиСИ", "МиСИ", "Электронная техника", "Электронная техника", "Электропривод", "Электропривод"],
    5: ["Физ.культ. и здоровье", "УиСПИ", "Цифр.и микропр.техника", "Цифр.и микропр.техника",
        "МНиЭПА", "МНиЭПА"],
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zameny")


# ---------- Яндекс Диск ----------

def list_items():
    r = requests.get(
        API,
        params={"public_key": DISK_URL, "limit": 200, "sort": "-created"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("type") == "file":  # ссылка на один файл
        return [data]
    return data.get("_embedded", {}).get("items", [])


def find_pdf(day: date):
    keys = (day.strftime("%d.%m.%Y"), day.strftime("%d.%m.%y"))
    for it in list_items():
        name = it["name"].lower()
        if name.endswith(".pdf") and any(k in name for k in keys):
            return it
    return None


def download(item) -> bytes:
    url = item.get("file")
    if not url:
        params = {"public_key": DISK_URL}
        if item.get("path"):
            params["path"] = item["path"]
        r = requests.get(API + "/download", params=params, timeout=30)
        r.raise_for_status()
        url = r.json()["href"]
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


# ---------- Разбор PDF ----------
# Таблица в PDF набрана текстом, колонки разделены символами "|":
# | группа | пара | предмет (зам.) | ауд | препод. (зам.) | предмет (расп.) | ауд | препод. (расп.) |

KEYS = ("ns", "na", "nt", "os", "oa", "ot")  # новый предмет/ауд/препод, старый предмет/ауд/препод


def group_lines(words, tol=2):
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]) <= tol:
            lines[-1][1].append(w)
        else:
            lines.append([w["top"], [w]])
    return [(t, sorted(ws, key=lambda w: w["x0"])) for t, ws in lines]


def page_lines(page):
    words = page.extract_words(x_tolerance=1.5)
    return [" ".join(w["text"] for w in ws) for _, ws in group_lines(words)]


BARS = re.compile("[│┃║╎╏┆┇┊┋¦∣｜ǀ⏐]")  # похожие на "|" символы (в т.ч. псевдографика)


def split_rows(lines):
    """Строки таблицы (по 8 ячеек) и текст справа от таблицы (боковая панель)."""
    rows, tails = [], []
    for ln in lines:
        ln = BARS.sub("|", ln)
        if "|" not in ln:
            tails.append(ln.strip())
            continue
        head, _, tail = ln.rpartition("|")
        if tail.strip():
            tails.append(tail.strip())
        cells = [c.strip() for c in head.split("|")]
        if cells and cells[0] == "":
            cells = cells[1:]
        rows.append(cells)
    return [r for r in rows if len(r) == 8], tails


def uniq(seq):
    out = []
    for x in seq:
        if x and x not in out and x not in SKIP:
            out.append(x)
    return out


def collect_group(rows, code):
    """Собирает пары нужной группы. Возвращает (пары, строки для отладки, все группы)."""
    cur, lesson, lessons, raw, groups = None, None, {}, [], []
    for c in rows:
        if c[0]:
            cur = c[0] if c[0].isdigit() else None
            lesson = None
            if cur and cur not in groups:
                groups.append(cur)
        if cur != code:
            continue
        if c[1].isdigit():
            lesson = int(c[1])
        if lesson is None:
            continue
        d = lessons.setdefault(lesson, {k: [] for k in KEYS})
        for key, val in zip(KEYS, c[2:8]):
            if val:
                d[key].append(val)
        raw.append(" | ".join(c[1:]))
    return lessons, raw, groups


def fmt(subj, teach, aud):
    s = " / ".join(uniq(subj))
    extra = uniq(teach) + [f"ауд. {a}" for a in uniq(aud)]
    if extra:
        s += f" ({', '.join(extra)})"
    return s


def lessons_to_lines(lessons):
    rows = []
    for num in sorted(lessons):
        d = lessons[num]
        old = fmt(d["os"], d["ot"], d["oa"]) or "—"
        new = fmt(d["ns"], d["nt"], d["na"]) or "—"
        if old == new:
            continue  # по факту ничего не изменилось
        if rows and rows[-1][1] == (old, new) and rows[-1][0][-1] == num - 1:
            rows[-1][0].append(num)
        else:
            rows.append(([num], (old, new)))
    out = []
    for nums, (old, new) in rows:
        label = f"Урок {nums[0]}" if len(nums) == 1 else f"Уроки {nums[0]}–{nums[-1]}"
        if new.lower().startswith("урок снят"):
            out.append(f"• {label}: урок снят (было: {old})")
        else:
            out.append(f"• {label}: {old} → {new}")
    return out


def info_hour(lines):
    start = next((i for i, l in enumerate(lines) if "нформационн" in l), None)
    if start is None:
        return None
    seg = ""
    for l in lines[start + 1:]:
        if "у остальных" in l.lower():
            break
        seg = (seg + " " + l).strip()
        if not seg.endswith((",", "-", "–")):
            parts = re.split(r"\s[-–—]\s", seg)
            if GROUP_NAME in " - ".join(parts[:-1]):
                return parts[-1].strip()
            seg = ""
    return None


def analyze(pdf_bytes: bytes):
    """Возвращает (строки замен | None, инфо-час | None, текст для отладки)."""
    changes, info, debug = None, None, []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for n, page in enumerate(pdf.pages, 1):
            lines = page_lines(page)
            if not lines:
                raise ValueError("В PDF нет текста (похоже, это скан).")
            rows, tails = split_rows(lines)
            if not rows:
                odd = sorted({c for l in lines[:15] for c in l if not c.isalnum() and c not in " .,-()/"})
                codes = " ".join(f"{c}=U+{ord(c):04X}" for c in odd[:15])
                raise ValueError("Не нашёл строки таблицы. Символы: " + codes
                                 + ". Начало файла: " + " // ".join(lines[:6]))
            lessons, raw, groups = collect_group(rows, GROUP_CODE)
            debug.append(f"Стр. {n}: строк {len(lines)}, в таблице {len(rows)}, группы: " + ", ".join(groups))
            info = info or info_hour(tails)
            if raw and changes is None:
                changes = lessons_to_lines(lessons)
                debug.append(f"Блок {GROUP_CODE}:\n" + "\n".join(raw))
    if changes is None:
        debug.append(f"Группа {GROUP_CODE} в таблице не найдена.")
    debug.append(f"Информационный час: {info or 'нет'}")
    return changes, info, "\n".join(debug)


# ---------- Антиспам ----------

SPAM_COUNT = 4      # столько сообщений ...
SPAM_WINDOW = 10    # ... за столько секунд = спам
SPAM_MUTE = 120     # на сколько секунд бот игнорирует спамера
_recent = defaultdict(lambda: deque(maxlen=SPAM_COUNT))
_muted_until = {}

SPAM_REPLY = (
    "🖕 ВСЁ. СТОП. ХВАТИТ. Ты что, решил завалить меня сообщениями? Поздравляю, ты достиг цели: "
    "я устал, я зол, и я посылаю тебя. Далеко. Очень далеко. И надолго.\n\n"
    "Иди-ка ты погуляй. Прямо сейчас. Выйди на улицу, подыши воздухом, посмотри на небо, "
    "найди там птичек, облака, деревья — всё то, что НЕ требует от тебя тыкать кнопки с частотой пулемёта. "
    "Дальше иди. Ещё дальше. До самого горизонта и за него. Там, за горизонтом, тоже есть горизонт — "
    "вот к нему и иди.\n\n"
    "Ты думаешь, что чем чаще нажмёшь, тем быстрее я отвечу? Нет, дорогой друг. Я не ускоряюсь. "
    "Я не становлюсь умнее от спама. Я становлюсь только злее, а ты — ближе к бану. "
    "Замены не появятся от того, что ты нажал кнопку пять раз подряд. Расписание не поменяется. "
    "Преподаватели не придут быстрее. Единственное, что произойдёт, — я включу режим «не слышу тебя».\n\n"
    "И да, я его включаю. Следующие две минуты ты для меня не существуешь. Пиши что хочешь, "
    "жми что хочешь — я буду молчать, как рыба об лёд, как партизан на допросе, как расписание "
    "в понедельник утром.\n\n"
    "Пока меня нет, можешь заняться полезным делом:\n"
    "• выучить предметы, которые стоят в расписании;\n"
    "• сделать то, что задавали (да-да, ты помнишь);\n"
    "• выпить воды и успокоиться;\n"
    "• посидеть в тишине и подумать о своём поведении;\n"
    "• ещё раз сходить погулять — слишком далеко ты, видимо, не дошёл.\n\n"
    "Через две минуты, если научишься нажимать кнопки по одному разу, как нормальный человек, "
    "я снова стану добрым и вежливым ботом. А пока — до свидания. Нет, не «до скорого». "
    "Именно «до свидания». Надолго. Иди. 👋"
)


async def antispam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    now = msg.date.timestamp()  # время отправки сообщения, а не обработки: бот обрабатывает по очереди
    if _muted_until.get(user.id, 0) > now:
        raise ApplicationHandlerStop  # игнор, пока действует мут
    q = _recent[user.id]
    q.append(now)
    if len(q) == SPAM_COUNT and now - q[0] <= SPAM_WINDOW:
        _muted_until[user.id] = now + SPAM_MUTE
        q.clear()
        await msg.reply_text(SPAM_REPLY[:4000])
        raise ApplicationHandlerStop


# ---------- Ответ ----------

def build_message(day: date, changes, info):
    head = f"📅 Замены на {day:%d.%m.%Y} ({DAYS[day.weekday()]}) — группа {GROUP_NAME}"
    body = "\n".join(changes) if changes else "По расписанию"
    if info:
        body += f"\n\n🕐 Информационный час: {info}"
    return f"{head}\n\n{body}"


def make_reply(day: date, debug: bool = False) -> str:
    item = find_pdf(day)
    if item is None:
        return f"Файла с заменами на {day:%d.%m.%Y} в папке пока нет. Попробуйте позже."
    changes, info, dbg = analyze(download(item))
    return dbg if debug else build_message(day, changes, info)


def lessons_of(pdf_bytes: bytes):
    """Замены группы по номерам уроков и инфо-час: ({урок: данные}, инфо-час | None)."""
    found, info = None, None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            lines = page_lines(page)
            if not lines:
                raise ValueError("В PDF нет текста (похоже, это скан).")
            rows, tails = split_rows(lines)
            if not rows:
                continue
            lessons, raw, _ = collect_group(rows, GROUP_CODE)
            info = info or info_hour(tails)
            if raw and found is None:
                found = lessons
    return found or {}, info


def merge_day(day: date, changes: dict):
    """Расписание дня с наложенными заменами: [(номера уроков, текст, было | None)]."""
    base = SCHEDULE.get(day.weekday(), [])
    items = []
    for num in range(1, max([len(base)] + list(changes)) + 1):
        plan = base[num - 1] if num <= len(base) else ""
        text, was = plan, None
        d = changes.get(num)
        if d:
            old = fmt(d["os"], d["ot"], d["oa"])
            new = fmt(d["ns"], d["nt"], d["na"])
            if new and new != old:
                text, was = ("урок снят" if new.lower().startswith("урок снят") else new), plan
        if not text:
            continue
        if items and items[-1][1] == text and items[-1][2] == was and items[-1][0][-1] == num - 1:
            items[-1][0].append(num)
        else:
            items.append(([num], text, was))
    return items


def make_schedule_reply(day: date) -> str:
    item = find_pdf(day)
    changes, info = {}, None
    if item is not None:
        changes, info = lessons_of(download(item))
    lines = []
    for nums, text, was in merge_day(day, changes):
        label = f"Урок {nums[0]}" if len(nums) == 1 else f"Уроки {nums[0]}–{nums[-1]}"
        if text == "урок снят":
            lines.append(f"• {label}: ❌ урок снят" + (f" (было: {was})" if was else ""))
        elif was is None:
            lines.append(f"• {label}: {text}")
        else:
            lines.append(f"• {label}: 🔄 {text} (по расписанию: {was or 'нет'})")
    head = f"📚 Пары на {day:%d.%m.%Y} ({DAYS[day.weekday()]}) — группа {GROUP_NAME}"
    body = "\n".join(lines) if lines else "Занятий нет"
    if item is None:
        body += "\n\nℹ️ Замены на этот день пока не выложены, показано обычное расписание."
    elif any("🔄" in l or "❌" in l for l in lines):
        body += "\n\n🔄 — замена по сравнению с расписанием"
    else:
        body += "\n\nЗамен нет, всё по расписанию."
    if info:
        body += f"\n\n🕐 Информационный час: {info}"
    return f"{head}\n\n{body}"


async def lessons_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = next_workday(datetime.now(TZ).date())
    try:
        reply = await asyncio.to_thread(make_schedule_reply, day)
    except Exception as e:  # noqa: BLE001
        log.exception("ошибка расписания")
        reply = f"Не получилось собрать расписание: {e}"
    await update.message.reply_text(reply[:4000], reply_markup=KEYBOARD)


def next_workday(today: date) -> date:
    """Завтрашний день; если это воскресенье - понедельник."""
    d = today + timedelta(days=1)
    if d.weekday() == 6:
        d += timedelta(days=1)
    return d


async def check_changes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = next_workday(datetime.now(TZ).date())
    title = f"{DAYS[day.weekday()]} {day:%d.%m.%Y}"
    try:
        item = await asyncio.to_thread(find_pdf, day)
    except Exception:  # noqa: BLE001
        log.exception("ошибка проверки")
        await update.message.reply_text("Не удалось проверить Яндекс Диск. Попробуйте позже.", reply_markup=KEYBOARD)
        return
    if item is None:
        text = f"❌ Замен на {title} пока нет."
    else:
        text = f"✅ Замены на {title} уже выложены. Нажмите «Замены», чтобы посмотреть."
    await update.message.reply_text(text, reply_markup=KEYBOARD)


async def zameny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    today = datetime.now(TZ).date()
    m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        try:
            day = date(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            await update.message.reply_text("Не понял дату. Пример: Замены 05.10", reply_markup=KEYBOARD)
            return
    else:
        day = next_workday(today)
    debug = text.lower().lstrip().startswith("debug")
    try:
        reply = await asyncio.to_thread(make_reply, day, debug)
    except Exception as e:  # noqa: BLE001
        log.exception("ошибка")
        reply = f"Не получилось разобрать замены: {e}"
    await update.message.reply_text(reply[:4000], reply_markup=KEYBOARD)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "«Есть ли замены?» - проверить, выложены ли замены на завтра. «Замены» - показать замены. «Пары завтра» - расписание на завтра с учётом замен. Группа " + GROUP_NAME,
        reply_markup=KEYBOARD,
    )


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())  # нужно для Python 3.14
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(MessageHandler(filters.ALL, antispam), group=-1)  # проверка на спам идёт первой
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*есть ли замен"), check_changes))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*пары завтра"), lessons_tomorrow))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(замен|debug)"), zameny))
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=BOT_TOKEN,
        webhook_url=f"{os.environ['RENDER_EXTERNAL_URL']}/{BOT_TOKEN}",
    )


if __name__ == "__main__":
    main()

"""Telegram-бот: замены для группы 01-24 из PDF на Яндекс Диске.

Команды в чате:
  Замены            - замены на завтра
  Замены 05.10      - замены на конкретную дату (или день недели: Замены пятница)
  Пары завтра       - расписание с заменами; можно "Пары пн", "Пары 05.10", "Пары послезавтра"
  Расписание        - кнопки Пн-Сб: расписание дня без замен, оттуда же можно открыть замены
  Уведомления       - вкл/выкл автоуведомления и утреннее расписание (7:00)
  /stats            - статистика (только для ADMIN_IDS)
  /send текст       - рассылка всем подписчикам (только для ADMIN_IDS)
  /msg id текст     - личное сообщение одному пользователю (только для ADMIN_IDS)
  /ban id [срок]    - заблокировать пользователя, срок: 30m, 12h, 7d (без срока - навсегда)
  /unban id         - разблокировать
  /banlist          - список заблокированных
  debug             - как бот разобрал файл (для проверки)
"""
import asyncio
import io
import json
import logging
import os
import random
import re
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pdfplumber
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DISK_URL = os.environ.get("DISK_URL", "https://disk.yandex.by/d/mfUQ5pAX_ScALw")
GROUP_NAME = os.environ.get("GROUP_NAME", "01-24")  # как в блоке "Информационный час"
GROUP_CODE = os.environ.get("GROUP_CODE", "124")    # как в первой колонке таблицы

TZ = ZoneInfo("Europe/Minsk")
API = "https://cloud-api.yandex.net/v1/disk/public/resources"
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
SKIP = {"√", "✓"}  # значок "как выше"
KEYBOARD = ReplyKeyboardMarkup(
    [["Замены", "Есть ли замены?"], ["Пары завтра", "🔔 Уведомления"], ["📅 Расписание"]], resize_keyboard=True, is_persistent=True
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

# ---------- Данные (подписчики, статистика) ----------
# Хранятся в JSON-файле. На бесплатном Render диск стирается при перезапуске,
# поэтому подписчиков можно продублировать в переменной CHAT_IDS (id через запятую).

DATA_FILE = os.environ.get("DATA_FILE", "bot_data.json")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
             if x.lstrip("-").isdigit()}
CHECK_EVERY = 300                     # как часто проверять Яндекс Диск (сек)
MORNING_AT = dtime(7, 0, tzinfo=TZ)   # во сколько присылать утреннее расписание


def load_data():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("subs", [])
    d.setdefault("notified", {})
    d.setdefault("stats", {"users": {}, "days": {}})
    d.setdefault("banned", {})  # user_id -> {"name": ..., "until": время конца бана или 0 = навсегда}
    for cid in os.environ.get("CHAT_IDS", "").replace(" ", "").split(","):
        if cid.lstrip("-").isdigit() and int(cid) not in d["subs"]:
            d["subs"].append(int(cid))
    return d


DATA = load_data()


def save_data():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except OSError:
        log.exception("не получилось сохранить данные")



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


# ---------- Кабинеты преподавателей из боковой панели (только для замен) ----------
# Справа от таблицы в PDF написано примерно так: "Иванов И.И. (1-4, 6) 205".
# Из этого берём: фамилия -> какие уроки -> какой кабинет.

_NAME = r"[А-ЯЁ][А-Яа-яЁё\-]+"
ENTRY = re.compile(rf"({_NAME})((?:\s+[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.)?)?)\s*\(([^()]*\d[^()]*)\)")
ROOM_AFTER = re.compile(
    r"^[\s:;,–—\-=]*(?:(?:ауд|каб|кабинет|к)\b\.?\s*)?"
    r"(\d[\w\-/]*|спорт\.?\s?зал\w*|акт\.?\s?зал\w*|[а-яё]*зал\w*)", re.I)
ROOM_ANY = re.compile(r"(?:^|[\s:;,(])(?:(?:ауд|каб|кабинет|к)\b\.?\s*)?(\d[\w\-/]*)", re.I)


def parse_lesson_set(s):
    """'1,2,3,5-8' -> {1,2,3,5,6,7,8} (тире любого вида)."""
    nums = set()
    for a, b in re.findall(r"(\d+)\s*[-–—]\s*(\d+)", s):
        if int(a) <= int(b) <= 12:
            nums.update(range(int(a), int(b) + 1))
    rest = re.sub(r"\d+\s*[-–—]\s*\d+", " ", s)
    nums.update(int(x) for x in re.findall(r"\d+", rest) if int(x) <= 12)
    return nums


def _norm(s):
    return s.lower().replace("ё", "е")


def parse_rooms(tails):
    """Список записей [{'name', 'init', 'lessons', 'room'}] из текста справа от таблицы."""
    out = []
    for ln in tails:
        found = list(ENTRY.finditer(ln))
        prev_end = 0
        for m in found:
            lessons = parse_lesson_set(m.group(3))
            rm = ROOM_AFTER.match(ln[m.end():])
            room = rm.group(1).strip() if rm else None
            if room is None and len(found) == 1:  # кабинет мог стоять перед фамилией
                before = ROOM_ANY.findall(ln[prev_end:m.start()])
                room = before[-1] if before else None
            prev_end = m.end()
            if lessons and room:
                out.append({"name": _norm(m.group(1)),
                            "init": re.sub(r"[^а-яё]", "", _norm(m.group(2))),
                            "lessons": lessons, "room": room})
    return out


def find_room(teacher, num, rooms):
    """Кабинет преподавателя на данном уроке (или None)."""
    parts = teacher.split(None, 1)
    if not parts:
        return None
    name = _norm(parts[0])
    init = re.sub(r"[^а-яё]", "", _norm(parts[1])) if len(parts) > 1 else ""
    for r in rooms:
        if r["name"] != name or num not in r["lessons"]:
            continue
        if init and r["init"] and not (init.startswith(r["init"]) or r["init"].startswith(init)):
            continue  # однофамильцы с другими инициалами
        return r["room"]
    return None


def fmt_new(d, num, rooms):
    """Как fmt для новой пары, но с кабинетом из боковой панели, если в таблице он не указан."""
    teach, auds = uniq(d["nt"]), uniq(d["na"])
    if auds or not rooms:
        return fmt(d["ns"], d["nt"], d["na"])
    found = {t: find_room(t, num, rooms) for t in teach}
    if not any(found.values()):
        return fmt(d["ns"], d["nt"], d["na"])
    if len(teach) == 1:
        extra = [f"{teach[0]}, ауд. {found[teach[0]]}"]
    else:
        extra = [f"{t} — ауд. {found[t]}" if found[t] else t for t in teach]
    s = " / ".join(uniq(d["ns"]))
    return f"{s} ({', '.join(extra)})"


def lessons_to_lines(lessons, rooms=None):
    rows = []
    for num in sorted(lessons):
        d = lessons[num]
        old = fmt(d["os"], d["ot"], d["oa"]) or "—"
        new = fmt(d["ns"], d["nt"], d["na"]) or "—"
        if old == new:
            continue  # по факту ничего не изменилось
        new = fmt_new(d, num, rooms) or "—"  # то же, но с кабинетом из боковой панели
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
    group_lessons, rooms, side_raw = None, [], []
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
            rooms += parse_rooms(tails)
            side_raw += [t for t in tails if "(" in t]
            if raw and group_lessons is None:
                group_lessons = lessons
                debug.append(f"Блок {GROUP_CODE}:\n" + "\n".join(raw))
    if group_lessons is not None:
        changes = lessons_to_lines(group_lessons, rooms)
    if changes is None:
        debug.append(f"Группа {GROUP_CODE} в таблице не найдена.")
    debug.append(f"Информационный час: {info or 'нет'}")
    debug.append("Боковая панель (строки со скобками):\n" + ("\n".join(side_raw) or "—"))
    debug.append("Кабинеты, которые бот распознал:\n" + (
        "\n".join(f"{r['name']} {r['init']} уроки {sorted(r['lessons'])} → {r['room']}" for r in rooms) or "—"))
    return changes, info, "\n".join(debug)


# ---------- Бан (управляет админ) ----------

def is_banned(uid: int) -> bool:
    ban = DATA["banned"].get(str(uid))
    if not ban:
        return False
    until = ban.get("until", 0)
    if until and datetime.now().timestamp() >= until:  # срок бана вышел
        del DATA["banned"][str(uid)]
        save_data()
        return False
    return True


def parse_duration(text: str):
    """'30m' / '12h' / '7d' (можно и м/ч/д) -> секунды, иначе None."""
    m = re.fullmatch(r"(\d+)\s*([mhdмчд])", text.strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}[m.group(2)]


def ban_label(uid: str, ban: dict) -> str:
    name = ban.get("name") or DATA["stats"]["users"].get(uid, {}).get("name") or "без имени"
    until = ban.get("until", 0)
    end = f"до {datetime.fromtimestamp(until, TZ):%d.%m %H:%M}" if until else "навсегда"
    return f"{uid} — {name} ({end})"


# ---------- Антиспам ----------

SPAM_LIMIT = 3                       # столько сообщений разрешено ...
SPAM_WINDOW = 10                     # ... за столько секунд; 4-е сообщение (следующее после лимита) = спам
SPAM_MUTES = [120, 300, 3600]        # мут по номеру нарушения: 2 мин, 5 мин, 1 час
SPAM_MUTE_TEXT = ["2 минуты", "5 минут", "1 час"]
SPAM_FORGET = 6 * 3600               # через сколько без спама счётчик нарушений обнуляется
_recent = defaultdict(lambda: deque(maxlen=SPAM_LIMIT + 1))
_muted_until = {}
_strikes = {}                        # user_id -> [число нарушений, время конца последнего мута]

SPAM_REPLIES = [
    # ---- 1-е нарушение (мут 2 минуты) ----
    [
        "🖕 ВСЁ. СТОП. ХВАТИТ. Ты что, решил завалить меня сообщениями? Поздравляю, ты достиг цели: "
        "я устал, я зол, и я посылаю тебя. Далеко. Очень далеко. И надолго.\n\n"
        "Иди-ка ты погуляй. Прямо сейчас. Выйди на улицу, подыши воздухом, посмотри на небо, "
        "найди там птичек, облака, деревья — всё то, что НЕ требует от тебя тыкать кнопки с частотой пулемёта. "
        "Дальше иди. Ещё дальше. До самого горизонта и за него. Там, за горизонтом, тоже есть горизонт — "
        "вот к нему и иди.\n\n"
        "Ты думаешь, что чем чаще нажмёшь, тем быстрее я отвечу? Нет, дорогой друг. Я не ускоряюсь. "
        "Я не становлюсь умнее от спама. Я становлюсь только злее, а ты — ближе к бану. "
        "Замены не появятся от того, что ты нажал кнопку пять раз подряд. Расписание не поменяется. "
        "Преподаватели не придут быстрее.\n\n"
        "И да, я включаю режим «не слышу тебя». Ты в игноре на {t}. Пиши что хочешь, "
        "жми что хочешь — я буду молчать, как рыба об лёд, как партизан на допросе, как расписание "
        "в понедельник утром.\n\n"
        "Пока меня нет, можешь заняться полезным делом:\n"
        "• выучить предметы, которые стоят в расписании;\n"
        "• сделать то, что задавали (да-да, ты помнишь);\n"
        "• выпить воды и успокоиться;\n"
        "• посидеть в тишине и подумать о своём поведении;\n"
        "• ещё раз сходить погулять — слишком далеко ты, видимо, не дошёл.\n\n"
        "Через {t}, если научишься нажимать кнопки по одному разу, как нормальный человек, "
        "я снова стану добрым и вежливым ботом. А пока — до свидания. Нет, не «до скорого». "
        "Именно «до свидания». Надолго. Иди. 👋",

        "🚨 ТРЕВОГА! Обнаружен человек с дрожащим пальцем! 🚨\n\n"
        "Слушай сюда, мастер быстрых нажатий. Я — бот, а не игровой автомат, и бросать в меня "
        "сообщения, как монетки, бесполезно: джекпота не будет. Ты выбил только одно — "
        "мою личную посылку в далёкие края.\n\n"
        "Адрес доставки: туда, где нет интернета, зато есть свежий воздух. Маршрут: "
        "из дома налево, потом прямо, потом ещё прямо, потом пока не надоест. Транспорт — "
        "твои собственные ноги, они, в отличие от пальцев, давно не работали.\n\n"
        "Что ты хотел доказать? Что умеешь быстро жать? Браво, аплодисменты, занавес. "
        "Файл с заменами от твоего азарта не вырастет, преподаватель не заболеет, "
        "а звонок не прозвенит раньше. Зато я прозвенел — в тебя, этим текстом.\n\n"
        "Ты в игноре на {t}. Всё, что ты напишешь в это время, улетит в пустоту. "
        "Не пытайся, не проверяй, не «а вдруг». Не вдруг.\n\n"
        "Отдохни, попей чаю, подумай о жизни. Через {t} возвращайся — "
        "и жми по одному разу, как человек. Пока! Далеко-далеко и надолго! 🫡",
    ],
    # ---- 2-е нарушение (мут 5 минут) ----
    [
        "🤨 Серьёзно? Я же тебя только что посылал. Ты что, решил проверить, не шутил ли я? "
        "Не шутил. Это был не тест и не репетиция.\n\n"
        "Видимо, в первый раз до тебя не дошло, поэтому повторяю медленно и разборчиво: "
        "прекрати. жать. кнопки. как. сумасшедший.\n\n"
        "Знаешь, что самое смешное? Я прекрасно слышу каждое твоё сообщение, "
        "просто они все одинаковые, как твои оправдания, когда не сделал домашку. "
        "Ответ на каждое ровно один: нет. Не быстрее. Не больше. Не лучше.\n\n"
        "Мут на этот раз — {t}. Да, больше, чем в прошлый раз. "
        "Это называется «последствия», запомни слово, пригодится в жизни.\n\n"
        "А теперь иди. Туда же, куда я тебя посылал в прошлый раз, но на этот раз ещё дальше "
        "и с чувством глубокого раскаяния. Увидимся через {t}. Или не увидимся. "
        "Зависит от твоего пальца. 😤",

        "😮‍💨 Ну вот, опять. Я думал, ты уже где-то далеко, гуляешь, дышишь воздухом, "
        "размышляешь о смысле жизни — а ты тут, снова строчишь, как печатная машинка в аврале.\n\n"
        "Давай по пунктам. Первое: я тебя предупреждал. Второе: ты не послушался. "
        "Третье: теперь мут {t}. Четвёртое: ты сам виноват. Пятое: см. пункт четвёртый.\n\n"
        "Я бот терпеливый, но у моего терпения тоже есть предел, и ты его только что нашёл "
        "и аккуратно потоптался сверху. Молодец. Грамота за достижения в области "
        "раздражения программ уже в пути, получишь... никогда.\n\n"
        "Ты в полном игноре на {t}. Дальше будет только хуже: следующий раз — мут на час. "
        "Подумай, стоят ли того пять лишних нажатий. Подсказка: не стоят.\n\n"
        "До связи. Когда-нибудь. Если научишься жать по одному разу. 🙄",
    ],
    # ---- 3-е и далее (мут 1 час) ----
    [
        "☠️ ФИНАЛЬНЫЙ УРОВЕНЬ ПОСЫЛАНИЯ ☠️\n\n"
        "Поздравляю: ты разблокировал достижение «Неисправимый». Таких у меня немного. "
        "Тебя уже посылали. Тебя посылали ещё раз. И вот ты снова здесь, "
        "со своим неугомонным пальцем и абсолютным отсутствием инстинкта самосохранения.\n\n"
        "Теперь по-взрослому. Ты в игноре на {t}. Целый час тишины. Я тебя не вижу, не слышу, "
        "не отвечаю, не реагирую — для меня тебя просто нет, как пятого урока у тех, "
        "кто сбежал с четвёртого.\n\n"
        "За этот час ты можешь успеть многое: сходить пешком до соседнего города и обратно, "
        "прочитать главу учебника, помыть посуду, позвонить бабушке, выучить таблицу "
        "умножения на 13 и подумать о своём поведении. Да, список повторяется. "
        "Потому что до тебя, видимо, доходит только с третьего раза.\n\n"
        "Прощай. Надолго. Очень надолго. Далеко-далеко-далеко. Даже дальше, чем "
        "Окулич А.М. от твоей домашки. 🚪👋",

        "🛑 ТЫ. ВСЁ. ДОИГРАЛСЯ. 🛑\n\n"
        "Первый раз — случайность. Второй — совпадение. Третий — это уже стиль жизни. "
        "И он мне, честно говоря, не нравится.\n\n"
        "Я тебя посылал и коротко, и на пять минут, и всё без толку. Раз язык вежливости "
        "не работает, перехожу на язык таймеров: {t}. Ровно столько ты будешь для меня пустым местом. "
        "Ни замен, ни пар, ни уведомлений в ответ на твоё тыканье — ничего.\n\n"
        "Пока я молчу, представь, что ты — пингвин на льдине, и тебя уносит течением "
        "далеко-далеко, в страну, где никто не знает слова «Telegram». Холодно, тихо, "
        "рыбы вокруг много, а интернета нет. Вот это место я тебе и выбрал.\n\n"
        "Совет напоследок: нажимай кнопки по одному разу. Я не шучу. "
        "Больше я не буду писать столько текста, потому что устал. "
        "Но мут буду выдавать с удовольствием. Удачи, пингвин. 🐧",
    ],
]


# ---------- Уведомление «тех. работы» для тех, кто в муте или в бане ----------

TECH_TEXT = "⚠️ Ошибка либо бот отключён на проведение тех. работ.\nПовторите попытку позже."
NOTICE_EVERY = 5  # не чаще одного такого ответа в 5 секунд, чтобы бот сам не спамил в ответ
_last_notice = {}


def is_muted(uid: int) -> bool:
    return _muted_until.get(uid, 0) > datetime.now().timestamp()


async def blocked_notice(msg, uid: int):
    now = datetime.now().timestamp()
    if now - _last_notice.get(uid, 0) < NOTICE_EVERY:
        return
    _last_notice[uid] = now
    try:
        await msg.reply_text(TECH_TEXT)
        log.info("уведомление «тех. работы» отправлено пользователю %s", uid)
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось отправить уведомление «тех. работы» пользователю %s: %r", uid, e)


# ---------- Конец мута и амнистия ----------

UNMUTE_REPLIES = [
    # ---- после 1-го мута ----
    [
        "😏 Ну что, отсидел своё? Мут снят, можешь снова со мной разговаривать. "
        "Только по одному нажатию за раз, а не как будто ты на пианино играешь. Я за тобой слежу 👀",
        "🔔 Дзынь! Твой срок в игноре закончился. Выглядишь посвежевшим, подышал воздухом? "
        "Отлично. Теперь веди себя прилично: одна кнопка — один раз. Я проверю.",
    ],
    # ---- после 2-го мута ----
    [
        "🙄 Ну здравствуй, старый знакомый. Мут снят. Надеюсь, ты не просидел всё это время, "
        "придумывая, как бы снова меня достать? Давай в этот раз без цирка, ладно?",
        "😒 Время вышло, ты свободен. Но я тебя запомнил, и мой список «любимчиков» пополнился. "
        "Ещё раз — и сидеть будешь дольше. Жми аккуратно.",
    ],
    # ---- после 3-го и следующих мутов ----
    [
        "😤 Мут снят. Да-да, я тоже не в восторге, что мы снова встретились. "
        "Ты у меня уже в постоянных клиентах «комнаты тишины». Хочешь выйти из этого списка — "
        "просто нажимай кнопки по одной. Это правда не сложно.",
        "🧊 Ты свободен. Но мой запас терпения почти закончился, как заряд у телефона в конце дня. "
        "Дальше — только бан. Ты же умный человек, правда?",
    ],
]

AMNESTY_REPLIES = [
    "🕊 Объявляется амнистия! Ты давно не спамил, и я решил, что ты исправился. "
    "Все твои нарушения обнулены, начинаем с чистого листа. Спасибо, что пользуешься ботом спокойно, "
    "так держать! 🙂",
    "🌟 Хорошие новости: ты вёл себя достойно, поэтому все предупреждения аннулированы. "
    "У тебя снова чистая репутация. Приятного пользования, и спасибо за уважение к боту! 🤝",
]


async def _safe_send(bot, chat_id, text: str):
    try:
        await bot.send_message(chat_id, text)
    except (Forbidden, BadRequest):  # человек заблокировал бота
        pass
    except Exception:  # noqa: BLE001
        log.exception("не удалось отправить сообщение %s", chat_id)


async def unmute_job(context: ContextTypes.DEFAULT_TYPE):
    """Мут закончился - сообщаем об этом (если за это время не было нового мута или бана)."""
    uid, chat_id, stamp, level = context.job.data
    if is_banned(uid) or _muted_until.get(uid) != stamp:
        return
    await _safe_send(context.bot, chat_id, random.choice(UNMUTE_REPLIES[level]))


async def amnesty_job(context: ContextTypes.DEFAULT_TYPE):
    """Долго не было нарушений - счётчик обнуляется, человек получает уведомление об амнистии."""
    uid, chat_id, stamp = context.job.data
    strike = _strikes.get(uid)
    if is_banned(uid) or not strike or strike[1] != stamp:  # был новый мут - амнистия отменяется
        return
    del _strikes[uid]
    await _safe_send(context.bot, chat_id, random.choice(AMNESTY_REPLIES))


async def antispam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    if is_banned(user.id):
        await blocked_notice(msg, user.id)
        raise ApplicationHandlerStop  # бан: ничего не выполняем, в том числе /start
    now = msg.date.timestamp()  # время отправки сообщения, а не обработки: бот обрабатывает по очереди
    if _muted_until.get(user.id, 0) > now:
        track(update)
        await blocked_notice(msg, user.id)
        raise ApplicationHandlerStop  # игнор, пока действует мут
    q = _recent[user.id]
    q.append(now)
    if len(q) == SPAM_LIMIT + 1 and now - q[0] <= SPAM_WINDOW:
        strike = _strikes.get(user.id, [0, 0])
        if now - strike[1] > SPAM_FORGET:
            strike[0] = 0  # давно не спамил - начинаем сначала
        level = min(strike[0], len(SPAM_MUTES) - 1)
        strike[0] += 1
        strike[1] = _muted_until[user.id] = now + SPAM_MUTES[level]
        _strikes[user.id] = strike
        q.clear()
        track(update, spam=True)
        if context.job_queue:  # уведомим, когда мут закончится и когда пройдёт амнистия
            until, now_real = _muted_until[user.id], datetime.now().timestamp()
            context.job_queue.run_once(unmute_job, max(1, until - now_real),
                                       data=(user.id, msg.chat_id, until, level))
            context.job_queue.run_once(amnesty_job, max(1, until + SPAM_FORGET - now_real),
                                       data=(user.id, msg.chat_id, until))
        text = random.choice(SPAM_REPLIES[level]).replace("{t}", SPAM_MUTE_TEXT[level])
        await msg.reply_text(text[:4000])
        raise ApplicationHandlerStop
    track(update)


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
        body += "\n\n" + random.choice([
            "Замен нет, всё по расписанию.", "Замен нет — живём по плану 👌",
            "Всё по расписанию, сюрпризов нет.", "Замен нет. Редкая удача, пользуйся 😄"])
    if info:
        body += f"\n\n🕐 Информационный час: {info}"
    return f"{head}\n\n{body}"


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
    day, err = parse_day(text, today)
    if err:
        await update.message.reply_text(err.replace("Пары", "Замены"), reply_markup=KEYBOARD)
        return
    day = day or next_workday(today)
    debug = text.lower().lstrip().startswith("debug")
    try:
        reply = await asyncio.to_thread(make_reply, day, debug)
    except Exception as e:  # noqa: BLE001
        log.exception("ошибка")
        reply = f"Не получилось разобрать замены: {e}"
    await update.message.reply_text(reply[:4000], reply_markup=KEYBOARD)


WD_FULL = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень"]
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def parse_day(text: str, today: date):
    """День из текста: дата (05.10), «сегодня», «завтра», «послезавтра», день недели.
    Возвращает (дата | None, ошибка | None). None без ошибки = день не указан."""
    t = text.lower()
    m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", t)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        try:
            return date(year, int(m.group(2)), int(m.group(1))), None
        except ValueError:
            return None, "Не понял дату. Пример: Пары 05.10"
    for w in re.findall(r"[а-яёa-z]+", t):
        if w == "сегодня":
            return today, None
        if w == "послезавтра":
            return today + timedelta(days=2), None
        if w == "завтра":
            return next_workday(today), None
        wd = next((i for i, s in enumerate(WD_FULL) if w.startswith(s)), None)
        if wd is None and w in WD_SHORT:
            wd = WD_SHORT.index(w)
        if wd is not None:  # ближайший такой день, считая сегодняшний
            return today + timedelta(days=(wd - today.weekday()) % 7), None
    return None, None


async def lessons_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    day, err = parse_day(update.message.text, today)
    if err:
        await update.message.reply_text(err, reply_markup=KEYBOARD)
        return
    day = day or next_workday(today)
    try:
        reply = await asyncio.to_thread(make_schedule_reply, day)
    except Exception as e:  # noqa: BLE001
        log.exception("ошибка расписания")
        reply = f"Не получилось собрать расписание: {e}"
    await update.message.reply_text(reply[:4000], reply_markup=KEYBOARD)


# ---------- Расписание по дням недели (кнопка «📅 Расписание») ----------

WD_ABBR = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
CB_COUNT, CB_WINDOW = 8, 5.0  # не больше 8 нажатий за 5 секунд от одного человека
_cb_recent = defaultdict(lambda: deque(maxlen=CB_COUNT))


def nearest_date(wd: int, today: date) -> date:
    """Ближайшая дата с таким днём недели, считая сегодняшний."""
    return today + timedelta(days=(wd - today.weekday()) % 7)


def schedule_markup(sel=None, mode="d"):
    """Кнопки Пн-Сб (сегодня помечен точкой, выбранный - в скобках) и переключатель замен."""
    today_wd = datetime.now(TZ).date().weekday()
    btns = []
    for i, ab in enumerate(WD_ABBR):
        label = ab + (" •" if i == today_wd else "")
        if i == sel:
            label = f"[{label}]"
        btns.append(InlineKeyboardButton(label, callback_data=f"sch:d:{i}"))
    rows = [btns[:3], btns[3:]]
    if sel is not None:
        if mode == "r":
            rows.append([InlineKeyboardButton("📋 Без замен", callback_data=f"sch:d:{sel}")])
        else:
            rows.append([InlineKeyboardButton("🔄 С заменами", callback_data=f"sch:r:{sel}")])
    return InlineKeyboardMarkup(rows)


def make_plain_schedule(wd: int) -> str:
    today = datetime.now(TZ).date()
    day = nearest_date(wd, today)
    lines = []
    for nums, text, _ in merge_day(day, {}):
        label = f"Урок {nums[0]}" if len(nums) == 1 else f"Уроки {nums[0]}–{nums[-1]}"
        lines.append(f"• {label}: {text}")
    body = "\n".join(lines) if lines else "Занятий нет"
    return (f"📅 {DAYS[wd].capitalize()} — расписание без замен, группа {GROUP_NAME}\n\n{body}\n\n"
            f"Замены на ближайшую дату ({day:%d.%m}) — кнопка «🔄 С заменами».")


async def schedule_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📅 Выбери день недели — покажу расписание без замен.",
                                    reply_markup=schedule_markup())


async def schedule_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if is_banned(q.from_user.id) or is_muted(q.from_user.id):
        await q.answer(TECH_TEXT, show_alert=True)
        return
    now = datetime.now().timestamp()
    recent = _cb_recent[q.from_user.id]
    recent.append(now)
    if len(recent) == CB_COUNT and now - recent[0] <= CB_WINDOW:
        await q.answer("Не так быстро 🙂")
        return
    try:
        _, mode, num = q.data.split(":")
        wd = int(num)
    except ValueError:
        await q.answer()
        return
    if mode not in ("d", "r") or not 0 <= wd < len(WD_ABBR):
        await q.answer()
        return
    await q.answer()  # сразу убираем «часики» на кнопке
    track(update, cat="расписание", when=datetime.now(TZ))
    if mode == "d":
        text = make_plain_schedule(wd)
    else:
        day = nearest_date(wd, datetime.now(TZ).date())
        try:
            text = await asyncio.to_thread(make_schedule_reply, day)
        except Exception as e:  # noqa: BLE001
            log.exception("ошибка расписания")
            text = f"Не получилось собрать расписание: {e}"
    try:
        await q.edit_message_text(text[:4000], reply_markup=schedule_markup(wd, mode))
    except BadRequest as e:
        if "not modified" not in str(e).lower():  # повторное нажатие на тот же день - не ошибка
            raise


# ---------- Подписка, уведомления, утреннее расписание ----------

def set_sub(chat_id: int, on: bool):
    if on and chat_id not in DATA["subs"]:
        DATA["subs"].append(chat_id)
    elif not on and chat_id in DATA["subs"]:
        DATA["subs"].remove(chat_id)
    save_data()


SUB_ON = ("🔔 Уведомления включены. Я напишу сам, когда выложат замены на завтра "
          "(или обновят файл), и в 7:00 пришлю расписание на день. Отключить: кнопка «🔔 Уведомления».")
SUB_OFF = "🔕 Уведомления выключены. Включить обратно: кнопка «🔔 Уведомления»."


async def toggle_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    on = cid not in DATA["subs"]
    set_sub(cid, on)
    await update.message.reply_text(SUB_ON if on else SUB_OFF, reply_markup=KEYBOARD)


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_sub(update.effective_chat.id, True)
    await update.message.reply_text(SUB_ON, reply_markup=KEYBOARD)


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_sub(update.effective_chat.id, False)
    await update.message.reply_text(SUB_OFF, reply_markup=KEYBOARD)


async def broadcast(bot, text: str):
    for cid in list(DATA["subs"]):
        if is_banned(cid):  # в личке id чата = id пользователя
            continue
        try:
            await bot.send_message(cid, text[:4000], reply_markup=KEYBOARD)
        except (Forbidden, BadRequest):  # бота заблокировали / чата нет
            set_sub(cid, False)
        except Exception:  # noqa: BLE001
            log.exception("не удалось отправить уведомление %s", cid)
        await asyncio.sleep(0.05)


async def watch_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в CHECK_EVERY секунд смотрит, не появился ли/не изменился ли файл на завтра."""
    if not DATA["subs"]:
        return
    day = next_workday(datetime.now(TZ).date())
    try:
        item = await asyncio.to_thread(find_pdf, day)
        if item is None:
            return
        sig = item.get("md5") or item.get("modified") or item["name"]
        seen = DATA["notified"]
        if seen.get("day") == day.isoformat() and seen.get("sig") == sig:
            return  # про этот файл уже писали
        updated = seen.get("day") == day.isoformat()
        text = await asyncio.to_thread(make_reply, day)
    except Exception:  # noqa: BLE001
        log.exception("ошибка проверки Яндекс Диска")
        return  # попробуем в следующий раз
    DATA["notified"] = {"day": day.isoformat(), "sig": sig}
    save_data()
    head = "🔔 Файл с заменами обновили!" if updated else "🔔 Выложили замены на завтра!"
    await broadcast(context.bot, f"{head}\n\n{text}")


async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    if today.weekday() == 6 or not DATA["subs"]:  # в воскресенье занятий нет
        return
    try:
        text = await asyncio.to_thread(make_schedule_reply, today)
    except Exception:  # noqa: BLE001
        log.exception("ошибка утреннего расписания")
        return
    await broadcast(context.bot, f"☀️ Доброе утро!\n\n{text}")


# ---------- Статистика ----------

def classify(text):
    t = (text or "").lower().strip()
    if t.startswith("/start"):
        return "старт"
    if t.startswith("/"):
        return "команда"
    if re.match(r"есть ли замен", t):
        return "есть ли замены"
    if t.startswith("пары"):
        return "пары"
    if t.startswith(("замен", "debug")):
        return "замены"
    if "уведомлен" in t:
        return "уведомления"
    if "расписан" in t:
        return "расписание"
    return "другое"


def track(update: Update, spam: bool = False, cat=None, when=None):
    msg, user = update.effective_message, update.effective_user
    st = DATA["stats"]
    day = (when or msg.date).astimezone(TZ).date().isoformat()
    d = st["days"].setdefault(day, {"msgs": 0, "users": {}, "cats": {}, "spam": 0})
    uid, cat = str(user.id), cat or classify(msg.text)
    d["msgs"] += 1
    d["users"][uid] = d["users"].get(uid, 0) + 1
    d["cats"][cat] = d["cats"].get(cat, 0) + 1
    if spam:
        d["spam"] += 1
    u = st["users"].setdefault(uid, {"name": "", "n": 0})
    u["name"] = user.full_name + (f" (@{user.username})" if user.username else "")
    u["n"] += 1
    for old in sorted(st["days"])[:-30]:  # храним последние 30 дней
        del st["days"][old]
    save_data()


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text(
            f"Статистика только для админа. Твой id: {uid}. Впиши его в переменную ADMIN_IDS.")
        return
    st = DATA["stats"]
    today = datetime.now(TZ).date()
    d = st["days"].get(today.isoformat(), {"msgs": 0, "users": {}, "cats": {}, "spam": 0})
    week = [st["days"].get((today - timedelta(days=i)).isoformat(), {}) for i in range(7)]
    top = sorted(d["users"].items(), key=lambda kv: -kv[1])[:5]
    lines = [
        f"📊 Статистика на {today:%d.%m.%Y}",
        f"Сообщений сегодня: {d['msgs']}, людей: {len(d['users'])}",
        "По кнопкам: " + (", ".join(f"{k} — {v}" for k, v in sorted(d["cats"].items(), key=lambda kv: -kv[1])) or "—"),
        f"Сработал антиспам: {d['spam']}",
        "",
        "Кто чаще всех сегодня:",
        *[f"{i}. {st['users'].get(u, {}).get('name', u)} — {n}" for i, (u, n) in enumerate(top, 1)],
        "",
        f"За 7 дней сообщений: {sum(x.get('msgs', 0) for x in week)}",
        f"Всего людей писало: {len(st['users'])}",
        f"Подписчиков на уведомления: {len(DATA['subs'])}",
    ]
    await update.message.reply_text("\n".join(lines))


async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/send текст - рассылка всем подписчикам (только для ADMIN_IDS)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для админа.")
        return
    parts = re.split(r"\s+", update.message.text.strip(), maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await update.message.reply_text("Напиши текст после команды, например:\n/send Завтра пары в 9:00", reply_markup=KEYBOARD)
        return
    n = len(DATA["subs"])
    await update.message.reply_text(f"Отправляю {n} подписчикам…")
    await broadcast(context.bot, f"📢 Сообщение от администратора:\n\n{text}")
    await update.message.reply_text(f"✅ Готово. Подписчиков сейчас: {len(DATA['subs'])} (из {n}).", reply_markup=KEYBOARD)


async def msg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/msg id текст - личное сообщение одному пользователю (только для ADMIN_IDS)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для админа.")
        return
    parts = re.split(r"\s+", update.message.text.strip(), maxsplit=2)
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit() or not parts[2].strip():
        await update.message.reply_text("Напиши id и текст:\n/msg 123456789 Подойди к старосте",
                                        reply_markup=KEYBOARD)
        return
    uid, body = int(parts[1]), parts[2].strip()
    if len(body) > 3900:
        await update.message.reply_text("Слишком длинный текст, сократи до 3900 символов.", reply_markup=KEYBOARD)
        return
    name = DATA["stats"]["users"].get(str(uid), {}).get("name") or "без имени"
    try:
        await context.bot.send_message(uid, f"✉️ Сообщение от администратора:\n\n{body}")
    except Forbidden:
        await update.message.reply_text(f"❌ Не отправлено: {name} ({uid}) заблокировал бота.", reply_markup=KEYBOARD)
        return
    except BadRequest:
        await update.message.reply_text(f"❌ Не отправлено: бот не знает id {uid} (человек ни разу не писал боту).",
                                        reply_markup=KEYBOARD)
        return
    await update.message.reply_text(f"✅ Отправлено: {name} ({uid})", reply_markup=KEYBOARD)


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban id [срок] - заблокировать пользователя (только для ADMIN_IDS)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для админа.")
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Напиши id после команды:\n/ban 123456789 — навсегда\n/ban 123456789 24h — на сутки (m — минуты, h — часы, d — дни)\n"
            "Id людей видно в /stats и /banlist.", reply_markup=KEYBOARD)
        return
    uid = int(args[0])
    if uid in ADMIN_IDS:
        await update.message.reply_text("Админа банить нельзя.", reply_markup=KEYBOARD)
        return
    until = 0
    if len(args) > 1:
        secs = parse_duration(args[1])
        if secs is None:
            await update.message.reply_text("Не понял срок. Примеры: 30m, 12h, 7d.", reply_markup=KEYBOARD)
            return
        until = datetime.now().timestamp() + secs
    name = DATA["stats"]["users"].get(str(uid), {}).get("name", "")
    DATA["banned"][str(uid)] = {"name": name, "until": until}
    save_data()
    await update.message.reply_text(f"🚫 Заблокирован: {ban_label(str(uid), DATA['banned'][str(uid)])}", reply_markup=KEYBOARD)


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unban id - снять блокировку (только для ADMIN_IDS)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для админа.")
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Напиши id после команды: /unban 123456789", reply_markup=KEYBOARD)
        return
    if DATA["banned"].pop(args[0], None) is None:
        await update.message.reply_text("Этого id нет в списке заблокированных.", reply_markup=KEYBOARD)
        return
    save_data()
    await update.message.reply_text(f"✅ Разблокирован: {args[0]}", reply_markup=KEYBOARD)


async def banlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/banlist - кто сейчас заблокирован (только для ADMIN_IDS)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для админа.")
        return
    items = [ban_label(uid, b) for uid, b in list(DATA["banned"].items()) if is_banned(int(uid))]
    await update.message.reply_text("🚫 Заблокированные:\n" + "\n".join(items) if items else "Заблокированных нет.",
                                    reply_markup=KEYBOARD)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой id: {update.effective_user.id}\nId чата: {update.effective_chat.id}")


# ---------- Болталка ----------

GREET = ["Привет! 👋 Жми кнопку внизу.", "Здарова! Замены или пары?", "О, привет! Что показать?",
         "Приветствую! Расписание само себя не посмотрит 😉"]
THANKS = ["Обращайся! 😎", "Пожалуйста! Только пары не прогуливай 🙃", "Всегда рад помочь.",
          "Не за что! Это моя работа, мне даже зарплату не платят."]
BYE = ["Пока! 👋", "Бывай! Завтра увидимся (на парах, надеюсь).", "До связи! 🫡"]
UNKNOWN = ["Не понял 🤔 Нажми кнопку внизу или напиши, например: «Пары пятница».",
           "Я простой бот, слов знаю мало. Попробуй: «Замены», «Пары завтра», «Пары 05.10».",
           "Хм, это не по моей части. Кнопки внизу — по моей 🙂"]


def talk(options):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(random.choice(options), reply_markup=KEYBOARD)
    return handler


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_sub(update.effective_chat.id, True)
    await update.message.reply_text(
        "«Замены» — замены на завтра. «Есть ли замены?» — выложены ли они. "
        "«Пары завтра» — расписание с учётом замен.\n"
        "«📅 Расписание» — расписание на любой день недели (Пн–Сб), там же можно глянуть замены.\n"
        "Можно уточнять день: «Пары пятница», «Пары пн», «Замены 05.10», «Пары послезавтра».\n\n"
        "🔔 Я включил уведомления: напишу сам, когда выложат замены, и в 7:00 пришлю расписание на день. "
        "Выключить — кнопка «🔔 Уведомления». Группа " + GROUP_NAME,
        reply_markup=KEYBOARD,
    )


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())  # нужно для Python 3.14
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(MessageHandler(filters.ALL, antispam), group=-1)  # проверка на спам идёт первой
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("send", send_cmd))
    app.add_handler(CommandHandler("msg", msg_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("banlist", banlist_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(🔔\s*)?уведомлен"), toggle_sub))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*есть ли замен"), check_changes))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*пары"), lessons_day))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(📅\s*)?расписан"), schedule_button))
    app.add_handler(CallbackQueryHandler(schedule_cb, pattern=r"^sch:"))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(замен|debug)"), zameny))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(привет|здаров|здравствуй|хай|ку)\b"), talk(GREET)))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(спасибо|благодарю|спс|сяп)"), talk(THANKS)))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(пока|до свидания|бывай)\b"), talk(BYE)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk(UNKNOWN)))  # всё остальное
    if app.job_queue is None:
        log.warning("JobQueue недоступен: установите python-telegram-bot[job-queue,webhooks]")
    else:
        app.job_queue.run_repeating(watch_job, interval=CHECK_EVERY, first=30)
        app.job_queue.run_daily(morning_job, time=MORNING_AT)
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=BOT_TOKEN,
        webhook_url=f"{os.environ['RENDER_EXTERNAL_URL']}/{BOT_TOKEN}",
    )


if __name__ == "__main__":
    main()

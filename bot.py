"""Telegram-бот: замены для группы 01-24 из PDF на Яндекс Диске.

Команды в чате:
  Замены            - замены на завтра
  Замены 05.10      - замены на конкретную дату (или день недели: Замены пятница)
  Пары завтра       - расписание с заменами; можно "Пары пн", "Пары 05.10", "Пары послезавтра"
  Расписание        - кнопки Пн-Сб: расписание дня без замен, оттуда же можно открыть замены
  Уведомления       - настройки: замены (вкл/выкл), расписание на день (по умолчанию 7:00) и расписание на завтра
                      (по умолчанию 20:00): у обоих расписаний вкл/выкл и своё время; там же кнопка «Помощь»
  /stats            - статистика (только для ADMIN_IDS)
  /send текст       - рассылка всем подписчикам (только для ADMIN_IDS)
  /msg id текст     - личное сообщение одному пользователю (только для ADMIN_IDS)
  /ban id [срок]    - заблокировать пользователя, срок: 30m, 12h, 7d (без срока - навсегда)
  /unban id         - разблокировать
  /banlist          - список заблокированных
  /admin            - панель админа с кнопками (создать ключ, список, вкл/выкл проверки) (только для ADMIN_IDS)
  /key [срок] [кол-во] - создать одноразовый ключ: /key, /key 7d, /key 30d 10 (только для ADMIN_IDS)
  /grantall срок [notify] - выдать доступ СРАЗУ ВСЕМ, кто пользовался ботом (без ключей): /grantall навсегда, /grantall 30d
  /keys             - список ключей
  /users [no]       - список всех пользователей бота (no - только без доступа)
  /delkey КЛЮЧ      - удалить ключ;  /revoke id - забрать доступ у человека
  /access on|off    - включить/выключить проверку ключа (on all - сбросить доступ даже у тех, кто вводил ключ)
  /backup           - прислать файл с данными; файл с подписью /restore - вернуть данные
  /unbankey [кол-во] - ключ разбана: снимает бан, блокировку за частый спам или мут (только для ADMIN_IDS)
  /mykey            - свой статус доступа
  debug             - как бот разобрал файл (для проверки)
"""
import asyncio
import io
import json
import logging
import os
import random
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pdfplumber
import requests
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove,
                      Update)
from telegram.error import BadRequest, Forbidden
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

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

# ---------- Данные (подписчики, статистика, ключи) ----------
# Хранятся в JSON-файле. На бесплатном Render диск стирается при перезапуске, поэтому
# данные дополнительно (и автоматически) дублируются в приватный GitHub Gist, если заданы
# переменные GITHUB_TOKEN и GIST_ID. Подписчиков можно продублировать и в CHAT_IDS (id через запятую).

DATA_FILE = os.environ.get("DATA_FILE", "bot_data.json")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
             if x.lstrip("-").isdigit()}
CHECK_FAST = 60                       # частая проверка (сек): пн-сб в рабочие часы
CHECK_SLOW = 300                      # обычная проверка (сек): в остальное время
FAST_FROM = dtime(8, 0)               # частая проверка с ...
FAST_TO = dtime(16, 0)                # ... до (не включая)
ACCESS_REQUIRED = os.environ.get("ACCESS_REQUIRED", "").lower() in ("1", "true", "on", "yes")  # ключ нужен с самого начала
_last_check = 0.0                     # когда проверяли в последний раз (monotonic)
MORNING_DEFAULT = "07:00"            # когда присылать расписание на день, если человек не выбрал своё время
EVENING_DEFAULT = "20:00"            # когда присылать расписание на завтра по умолчанию
MORNING_GRACE = timedelta(minutes=60)  # если бот проспал нужную минуту, догоняем не позже чем через столько


def new_pref(morning: bool, hhmm: str = MORNING_DEFAULT, evening: bool = False, ehhmm: str = EVENING_DEFAULT) -> dict:
    """Настройки расписаний: на день (morning) и на завтра (evening). Если время сегодня уже прошло, первая отправка будет завтра."""
    now = datetime.now(TZ)
    hm, today = now.time().strftime("%H:%M"), now.date().isoformat()
    return {"morning": morning, "time": hhmm, "last": today if hm >= hhmm else "",
            "evening": evening, "etime": ehhmm, "elast": today if hm >= ehhmm else ""}


def fix_data(d):
    """Добавляет недостающие поля и переносит старые форматы."""
    d.setdefault("subs", [])
    d.setdefault("notified", {})
    d.setdefault("prefs", {})  # chat_id -> {"morning"/"time"/"last": расписание на день, "evening"/"etime"/"elast": расписание на завтра}
    d.setdefault("stats", {"users": {}, "days": {}})
    d.setdefault("banned", {})  # user_id -> {"name": ..., "until": время конца бана или 0 = навсегда}
    acc = d.setdefault("access", {})
    acc.setdefault("required", ACCESS_REQUIRED)  # нужен ли ключ доступа
    acc.setdefault("allowed", {})                # id -> {"until": конец доступа (0 = навсегда), "granted", "warned"}
    acc.setdefault("keys", {})                   # ключ -> {created, dur, max_uses, uses, users}
    if isinstance(acc["allowed"], list):         # старый формат: просто список id
        acc["allowed"] = {str(u): {"until": 0, "granted": 0, "warned": False} for u in acc["allowed"]}
    for k in acc["keys"].values():
        k.setdefault("dur", 0)
        k.setdefault("kind", "access")  # access - даёт доступ, unban - снимает бан
    for uid in os.environ.get("ACCESS_IDS", "").replace(" ", "").split(","):  # запасной список (диск мог стереться)
        if uid.lstrip("-").isdigit():
            acc["allowed"].setdefault(str(int(uid)), {"until": 0, "granted": 0, "warned": False})
    for cid in os.environ.get("CHAT_IDS", "").replace(" ", "").split(","):
        if cid.lstrip("-").isdigit() and int(cid) not in d["subs"]:
            d["subs"].append(int(cid))
    for cid in d["subs"]:  # старые подписчики: расписание на день у них было включено в 7:00
        d["prefs"].setdefault(str(cid), new_pref(True))
    for pr in d["prefs"].values():  # расписание на завтра появилось позже: у старых пользователей оно выключено
        pr.setdefault("evening", False)
        pr.setdefault("etime", EVENING_DEFAULT)
        pr.setdefault("elast", "")
    return d


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GIST_ID = os.environ.get("GIST_ID", "").strip()
GIST_FILE = "bot_data.json"
GIST_URL = f"https://api.github.com/gists/{GIST_ID}"
GIST_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
_gist_ready = False                  # True, если Gist удалось прочитать (только тогда в него пишем)
_gist_text = [None]                  # последний снимок данных для отправки
_gist_event = threading.Event()
_gist_thread = None


def gist_load():
    """Данные из Gist: dict ({} если файл пуст) или None, если прочитать не вышло (тогда Gist не трогаем)."""
    if not (GITHUB_TOKEN and GIST_ID):
        return None
    for _ in range(3):
        try:
            r = requests.get(GIST_URL, headers=GIST_HEADERS, timeout=15)
            r.raise_for_status()
            f = r.json().get("files", {}).get(GIST_FILE)
            if not f:
                return {}
            text = f.get("content") or ""
            if f.get("truncated"):
                text = requests.get(f["raw_url"], headers=GIST_HEADERS, timeout=15).text
            d = json.loads(text) if text.strip() else {}
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            log.exception("не получилось прочитать Gist")
        time.sleep(2)
    return None


def _gist_worker():
    while True:
        _gist_event.wait()
        time.sleep(2)            # собираем частые изменения в одну отправку
        _gist_event.clear()
        try:
            r = requests.patch(GIST_URL, headers=GIST_HEADERS, timeout=20,
                               json={"files": {GIST_FILE: {"content": _gist_text[0]}}})
            r.raise_for_status()
        except Exception:  # noqa: BLE001
            log.exception("не получилось сохранить копию в Gist, повторю позже")
            _gist_event.set()
            time.sleep(30)


def load_data():
    global _gist_ready
    d = gist_load()
    if d is not None:
        _gist_ready = True
        log.info("Данные читаю из Gist, автосохранение в Gist включено")
    if not d:
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
    if not (GITHUB_TOKEN and GIST_ID):
        log.warning("GITHUB_TOKEN/GIST_ID не заданы: данные только на диске, Render их может стереть")
    elif not _gist_ready:
        log.error("Gist не прочитался: копию в него не пишу, чтобы не затереть старые данные")
    return fix_data(d)


DATA = load_data()


def save_data():
    global _gist_thread
    try:
        text = json.dumps(DATA, ensure_ascii=False)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, DATA_FILE)
    except OSError:
        log.exception("не получилось сохранить данные")
    if _gist_ready:
        _gist_text[0] = text
        _gist_event.set()
        if _gist_thread is None or not _gist_thread.is_alive():
            _gist_thread = threading.Thread(target=_gist_worker, daemon=True)
            _gist_thread.start()



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
    why = ", антиспам" if ban.get("reason") == "spam" else ""
    return f"{uid} — {name} ({end}{why})"


# ---------- Ключи доступа ----------
# Ключ даёт доступ навсегда (dur = 0) или на время (dur = секунд с момента ввода ключа).
# Админы (ADMIN_IDS) проходят без ключа. Пока проверка выключена (/access off), бот открыт для всех.

KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих символов (0/O, 1/I)
WARN_MAX = 24 * 3600                               # предупреждать о конце доступа не раньше чем за сутки
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT", "").strip()  # например @username - покажем тем, у кого нет ключа


def norm_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def fmt_key(key: str) -> str:
    return f"{key[:4]}-{key[4:]}"


def dur_label(secs: int) -> str:
    if not secs:
        return "навсегда"
    if secs % 86400 == 0:
        return f"{secs // 86400} дн."
    if secs >= 3600:
        return f"{secs // 3600} ч"
    return f"{max(1, secs // 60)} мин"


def until_label(until: float) -> str:
    return "навсегда" if not until else f"до {datetime.fromtimestamp(until, TZ):%d.%m.%Y %H:%M}"


def parse_term(text: str):
    """'7d' / '12h' / '30m' -> секунды; 'навсегда' / '0' -> 0; непонятное -> None."""
    t = text.strip().lower()
    if t in ("0", "forever", "навсегда", "inf", "∞"):
        return 0
    return parse_duration(t)


def grant(uid: int, dur: int):
    """Выдаёт доступ на dur секунд (0 = навсегда). Если доступ уже есть - продлевает."""
    allowed, now = DATA["access"]["allowed"], time.time()
    cur = allowed.get(str(uid))
    live = bool(cur) and (not cur["until"] or cur["until"] > now)
    if not dur or (live and not cur["until"]):
        until = 0
    else:
        until = (cur["until"] if live else now) + dur
    allowed[str(uid)] = {"until": until, "granted": now, "warned": False}


def has_access(uid: int) -> bool:
    acc = DATA["access"]
    if not acc["required"] or uid in ADMIN_IDS:
        return True
    a = acc["allowed"].get(str(uid))
    return bool(a) and (not a["until"] or time.time() < a["until"])


def known_users() -> list:
    """Все, кто хоть раз пользовался ботом (личные чаты), кроме админов."""
    ids = {int(u) for u in DATA["stats"]["users"] if u.lstrip("-").isdigit()}
    ids |= set(DATA["subs"])
    ids |= {int(k) for k in DATA["prefs"] if k.lstrip("-").isdigit()}
    return sorted(i for i in ids if i > 0 and i not in ADMIN_IDS)


def grant_targets():
    """(кому выдать, у кого доступ уже действует, заблокированные)."""
    allowed, now = DATA["access"]["allowed"], time.time()
    todo, already, banned = [], [], []
    for uid in known_users():
        a = allowed.get(str(uid))
        if is_banned(uid):
            banned.append(uid)
        elif a and (not a["until"] or a["until"] > now):
            already.append(uid)
        else:
            todo.append(uid)
    return todo, already, banned


def grant_all(dur: int):
    """Сразу выдаёт и активирует доступ всем, кто пользовался ботом и ещё не имеет его. Ключи не нужны."""
    todo, already, banned = grant_targets()
    for uid in todo:
        grant(uid, dur)
    save_data()
    return todo, already, banned


def key_state(k: dict) -> str:
    return "used" if k["uses"] >= k["max_uses"] else "ok"


def redeem_key(uid: int, raw: str, kind: str = "access") -> str:
    """Применяет ключ нужного типа. Возвращает 'ok' / 'invalid' / 'used'."""
    k = DATA["access"]["keys"].get(norm_key(raw))
    if not k or k.get("kind", "access") != kind:
        return "invalid"
    if key_state(k) == "used":
        return "used"
    k["uses"] += 1
    k["users"].append(uid)
    if kind == "unban":
        DATA["banned"].pop(str(uid), None)
        _muted_until.pop(uid, None)
        _strikes.pop(uid, None)
    else:
        grant(uid, k["dur"])
    save_data()
    return "ok"


def create_key(uses: int, dur: int, kind: str = "access") -> str:
    keys = DATA["access"]["keys"]
    while True:
        key = "".join(secrets.choice(KEY_ALPHABET) for _ in range(8))
        if key not in keys:
            break
    keys[key] = {"created": time.time(), "dur": dur, "max_uses": uses, "uses": 0, "users": [], "kind": kind}
    save_data()
    return key


def set_access(on: bool, strict: bool = False) -> str:
    """Включает/выключает проверку ключа, возвращает текст для админа."""
    acc = DATA["access"]
    if not on:
        acc["required"] = False
        save_data()
        return "🔓 Проверка ключа выключена: бот открыт для всех."
    if strict:
        n = len(acc["allowed"])
        acc["allowed"] = {}
        text = f"🔒 Проверка ключа включена. Доступ сброшен и у тех, кто вводил ключ ({n}): ключ нужен каждому, кроме админов."
    else:
        # доступ сохраняется только у тех, кто вводил ключ (и у админов); остальным ключ нужен
        known = set(DATA["subs"]) | {int(u) for u in DATA["stats"]["users"] if u.lstrip("-").isdigit()}
        without = [u for u in known if u not in ADMIN_IDS and str(u) not in acc["allowed"]]
        text = (f"🔒 Проверка ключа включена. У кого уже есть ключ ({len(acc['allowed'])}) и у админов доступ остаётся. "
                f"Без ключа остались {len(without)} чел., кто раньше пользовался ботом: им нужен ключ.\n"
                "Выдать им доступ сразу, без ключей: /grantall навсегда (или /grantall 30d).")
    acc["required"] = True
    save_data()
    return text


KEY_ERRORS = {
    "invalid": "❌ Неверный ключ. Проверь и отправь ещё раз.",
    "used": "❌ Этот ключ уже использован. Попроси новый у администратора.",
    "forever": "✅ У тебя уже доступ навсегда, ключ тратить не нужно. Он остаётся действующим.",
    "locked": "⏳ Слишком много неверных ключей. Подожди 15 минут и попробуй снова.",
}
FAIL_LIMIT, FAIL_WINDOW, FAIL_LOCK = 5, 600, 900   # 5 неверных ключей за 10 минут -> пауза 15 минут
_key_fails = {}                                    # user_id -> [число ошибок, начало окна, пауза до]


def try_key(uid: int, raw: str, kind: str = "access") -> str:
    """Применяет ключ с защитой от перебора. Возвращает 'ok' / 'invalid' / 'used' / 'forever' / 'locked'."""
    now = time.time()
    f = _key_fails.get(uid)
    if f and f[2] > now:
        return "locked"
    a = DATA["access"]["allowed"].get(str(uid))
    k0 = DATA["access"]["keys"].get(norm_key(raw))
    if kind == "access" and a and not a["until"] and k0 and k0.get("kind", "access") == "access":
        return "forever"  # доступ уже вечный: ключ не расходуем
    res = redeem_key(uid, raw, kind)
    if res == "invalid":
        if not f or now - f[1] > FAIL_WINDOW:
            f = [0, now, 0]
        f[0] += 1
        if f[0] >= FAIL_LIMIT:
            f = [0, now, now + FAIL_LOCK]
        _key_fails[uid] = f
    elif res == "ok":
        _key_fails.pop(uid, None)
    return res


def lock_text(note: str = "") -> str:
    who = f"Ключ выдаёт администратор: {ADMIN_CONTACT}" if ADMIN_CONTACT else "Ключ выдаёт администратор."
    return f"{note}🔒 Доступ к боту по ключу.\n\nОтправь ключ сообщением (вид XXXX-XXXX).\n{who}"


async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пока включена проверка, без ключа бот ничего не делает (кроме приёма самого ключа)."""
    user = update.effective_user
    if not user:
        return
    acc, uid, msg = DATA["access"], user.id, update.message
    if has_access(uid):
        # продление: человек с доступом прислал новый ключ
        if acc["required"] and uid not in ADMIN_IDS and msg and msg.text:
            raw = msg.text.strip()
            if len(raw) <= 20 and len(norm_key(raw)) == 8 and norm_key(raw) in acc["keys"]:
                res = try_key(uid, raw)
                if res == "ok":
                    await msg.reply_text(f"✅ Ключ принят. Доступ: {until_label(acc['allowed'][str(uid)]['until'])}.")
                else:
                    await msg.reply_text(KEY_ERRORS[res])
                raise ApplicationHandlerStop
        return
    if update.callback_query:
        await update.callback_query.answer("🔒 Нужен ключ доступа. Отправь его боту сообщением.", show_alert=True)
        raise ApplicationHandlerStop
    if not msg:
        return
    text = (msg.text or "").strip()
    if re.match(r"(?i)^/myid\b", text):
        return  # чтобы можно было узнать свой id
    m = re.match(r"(?i)^/start(?:@\w+)?\s+(.+)$", text)  # ссылка вида t.me/бот?start=КЛЮЧ
    candidate = m.group(1) if m else ("" if text.startswith("/") else text)
    if candidate and len(candidate) <= 40 and len(norm_key(candidate)) == 8:  # похоже на ключ
        res = try_key(uid, candidate)
        if res == "ok":
            await msg.reply_text(f"✅ Ключ принят! Доступ: {until_label(acc['allowed'][str(uid)]['until'])}.")
            await start(update, context)
        else:
            await msg.reply_text(KEY_ERRORS[res])
        raise ApplicationHandlerStop
    expired = str(uid) in acc["allowed"]  # запись есть, а доступа нет - значит срок вышел
    await msg.reply_text(lock_text("⌛ Срок твоего доступа закончился.\n\n" if expired else ""),
                         reply_markup=ReplyKeyboardRemove())
    raise ApplicationHandlerStop


async def access_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в минуту: предупреждает о скором конце доступа и сообщает, когда он закончился."""
    acc = DATA["access"]
    if not acc["required"]:
        return
    now, changed = time.time(), False
    for uid_s, a in list(acc["allowed"].items()):
        until = a.get("until", 0)
        if not until:
            continue
        uid = int(uid_s)
        if now >= until:
            del acc["allowed"][uid_s]
            changed = True
            set_sub(uid, False)  # без доступа рассылки не нужны; при новом ключе /start включит снова
            await _safe_send(context.bot, uid,
                             "⌛ Срок твоего доступа закончился.\n\nЧтобы продолжить пользоваться ботом, "
                             "отправь новый ключ (его выдаёт администратор).", ReplyKeyboardRemove())
        elif not a.get("warned") and until - now <= min(WARN_MAX, max(60, (until - a.get("granted", now)) / 4)):
            a["warned"] = True
            changed = True
            await _safe_send(context.bot, uid,
                             f"⏳ Твой доступ заканчивается {until_label(until)}.\n\n"
                             "Чтобы продлить, просто отправь боту новый ключ сообщением.")
    if changed:
        save_data()


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
SPAM_LOCK_AT = 4  # на столько-м нарушении за короткое время человек блокируется до ключа разбана


def lock_ban_text() -> str:
    who = f" ({ADMIN_CONTACT})" if ADMIN_CONTACT else ""
    return ("🚫 Ты слишком часто нарушал правила (спам), поэтому доступ закрыт.\n\n"
            f"Чтобы вернуться, нужен ключ разбана: его выдаёт администратор{who}. "
            "Отправь ключ сообщением (вид XXXX-XXXX).")


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
        ban = DATA["banned"].get(str(uid)) or {}
        await msg.reply_text(lock_ban_text() if ban.get("reason") == "spam" else TECH_TEXT)
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


async def _safe_send(bot, chat_id, text: str, reply_markup=None):
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
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


async def try_unban_key(msg, user, context) -> bool:
    """Заблокированный прислал ключ разбана? True, если сообщение обработано как ключ."""
    text = (msg.text or "").strip()
    m = re.match(r"(?i)^/start(?:@\w+)?\s+(.+)$", text)
    candidate = m.group(1) if m else ("" if text.startswith("/") else text)
    if not candidate or len(candidate) > 40 or len(norm_key(candidate)) != 8:
        return False
    was_muted = is_muted(user.id)
    res = try_key(user.id, candidate, "unban")
    if res == "ok":
        _recent[user.id].clear()
        what = "мут" if was_muted else "блокировка"
        await msg.reply_text(f"✅ Ключ принят, {what} снят{'' if was_muted else 'а'}. Можно пользоваться ботом.",
                             reply_markup=KEYBOARD)
        name = user.full_name + (f" (@{user.username})" if user.username else "")
        for admin in ADMIN_IDS:
            await _safe_send(context.bot, admin, f"🔓 {name} ({user.id}) снял {'мут' if was_muted else 'блокировку'} ключом разбана.")
    else:
        await msg.reply_text(KEY_ERRORS.get(res, KEY_ERRORS["invalid"]))
    return True


MUTE_KEY_RE = re.compile(r"^[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}$")  # вид ключа XXXX-XXXX (в муте реагируем только на него)


async def antispam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    if user.id in ADMIN_IDS:  # админа не мутим и не банним, иначе при проверке команд он сам себя заблокирует
        track(update)
        return
    if is_banned(user.id):
        if await try_unban_key(msg, user, context):
            raise ApplicationHandlerStop
        await blocked_notice(msg, user.id)
        raise ApplicationHandlerStop  # бан: ничего не выполняем, в том числе /start
    now = msg.date.timestamp()  # время отправки сообщения, а не обработки: бот обрабатывает по очереди
    if _muted_until.get(user.id, 0) > now:
        track(update)
        if MUTE_KEY_RE.match((msg.text or "").strip()) and await try_unban_key(msg, user, context):
            raise ApplicationHandlerStop  # прислал ключ разбана: мут снят (или ключ неверный)
        await blocked_notice(msg, user.id)
        raise ApplicationHandlerStop  # игнор, пока действует мут
    q = _recent[user.id]
    q.append(now)
    if len(q) == SPAM_LIMIT + 1 and now - q[0] <= SPAM_WINDOW:
        strike = _strikes.get(user.id, [0, 0])
        if now - strike[1] > SPAM_FORGET:
            strike[0] = 0  # давно не спамил - начинаем сначала
        if strike[0] + 1 >= SPAM_LOCK_AT:  # слишком часто: блокировка, снять можно ключом разбана или /unban
            name = user.full_name + (f" (@{user.username})" if user.username else "")
            q.clear()
            _strikes.pop(user.id, None)
            _muted_until.pop(user.id, None)
            DATA["banned"][str(user.id)] = {"name": name, "until": 0, "reason": "spam"}
            track(update, spam=True)  # заодно сохраняет данные
            await msg.reply_text(lock_ban_text())
            for admin in ADMIN_IDS:
                await _safe_send(context.bot, admin,
                                 f"🚨 {name} ({user.id}) заблокирован за частый спам.\n"
                                 "Снять: /unban id или выдать ему ключ: /unbankey")
            raise ApplicationHandlerStop
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


# ---------- Весёлые фразы под ответами ----------
# Показываются только в ответах на кнопки «Замены», «Есть ли замены?» и «Пары завтра»
# (в авто-уведомления и утреннее расписание не добавляются). Свои фразы можно дописывать в списки.

FUN = {
    # замены есть
    "changes": [
        "Замены есть — сверяй часы и пары 🕵️",
        "Расписание сегодня с сюрпризом 🎁",
        "Хорошо, что я проверил, а то пошёл бы не туда 😅",
        "Внимательно читай, а то придёшь в пустой кабинет 🚪",
        "Расписание — это лишь рекомендация 📜",
        "Сфоткай и закрепи, потом не придётся спрашивать в чате 📌",
        "План меняется, мы адаптируемся 🦎",
        "Не паникуем, просто идём по новому маршруту 🧭",
        "Кто предупреждён, тот вооружён ⚔️",
        "Сегодня учебный квест: найди, что поменялось 🔎",
        "Если что-то не сходится с расписанием — это не ты, это замены.",
        "Перешли одногруппникам, а то будет весело 😄",
        "Хорошая новость: ты знаешь заранее. Плохая: идти всё равно надо 🙃",
        "Расписание как прогноз погоды: иногда меняется 🌦",
        "Перепроверь кабинет, не будь героем под закрытой дверью 🚶",
        "Новая глава расписания, читаем внимательно 📖",
        "Бывает. Главное — не проспать новый вариант 😴",
        "Другие предметы, другие лица, тот же ты 🎭",
        "Замены — это когда расписание тоже любит разнообразие 🎨",
        "Лучше сверить дважды, чем стоять в коридоре один раз 🧍",
        "Не пугайся, это всего лишь расписание, которое передумало 🤷",
        "Чуть-чуть хаоса ещё никому не мешало 🌪",
        "Расписание решило устроить разминку для мозга 🧠",
        "Кто следит за заменами, тот не теряется 🧭",
        "Сегодня гибкий график — в прямом смысле 🤸",
        "Перемены в расписании — перемены в настроении (надеюсь, к лучшему) 🌈",
        "Замены — как сюрприз в коробке: интересно, но лучше быть готовым 📦",
        "Ничего страшного, просто немного импровизации 🎷",
        "Хороший день, чтобы потренировать внимательность 🔍",
        "Если сомневаешься — переспроси у старосты, она точно в курсе 😉",
    ],
    # урок снят
    "cancel": [
        "Урок снят — выдыхаем 🎉",
        "Окно в расписании — повод для кофе ☕",
        "Свободный урок: самое время доделать то, что откладывал 📚",
        "Тот случай, когда замена — лучший подарок 🎁",
        "Лишний час сна, или хотя бы лишний час обеда 😌",
        "Сегодня расписание сжалилось над нами 🙏",
        "Урок снят. Дальше сам решай: домашка или сериал 🍿",
        "Кто-то наверху услышал твои молитвы ✨",
        "Не радуйся слишком громко, пока не сверил с чатом 🤫",
        "Минус пара — плюс настроение 📈",
        "Идеальный момент прогуляться, хотя бы до столовой 🚶",
        "Закон подлости отдыхает: сегодня всё в нашу пользу 😎",
        "Освободившееся время лучше потратить с умом. Или поспать 💤",
        "Спасибо расписанию за неожиданный перерыв 🫶",
        "Радуемся тихо, чтобы не сглазить 🤞",
        "Подарок судьбы: на один урок меньше 🎀",
        "Вот это поворот. Приятный 😏",
        "Кажется, сегодня день будет добрее обычного ☀️",
        "Урок снят: ура, но не расслабляйся совсем 😄",
        "Свободное окно — как оазис в пустыне расписания 🏜",
        "Если урок снят, значит, мир всё-таки справедлив ⚖️",
        "Лишний перерыв никому не вредил ☕",
        "Окно в расписании: заполнить сном, едой или учёбой — решать тебе 🎲",
        "Весть радостная: нагрузка стала легче 📉",
    ],
    # замен нет
    "none": [
        "Сюрпризов нет: идём по плану 👌",
        "Тишина и спокойствие — расписание держит оборону 🛡",
        "Ни одной замены. Скучно, зато предсказуемо 😌",
        "Всё по расписанию. Редкая удача — пользуйся 😄",
        "Никаких изменений: можно спокойно доспать до будильника ⏰",
        "Расписание сегодня не кусается 🐶",
        "Без замен, без паники, без лишних разговоров ✌️",
        "Классика жанра: всё как в расписании 📅",
        "Идеальное «ничего не поменялось» 👍",
        "Преподаватели в строю, расписание в силе 💪",
        "Обошлось без неожиданностей. Береги это чувство 🧘",
        "Как в учебнике: всё по плану 📘",
        "Проверка пройдена: изменений нет ✅",
        "Можно спокойно собирать сумку по привычке 🎒",
        "Стабильность — признак мастерства 🏆",
        "Все на своих местах, всё по-старому 🕰",
        "Беспокоиться не о чем, кроме домашки 📝",
        "Если и опоздаешь, то уже не из-за замен 😏",
        "Замен нет. Ищи другой повод для драмы 🎭",
        "Даже расписание иногда бывает добрым 🌞",
        "Скучная новость — хорошая новость 😴",
        "Нет замен — нет проблем. Почти 😅",
        "Расписание сегодня предсказуемо, как понедельник после выходных 😅",
        "Всё идёт своим чередом, и это прекрасно 🌊",
        "Нулевая интрига, стопроцентная стабильность 📊",
        "Нет замен — можно спать чуть спокойнее 🛏",
        "Проверил за тебя: изменений нет 🔎",
        "Расписание в силе, ты тоже держись 🫡",
        "Ничего нового, но зато и неприятностей нет 🙌",
        "Работаем по классической схеме 📐",
    ],
    # «Замены»: файла ещё нет
    "notposted": [
        "Файл с заменами пока в пути 🐢",
        "Ещё не выложили — запасись терпением 🧘",
        "Замены в процессе, загляни позже ⏳",
        "Тишина — значит, ещё пишут. Заходи позже 📝",
        "Пока замен нет, живём по расписанию 🤞",
        "Файла ещё нет. Преподаватели тоже не всегда знают заранее 🤷",
        "Пока затишье. Включи «🔔 Уведомления», и я скажу, когда появится",
        "Чашка чая — и проверь позже ☕",
        "Не нервничай: как только выложат, всё будет тут 📬",
        "Ожидание — тоже часть учебного процесса ⏳",
        "Бумаги пока не дошли, но я слежу 👀",
        "Файл ещё не появился. Дыши ровно 🌬",
        "Замены, как поезд: обязательно приедут, но не точно по времени 🚂",
        "Пока нечего показать, кроме моего сочувствия 🫂",
        "Замены ещё не появились — наверное, их пишут с любовью ❤️",
        "Я проверил: пока пусто. Но я не сдаюсь 💪",
        "Файла нет. Давай посмотрим на ситуацию философски 🧘",
        "Секунду-другую (ну, или пару часов) — и замены появятся ⏳",
        "Рано ещё нервничать, замены не выложены 😌",
    ],
    # «Есть ли замены?»: файл выложен
    "posted": [
        "Бегом смотреть — жми «Замены» 🏃",
        "Файл уже тут, осталось только прочитать 📄",
        "Свежие замены, ещё тёплые 🔥",
        "Есть что почитать на ночь 📖",
        "Новости с фронта расписания: файл выложен 📰",
        "Не тяни, жми «Замены» 👇",
        "Выложили! Расписание больше не загадка 🔎",
        "Ну что, посмотрим, кому повезло? 🎲",
        "Интрига: есть изменения или нет? Узнаем, нажав «Замены» 🕵️",
        "Файл на месте. Дальше — дело техники 🛠",
        "Файл появился. Читай — и спи спокойно 😴",
        "Кто предупреждён, тот не опоздал ⏰",
        "Всё выложено, осталось только набраться смелости 😄",
        "Информация готова, ты тоже готов? 🫡",
        "Файл ждёт, как невыученный билет 🎫",
        "Самое время узнать, что там 👀",
        "Файл выложен — самое время всё узнать 🔍",
        "Замены на месте, можно смотреть 👇",
        "Ничего не пропустишь, если заглянешь сейчас 👀",
        "Время истины пришло: жми «Замены» ⚖️",
        "Есть файл, есть повод проверить 🙌",
    ],
    # «Есть ли замены?»: файла нет
    "notposted_check": [
        "Пока тишина 🤫",
        "Ждём, не нервничаем ⏳",
        "Замены ещё не появились. Расписание держится 💪",
        "Преподаватели ещё думают 🤔",
        "Пока нет. Но я смотрю, ага 👀",
        "Файла нет — значит, пока всё стабильно (или просто не успели) 😏",
        "Выложат — скажу. Включи «🔔 Уведомления», чтобы не проверять самому",
        "Свежих замен нет. Можно выдохнуть… пока 😮‍💨",
        "Пусто. Загляни чуть позже ⏰",
        "Файл в пути, как почта в понедельник 🐌",
        "Не жми каждую минуту, я сам сообщу, если включены уведомления 😅",
        "Пока ничего нет, но ты держись 🫶",
        "Нет новостей — это тоже новости 📰",
        "Тихо, как перед звонком на первую пару 🔔",
        "Замены ещё не выложены. Чайку? ☕",
        "Пока ничего нового, продолжаем ждать 🕰",
        "Нет файла — нет вопросов 😄",
        "Информации пока нет, но паники тоже нет 🧘",
        "Бывает, что замены выкладывают поздно, будь терпелив 🌙",
        "Всё ещё пусто. Но надежда умирает последней 🌱",
    ],
    # «Пары завтра»: без замен
    "sched_plain": [
        "Расписание проверено, можно собираться 🎒",
        "Список готов, осталось дожить до звонка 🔔",
        "Складываем сумку и спим спокойно 😴",
        "Пары ждут, как верные друзья 🫡",
        "Запомни: расписание тебя тоже ждёт 📅",
        "Главное — не перепутать кабинет 🚪",
        "Береги силы, понадобятся ⚡",
        "Теперь ты знаешь всё и можешь спокойно переживать 😌",
        "Не забудь зарядить телефон, пригодится на перемене 🔋",
        "Лучше один раз посмотреть, чем три раза спросить в чате 💬",
        "Завтрак — не роскошь, а средство передвижения 🥪",
        "Кто рано встаёт, тот к первой паре успевает ⏰",
        "Расписание как сюжет: знаешь, что будет, но всё равно переживаешь 🎬",
        "Если что-то поменяется, я напишу (с включёнными 🔔 уведомлениями)",
        "Всё в порядке, живём дальше 🌿",
        "Расписание составлено, осталось его пережить 😎",
        "Продумай маршрут, и день пройдёт легче 🗺",
        "Вода, зарядка, сумка — и вперёд 🚀",
        "Учиться, учиться и ещё раз… ну ты понял 📚",
        "Ни одной замены в расписании — идём ровно 🎯",
        "Пары как пары: ничего необычного 📚",
        "Хороший план на день, осталось его выполнить ✅",
        "Подготовься заранее — и день пройдёт гладко 🌤",
        "Пусть пары будут короткими, а перемены длинными 🙏",
        "Расписание есть, смелость тоже найдётся 🦁",
        "Запомни: кабинет, предмет, преподаватель — и вперёд 🧠",
        "Никаких неожиданных замен, только классика 🎼",
        "Пей воду, не забывай про обед, и всё будет хорошо 💧",
    ],
    # «Пары завтра»: есть замены 🔄
    "sched_changed": [
        "Часть пар заменили — сверяй 🔄 с расписанием 👀",
        "Внимательно: сегодня не всё так, как обычно 🧐",
        "Сохрани, чтобы потом не листать 📌",
        "Расписание обновилось, твоя паника — нет 😎",
        "Значок 🔄 — не украшение, а предупреждение ⚠️",
        "Новый день — новые приключения 🎢",
        "Перед выходом перечитай ещё раз, не пожалеешь 📖",
        "Теперь можно собираться, но с поправкой на замены 🎒",
        "Замены нашлись, и это уже полдела 🔍",
        "Лучше знать заранее, чем бежать по коридору 🏃",
        "День с изюминкой: что-то поменялось 🍇",
        "Расписание подмигнуло и поменяло пару-тройку вещей 😉",
        "Отметь для себя, что поменялось, и всё будет хорошо ✅",
        "Не потеряйся в заменах, ты справишься 🧭",
        "Заметил 🔄? Это значит, что план поменялся 🧐",
        "Расписание немного поиграло в догонялки 🏃",
        "Всё под контролем, просто с корректировками 🛠",
        "Хорошо, что ты проверил заранее 👏",
        "Не все пары как всегда, но ты справишься 💪",
        "Замены учтены, можно спокойно планировать день 🗓",
    ],
    # «Пары завтра»: замены ещё не выложены
    "sched_notposted": [
        "Это расписание по плану, замены ещё могут добавиться ⏳",
        "Включи «🔔 Уведомления» — напишу, когда появятся замены",
        "Пока смотрим на базовый план 🗺",
        "Расписание есть, замен нет — пока что 🤞",
        "План А готов, план Б (замены) ждёт своего часа 📬",
        "Пока без замен, но кто знает, что будет вечером 🌆",
        "Смотри на расписание, но держи ухо востро 👂",
        "Замены ещё не пришли, не зазнавайся раньше времени 😄",
        "Если что — я скажу, когда файл выложат 📢",
        "Основное расписание на месте, остальное — по обстоятельствам 🎲",
        "Замены пока не выложены, но базовое расписание уже перед тобой 🗂",
        "Подожди немного: возможно, что-то ещё поменяется 🌗",
        "Пока это всё, что я знаю. Остальное — по мере поступления 📨",
        "Расписание готово, замены на подходе (возможно) 🚶",
    ],
    # день без занятий
    "noclasses": [
        "Занятий нет — отдыхаем 🛌",
        "Выходной режим: включён 🌴",
        "Сегодня можно не заглядывать в расписание, но ты всё равно заглянул 😄",
        "Свободный день, не растрать его впустую (или растрать, твоё право) 😎",
        "Пар нет, а настроение есть 🥳",
        "Расписание пустое. Красота 🌿",
        "Нет пар — нет проблем 🙌",
        "Идеальный день: заниматься только тем, что нравится 🎨",
        "Сегодня можно жить без будильника ⏰",
    ],
    # дополнительные пулы (подмешиваются, когда подходят)
    "long": [
        "Четыре пары — это серьёзно, но ты справишься 💪",
        "Сегодня день на четыре пары: запасись терпением и перекусом 🥪",
        "Длинный день: пары не спринт, а марафон 🏃",
        "Четыре пары подряд — как мини-сезон сериала 🍿",
        "Кофе или чай тебе в помощь ☕",
        "Бутерброд в сумку, и день пройдёт легче 🥪",
        "К последней паре ты станешь героем 🦸",
        "Длинный день — зато вечером ужин вкуснее 🍲",
        "Держись, последний звонок тоже когда-нибудь прозвенит 🔔",
        "Сегодня учебный день на полную катушку 🎢",
        "Длинный день: береги голос и нервы 🎤",
        "Четыре пары — прокачка выносливости +1 🎮",
        "Выдержишь четыре пары — выдержишь всё 🏋️",
        "Не забудь пообедать между парами, без еды никуда 🍱",
    ],
    "short": [
        "Короткий день: всего три пары 😎",
        "Три пары — и свободен 🕊",
        "Сегодня короткий день, пользуйся 🎁",
        "Три пары — это вполне переживаемо 😌",
        "Короткий день — значит, вечер будет длиннее 🌇",
        "День на три пары: и поучиться, и отдохнуть успеешь ⚖️",
        "Три пары, и домой: вполне приятный план 🏠",
        "Короткий день — повод не терять время впустую (или всё-таки потерять) 😄",
        "Всего три пары: даже скучать некогда 😉",
        "Лёгкий режим: три пары и свободное время ⏳",
        "Три пары пролетят быстрее, чем кажется 🚀",
        "Короткий день — как маленький бонус к неделе 🎀",
    ],
    "pe": [
        "Сегодня физкультура: не забудь форму 👟",
        "Спортивная форма — не опция 🏃",
        "Кроссовки в сумку, оправдания — дома 😄",
        "На физре главное — не умереть, остальное неважно 🏐",
        "Физкультура: шанс размяться после сидячего дня 🤸",
        "Не забудь бутылку воды 💧 и форму 👕",
        "Здоровье в твоих руках (и ногах) 🦵",
        "Спорт — это жизнь, а ещё это оценка 🏅",
        "На физкультуру без формы — как на экзамен без ручки 🖊",
        "Размяться полезно: сегодня есть такая возможность 🏃",
        "Физра сегодня: время показать, на что ты способен 🏆",
        "Форма, кроссовки, хорошее настроение — комплект на физкультуру 🎽",
        "Размяться перед парами не помешает: сегодня физкультура 🤾",
        "Не забудь перед физрой хотя бы чуть-чуть поесть 🍌",
    ],
    "info": [
        "Не пропусти информационный час 🕐",
        "Информационный час: там всегда есть что узнать 📢",
        "Информационный час — это про факты, а не про сон 😄",
        "Записывай время информационного часа, чтобы не забыть ✍️",
        "Информационный час не ждёт опоздавших ⏰",
        "Все на информационный час! 🏃",
        "Информационный час — тоже часть расписания, не забудь 📅",
        "Подготовься морально к информационному часу 😌",
        "Информационный час: не опаздывай, будет интересно (или не очень) 😄",
        "Загляни в расписание: там есть и информационный час 🕐",
        "Не забудь про информационный час, он тоже в плане 📌",
    ],
}

WEEKDAY_FUN = {
    0: ["Понедельник — день тяжёлый, но мы тяжелее 💪",
        "Понедельник, держись: впереди целая неделя 😅",
        "Старт недели, давай без драмы 🚀",
        "Понедельник: бодрость в режиме энергосбережения 🔋",
        "С понедельника начинаем новую жизнь (или хотя бы пары) 🌱",
        "Понедельник: поднимаем себя за уши и идём на пары 🏃"],
    1: ["Вторник — вторая попытка начать неделю 🔁",
        "Вторник: уже втянулись, наверное 😏",
        "Вторник — самый нейтральный день недели 🧘",
        "Вторник: понедельник позади, а пятница ещё далеко 😅",
        "Дело пошло: вторник на связи 📞",
        "Вторник: неделя уже раскачалась 🎡"],
    2: ["Среда — экватор недели, держимся 🏝",
        "Среда: полпути пройдено, дальше только веселее 🎯",
        "Середина недели — уже можно мечтать о выходных 🌴",
        "Среда: горка пройдена, теперь вниз ⛷",
        "Среда — маленькая победа над неделей 🏆",
        "Среда — день, когда понимаешь, что неделя идёт полным ходом 😄"],
    3: ["Четверг — почти пятница, потерпи 🕊",
        "Четверг: завтра уже последний рывок недели 🏁",
        "Четверг — это маленькая пятница. Ну, почти 😄",
        "Ещё чуть-чуть, и выходные 🔜",
        "Четверг: терпение и ещё немного терпения 🧘",
        "Четверг: последний этап перед финишной прямой 🏃"],
    4: ["Пятница! Осталось чуть-чуть 🥳",
        "Пятница — день, когда даже расписание улыбается 😁",
        "Последний рывок недели 🏁",
        "Пятница: мысленно ты уже на выходных 🏖",
        "Пятница: учёба учёбой, а настроение уже выходное 🎉",
        "Пятница: надень улыбку, остальное приложится 😁"],
    5: ["Суббота — учёба, но с особым шармом 😅",
        "Суббота: пока другие отдыхают, мы закаляемся 💪",
        "Суббота — день героев 🦸",
        "Суббота — последний шаг к законному отдыху 🛌",
        "Суббота: потерпи, воскресенье уже рядом 🌅",
        "Суббота: кто учится, тот молодец 🏆"],
    6: ["Воскресенье: занятий нет, но расписание всё равно проверяют 😄",
        "Воскресенье — для отдыха, а не для пар 🛋"],
}

_last_fun = {}  # какую фразу показывали в прошлый раз для этого списка (чтобы не повторять подряд)


def fun(kind: str, day: date = None, extra=()) -> str:
    """Случайная фраза по ситуации kind. Иногда вместо неё берётся фраза про день недели
    или из подходящего доп. списка (extra: 'long', 'short', 'pe', 'info')."""
    r = random.random()
    if day is not None and r < 0.22 and WEEKDAY_FUN.get(day.weekday()):
        key, pool = f"wd{day.weekday()}", WEEKDAY_FUN[day.weekday()]
    elif extra and r < 0.55:
        key = random.choice(list(extra))
        pool = FUN[key]
    else:
        key, pool = kind, FUN[kind]
    options = [p for p in pool if p != _last_fun.get(key)] or pool
    phrase = random.choice(options)
    _last_fun[key] = phrase
    return phrase


# ---------- Ответ ----------

def build_message(day: date, changes, info, fun_on: bool = False):
    head = f"📅 Замены на {day:%d.%m.%Y} ({DAYS[day.weekday()]}) — группа {GROUP_NAME}"
    body = "\n".join(changes) if changes else "По расписанию"
    if info:
        body += f"\n\n🕐 Информационный час: {info}"
    if fun_on:
        if any("урок снят" in l.lower() for l in changes or []):
            kind = "cancel"
        else:
            kind = "changes" if changes else "none"
        body += "\n\n" + fun(kind, day, ("info",) if info else ())
    return f"{head}\n\n{body}"


def make_reply(day: date, debug: bool = False, fun_on: bool = False) -> str:
    item = find_pdf(day)
    if item is None:
        text = f"Файла с заменами на {day:%d.%m.%Y} в папке пока нет. Попробуйте позже."
        return text + ("\n\n" + fun("notposted", day) if fun_on and not debug else "")
    changes, info, dbg = analyze(download(item))
    return dbg if debug else build_message(day, changes, info, fun_on)


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


def make_schedule_reply(day: date, fun_on: bool = False) -> str:
    item = find_pdf(day)
    changes, info = {}, None
    if item is not None:
        changes, info = lessons_of(download(item))
    lines = []
    items = merge_day(day, changes)
    for nums, text, was in items:
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
    if fun_on:
        if not lines:
            kind = "noclasses"
        elif item is None:
            kind = "sched_notposted"
        elif any("❌" in l for l in lines):
            kind = "cancel"
        elif any("🔄" in l for l in lines):
            kind = "sched_changed"
        else:
            kind = "sched_plain"
        extra = []
        # пара = 2 урока; снятые уроки не считаем. Длинный день: 4 пары и больше, короткий: до 3 пар
        real = [n[-1] for n, t, _ in items if t != "урок снят"]
        pairs = (max(real) + 1) // 2 if real else 0
        if pairs >= 4:
            extra.append("long")
        elif pairs >= 1:
            extra.append("short")
        if any("физ" in l.lower() for l in lines):
            extra.append("pe")
        if info:
            extra.append("info")
        body += "\n\n" + fun(kind, day, tuple(extra))
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
        text = f"❌ Замен на {title} пока нет.\n\n" + fun("notposted_check", day)
    else:
        text = f"✅ Замены на {title} уже выложены. Нажмите «Замены», чтобы посмотреть.\n\n" + fun("posted", day)
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
        reply = await asyncio.to_thread(make_reply, day, debug, True)
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
        reply = await asyncio.to_thread(make_schedule_reply, day, True)
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
# Три независимых вида уведомлений:
#   замены               - DATA["subs"] (пишу, когда выложили/обновили файл с заменами; времени нет)
#   расписание на день   - DATA["prefs"][id]["morning"/"time"] (раз в день в выбранное время, по умолчанию 7:00)
#   расписание на завтра - DATA["prefs"][id]["evening"/"etime"] (раз в день в выбранное время, по умолчанию 20:00)

KINDS = {  # "m" - расписание на день, "e" - расписание на завтра: в каких полях prefs хранятся настройки
    "m": {"on": "morning", "time": "time", "last": "last", "default": MORNING_DEFAULT},
    "e": {"on": "evening", "time": "etime", "last": "elast", "default": EVENING_DEFAULT},
}


def get_pref(chat_id: int) -> dict:
    p = DATA["prefs"].get(str(chat_id))
    if p is None:
        p = DATA["prefs"][str(chat_id)] = new_pref(False)
    p.setdefault("time", MORNING_DEFAULT)
    p.setdefault("last", "")
    p.setdefault("evening", False)
    p.setdefault("etime", EVENING_DEFAULT)
    p.setdefault("elast", "")
    return p


def parse_hhmm(s):
    """'7', '7:05', '07.30', '7-30' -> '07:05' или None."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:\s*[:.\-]\s*(\d{2}))?\s*", s or "")
    if not m:
        return None
    h, mi = int(m[1]), int(m[2] or 0)
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def _rearm(p: dict, kind: str = "m"):
    """После включения или смены времени: если время сегодня уже прошло - ждём до завтра, иначе сегодня ещё придёт."""
    k, now = KINDS[kind], datetime.now(TZ)
    p[k["last"]] = now.date().isoformat() if now.time().strftime("%H:%M") >= p[k["time"]] else ""


def set_alerts(chat_id: int, on: bool):
    if on and chat_id not in DATA["subs"]:
        DATA["subs"].append(chat_id)
    elif not on and chat_id in DATA["subs"]:
        DATA["subs"].remove(chat_id)
    save_data()


def set_schedule(chat_id: int, kind: str, on: bool):
    """Включает/выключает расписание на день (kind='m') или на завтра (kind='e')."""
    p, k = get_pref(chat_id), KINDS[kind]
    if on and not p[k["on"]]:
        _rearm(p, kind)
    p[k["on"]] = on
    save_data()


def set_schedule_time(chat_id: int, kind: str, hhmm: str):
    p = get_pref(chat_id)
    p[KINDS[kind]["time"]] = hhmm
    _rearm(p, kind)
    save_data()


def set_morning(chat_id: int, on: bool):
    set_schedule(chat_id, "m", on)


def set_morning_time(chat_id: int, hhmm: str):
    set_schedule_time(chat_id, "m", hhmm)


def set_sub(chat_id: int, on: bool):
    """Всё сразу: замены, расписание на день и на завтра (время, которое выбрал человек, сохраняется)."""
    set_schedule(chat_id, "m", on)
    set_schedule(chat_id, "e", on)
    set_alerts(chat_id, on)


def all_subscribers() -> list:
    """Все, кому что-то включено (для рассылок админа)."""
    ids = set(DATA["subs"]) | {int(k) for k, p in DATA["prefs"].items()
                               if (p.get("morning") or p.get("evening")) and k.lstrip("-").isdigit()}
    return sorted(ids)


def notif_text(chat_id: int) -> str:
    alerts, p = chat_id in DATA["subs"], get_pref(chat_id)
    return "\n".join([
        "🔔 Настройки уведомлений", "",
        f"• Замены: {'включены — напишу, когда выложат или обновят файл' if alerts else 'выключены'}",
        f"• Расписание на день: {'включено — пришлю в ' + p['time'] if p['morning'] else 'выключено'}",
        f"• Расписание на завтра: {'включено — пришлю в ' + p['etime'] if p['evening'] else 'выключено'}",
        "", "Ошибки или неточности? Кнопка «🆘 Помощь».",
    ])


def notif_markup(chat_id: int) -> InlineKeyboardMarkup:
    alerts, p, B = chat_id in DATA["subs"], get_pref(chat_id), InlineKeyboardButton
    rows = [[B(f"{'🔔' if alerts else '🔕'} Замены: {'вкл' if alerts else 'выкл'}", callback_data="ntf:a")],
            [B(f"{'☀️' if p['morning'] else '🌙'} Расписание на день: {'вкл' if p['morning'] else 'выкл'}", callback_data="ntf:m")]]
    if p["morning"]:
        rows.append([B(f"⏰ Время (день): {p['time']}", callback_data="ntf:t")])
    rows.append([B(f"{'🌆' if p['evening'] else '🌙'} Расписание на завтра: {'вкл' if p['evening'] else 'выкл'}", callback_data="ntf:e")])
    if p["evening"]:
        rows.append([B(f"⏰ Время (завтра): {p['etime']}", callback_data="ntf:et")])
    rows.append([B("🆘 Помощь", callback_data="ntf:h")])
    return InlineKeyboardMarkup(rows)


TIME_CHOICES = {"m": ["06:00", "06:30", "07:00", "07:30", "08:00", "08:30"],
                "e": ["18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]}
TIME_TEXT = {"m": "⏰ Во сколько присылать расписание на день? Выбери время или нажми «Своё время».",
             "e": "⏰ Во сколько присылать расписание на завтра? Выбери время или нажми «Своё время»."}
# коды в callback_data: для расписания на день t/s/c/m, для расписания на завтра et/es/ec/e
CB_CODES = {"m": {"toggle": "m", "menu": "t", "set": "s", "custom": "c"},
            "e": {"toggle": "e", "menu": "et", "set": "es", "custom": "ec"}}


def time_markup(chat_id: int, kind: str = "m") -> InlineKeyboardMarkup:
    k, codes, B = KINDS[kind], CB_CODES[kind], InlineKeyboardButton
    cur = get_pref(chat_id)[k["time"]]
    btns = [B(("✅ " if t == cur else "") + t, callback_data=f"ntf:{codes['set']}:" + t.replace(":", ""))
            for t in TIME_CHOICES[kind]]
    return InlineKeyboardMarkup([btns[:3], btns[3:],
                                 [B("✏️ Своё время", callback_data="ntf:" + codes["custom"])],
                                 [B("🔕 Выключить", callback_data="ntf:" + codes["toggle"])],
                                 [B("↩️ Назад", callback_data="ntf:b")]])


AWAIT_TIME = {}  # chat_id -> (до какого момента (monotonic), вид "m"/"e"): ждём время, написанное текстом


class _AwaitTime(filters.MessageFilter):
    def filter(self, message):
        return AWAIT_TIME.get(message.chat_id, (0, ""))[0] > time.monotonic()


# ---- Помощь: контакт админа ----
_contact = [""]


async def admin_contact(bot) -> str:
    """@юзернейм админа: из переменной ADMIN_CONTACT, а если её нет - берём у первого админа из ADMIN_IDS."""
    c = ADMIN_CONTACT.strip()
    if c:
        return c if c.startswith(("@", "http", "+")) else "@" + c
    if _contact[0]:
        return _contact[0]
    for admin in sorted(ADMIN_IDS):
        try:
            u = (await bot.get_chat(admin)).username
        except Exception:  # noqa: BLE001
            continue
        if u:
            _contact[0] = "@" + u
            return _contact[0]
    return ""


async def help_text(bot) -> str:
    c = await admin_contact(bot)
    who = f"напиши администратору: {c}" if c else "напиши администратору (спроси у старосты, как с ним связаться)"
    return ("🆘 Помощь\n\n"
            "Бот ошибся, не отвечает, прислал неверные замены или неточное расписание? Нашёл неисправность? "
            f"Пожалуйста, {who}\n\n"
            "Чтобы быстрее разобраться, напиши:\n"
            "• что нажал или написал боту;\n"
            "• что ожидал и что получил (лучше со скриншотом);\n"
            "• на какую дату были замены или расписание.")


async def toggle_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «🔔 Уведомления» - меню настроек."""
    cid = update.effective_chat.id
    AWAIT_TIME.pop(cid, None)
    await update.message.reply_text(notif_text(cid), reply_markup=notif_markup(cid))


async def _edit(q, text, markup):
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def notif_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    cid = q.message.chat_id
    parts = q.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    AWAIT_TIME.pop(cid, None)
    kind = "e" if act in ("e", "et", "es", "ec") else "m"
    note = ""
    if act == "a":
        on = cid not in DATA["subs"]
        set_alerts(cid, on)
        note = "🔔 Уведомления о заменах включены" if on else "🔕 Уведомления о заменах выключены"
    elif act in ("m", "e"):
        on = not get_pref(cid)[KINDS[kind]["on"]]
        set_schedule(cid, kind, on)
        what = "на день" if kind == "m" else "на завтра"
        hhmm = get_pref(cid)[KINDS[kind]["time"]]
        note = f"☀️ Расписание {what} включено — в {hhmm}" if on else f"🌙 Расписание {what} выключено"
    elif act in ("t", "et"):
        await q.answer()
        await _edit(q, TIME_TEXT[kind], time_markup(cid, kind))
        return
    elif act in ("s", "es") and len(parts) > 2:
        hhmm = parse_hhmm(parts[2][:2] + ":" + parts[2][2:])
        if hhmm:
            set_schedule_time(cid, kind, hhmm)
            note = f"⏰ Расписание {'на день' if kind == 'm' else 'на завтра'} буду присылать в {hhmm}"
    elif act in ("c", "ec"):
        AWAIT_TIME[cid] = (time.monotonic() + 600, kind)
        await q.answer()
        await _edit(q, "✏️ Напиши время сообщением, например: 6:45 или 08.15\n(от 00:00 до 23:59)",
                    InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data="ntf:b")]]))
        return
    elif act == "h":
        await q.answer()
        await _edit(q, await help_text(context.bot),
                    InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Назад", callback_data="ntf:b")]]))
        return
    await q.answer(note or None)
    await _edit(q, notif_text(cid), notif_markup(cid))


async def time_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Человек нажал «Своё время» и прислал время текстом."""
    cid = update.effective_chat.id
    hhmm = parse_hhmm(update.message.text)
    if not hhmm:
        await update.message.reply_text("Не похоже на время 🤔 Напиши, например: 6:45 или 08.15 (от 00:00 до 23:59).")
        return  # продолжаем ждать
    kind = AWAIT_TIME.pop(cid, (0, "m"))[1]
    set_schedule_time(cid, kind, hhmm)
    p = get_pref(cid)
    extra = "" if p[KINDS[kind]["on"]] else "\n\nСейчас это расписание выключено — включи его кнопкой ниже."
    await update.message.reply_text(f"✅ Время сохранено: {hhmm}.{extra}\n\n" + notif_text(cid),
                                    reply_markup=notif_markup(cid))


def sub_on_text(chat_id: int) -> str:
    p = get_pref(chat_id)
    return (f"🔔 Уведомления включены. Я напишу сам, когда выложат замены на завтра (или обновят файл), "
            f"в {p['time']} пришлю расписание на день, а в {p['etime']} — на завтра. "
            "Настроить: кнопка «🔔 Уведомления».")


SUB_OFF = "🔕 Уведомления выключены. Включить обратно: кнопка «🔔 Уведомления»."


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    set_sub(cid, True)
    await update.message.reply_text(sub_on_text(cid), reply_markup=KEYBOARD)


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_sub(update.effective_chat.id, False)
    await update.message.reply_text(SUB_OFF, reply_markup=KEYBOARD)


async def broadcast(bot, text: str, ids=None):
    """Рассылка: по умолчанию тем, у кого включены замены; можно передать свой список id."""
    for cid in list(DATA["subs"] if ids is None else ids):
        if is_banned(cid) or not has_access(cid):  # в личке id чата = id пользователя
            continue
        try:
            await bot.send_message(cid, text[:4000], reply_markup=KEYBOARD)
        except (Forbidden, BadRequest):  # бота заблокировали / чата нет
            set_sub(cid, False)
        except Exception:  # noqa: BLE001
            log.exception("не удалось отправить уведомление %s", cid)
        await asyncio.sleep(0.05)


async def watch_job(context: ContextTypes.DEFAULT_TYPE):
    """Смотрит, не появился ли/не изменился ли файл на завтра.
    Пн-сб с 8:00 до 16:00 - каждую минуту, в остальное время - раз в 5 минут."""
    global _last_check
    now = datetime.now(TZ)
    fast = now.weekday() < 6 and FAST_FROM <= now.time() < FAST_TO
    gap = CHECK_FAST if fast else CHECK_SLOW
    if time.monotonic() - _last_check < gap - 5:  # -5 сек запас на дрожание таймера
        return
    _last_check = time.monotonic()
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


def due_list(now: datetime, kind: str) -> list:
    """Кому сейчас пора слать расписание вида kind: [(chat_id, настройки, 'ЧЧ:ММ')]."""
    k, today = KINDS[kind], now.date()
    due = []
    for cid, p in DATA["prefs"].items():
        if not p.get(k["on"]) or p.get(k["last"]) == today.isoformat() or not cid.lstrip("-").isdigit():
            continue
        hhmm = parse_hhmm(p.get(k["time"], k["default"])) or k["default"]
        at = datetime.combine(today, dtime(int(hhmm[:2]), int(hhmm[3:])), TZ)
        if at <= now < at + MORNING_GRACE:
            due.append((int(cid), p, hhmm))
    return due


async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в минуту: кому сейчас пора - тому шлём расписание на сегодня (у каждого своё время, по умолчанию 7:00)."""
    now = datetime.now(TZ)
    today = now.date()
    if today.weekday() == 6:  # в воскресенье занятий нет
        return
    due = due_list(now, "m")
    if not due:
        return
    for _, p, _ in due:  # отмечаем заранее, чтобы не выслать дважды
        p["last"] = today.isoformat()
    save_data()
    try:
        text = await asyncio.to_thread(make_schedule_reply, today)
    except Exception:  # noqa: BLE001
        log.exception("ошибка утреннего расписания")
        for _, p, _ in due:  # попробуем в следующую минуту
            p["last"] = ""
        save_data()
        return
    morning = [cid for cid, _, hhmm in due if hhmm < "12:00"]
    later = [cid for cid, _, hhmm in due if hhmm >= "12:00"]
    if morning:
        await broadcast(context.bot, f"☀️ Доброе утро!\n\n{text}", ids=morning)
    if later:
        await broadcast(context.bot, f"📅 Расписание на сегодня\n\n{text}", ids=later)


async def evening_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в минуту: кому сейчас пора - тому шлём расписание на завтра (у каждого своё время, по умолчанию 20:00).
    Если завтра воскресенье (то есть сегодня суббота), ничего не шлём."""
    now = datetime.now(TZ)
    today = now.date()
    day = today + timedelta(days=1)
    if day.weekday() == 6:
        return
    due = due_list(now, "e")
    if not due:
        return
    for _, p, _ in due:
        p["elast"] = today.isoformat()
    save_data()
    try:
        text = await asyncio.to_thread(make_schedule_reply, day)
    except Exception:  # noqa: BLE001
        log.exception("ошибка расписания на завтра")
        for _, p, _ in due:
            p["elast"] = ""
        save_data()
        return
    await broadcast(context.bot, f"🌙 Расписание на завтра\n\n{text}", ids=[cid for cid, _, _ in due])


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
    u["last"] = int((when or msg.date).timestamp())  # когда человек пользовался ботом в последний раз
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
        f"Уведомления о заменах: {len(DATA['subs'])} чел., расписание на день: "
        f"{sum(1 for p in DATA['prefs'].values() if p.get('morning'))} чел., "
        f"расписание на завтра: {sum(1 for p in DATA['prefs'].values() if p.get('evening'))} чел.",
    ]
    await update.message.reply_text("\n".join(lines))


async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/send текст - рассылка всем подписчикам (только для ADMIN_IDS)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(not_admin_text(update))
        return
    parts = re.split(r"\s+", update.message.text.strip(), maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await update.message.reply_text("Напиши текст после команды, например:\n/send Завтра пары в 9:00", reply_markup=KEYBOARD)
        return
    n = len(all_subscribers())
    await update.message.reply_text(f"Отправляю {n} подписчикам…")
    await broadcast(context.bot, f"📢 Сообщение от администратора:\n\n{text}", ids=all_subscribers())
    await update.message.reply_text(f"✅ Готово. Подписчиков сейчас: {len(all_subscribers())} (из {n}).", reply_markup=KEYBOARD)


async def msg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/msg id текст - личное сообщение одному пользователю (только для ADMIN_IDS)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(not_admin_text(update))
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
        await update.message.reply_text(not_admin_text(update))
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
    await update.message.reply_text(f"🚫 Заблокирован: {ban_label(str(uid), DATA['banned'][str(uid)])}\n"
                                    "Снять: /unban id или выдать ему ключ разбана: /unbankey", reply_markup=KEYBOARD)


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unban id - снять блокировку (только для ADMIN_IDS)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(not_admin_text(update))
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
        await update.message.reply_text(not_admin_text(update))
        return
    items = [ban_label(uid, b) for uid, b in list(DATA["banned"].items()) if is_banned(int(uid))]
    await update.message.reply_text("🚫 Заблокированные:\n" + "\n".join(items) if items else "Заблокированных нет.",
                                    reply_markup=KEYBOARD)


def _admin_only(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS


def not_admin_text(update: Update) -> str:
    uid = update.effective_user.id
    if not ADMIN_IDS:
        return (f"⚠️ Переменная ADMIN_IDS не задана на сервере, поэтому админов нет.\n"
                f"Твой id: {uid}. Впиши его в Render → Environment → ADMIN_IDS и перезапусти сервис.")
    return (f"Эта команда только для админа.\nТвой id: {uid}. "
            "Если ты админ, проверь, что этот id есть в ADMIN_IDS на Render.")


def key_card(key: str) -> str:
    k = DATA["access"]["keys"][key]
    if k.get("kind") == "unban":
        return (f"🔓 Ключ разбана: `{fmt_key(key)}`\nСнимает бан или блокировку за спам. "
                "Одноразовый: одна активация.")
    note = "" if DATA["access"]["required"] else "\n\n⚠️ Проверка ключа сейчас выключена. Включить: /access on"
    if not _gist_ready:
        note += "\n\n⚠️ Gist не подключён: после перезапуска Render ключи и доступы пропадут. Нужны GITHUB_TOKEN и GIST_ID."
    return f"🔑 Ключ: `{fmt_key(key)}`\nДоступ: {dur_label(k['dur'])}\nОдноразовый: подходит для одной активации{note}"


def keys_text() -> str:
    acc = DATA["access"]
    lines = [f"Проверка ключа: {'включена' if acc['required'] else 'выключена'}. Людей с доступом: {len(acc['allowed'])}", ""]
    for key, k in sorted(acc["keys"].items(), key=lambda kv: -kv[1]["created"]):
        mark = "☑️" if key_state(k) == "used" else "✅"
        who = f", ввёл {k['users'][0]}" if k["users"] else ""
        what = "ключ разбана" if k.get("kind") == "unban" else f"доступ {dur_label(k['dur'])}"
        lines.append(f"{mark} {fmt_key(key)} — {what}{who}")
    if not acc["keys"]:
        lines.append("Ключей нет. Создать: /key или /admin")
    return "\n".join(lines)[:4000]


def _ago(ts) -> str:
    return f"{datetime.fromtimestamp(ts, TZ):%d.%m %H:%M}" if ts else "—"


def users_pages(only_no_access: bool = False) -> list:
    """Список ВСЕХ пользователей бота (не только с ключом): доступ, уведомления, последняя активность.
    Возвращает список сообщений (Telegram режет длинные)."""
    acc, st, now = DATA["access"], DATA["stats"]["users"], time.time()
    ids = set(known_users()) | {int(u) for u in acc["allowed"] if u.lstrip("-").isdigit()}
    ids |= {int(u) for u in DATA["banned"] if u.lstrip("-").isdigit()} | {u for u in ADMIN_IDS if str(u) in st}
    rows, n_ok, n_no, n_ban, n_adm = [], 0, 0, 0, 0
    for uid in ids:
        key, name = str(uid), st.get(str(uid), {}).get("name") or "без имени"
        a = acc["allowed"].get(key)
        live = bool(a) and (not a["until"] or a["until"] > now)
        banned = is_banned(uid)
        if uid in ADMIN_IDS:
            status, n_adm = "👑 админ", n_adm + 1
        elif banned:
            status, n_ban = "🚫 заблокирован", n_ban + 1
        elif live:
            status, n_ok = "✅ доступ " + until_label(a["until"]), n_ok + 1
        else:
            status, n_no = ("⌛ доступ истёк" if a else "❌ нет доступа"), n_no + 1
        if only_no_access and (uid in ADMIN_IDS or banned or live):
            continue
        pr = DATA["prefs"].get(key, {})
        icons = ("🔔" if uid in DATA["subs"] else "") + ("☀️" if pr.get("morning") else "") + ("🌙" if pr.get("evening") else "")
        last = st.get(key, {}).get("last", 0)
        rows.append((last, f"{uid} — {name}\n   {status} · {icons or 'без уведомлений'} · был: {_ago(last)}"))
    rows.sort(key=lambda r: -r[0])
    head = (f"👥 Пользователей: {len(ids)}  (✅ {n_ok} с доступом, ❌ {n_no} без, 🚫 {n_ban} в бане, 👑 {n_adm} админов)\n"
            "Значки: 🔔 замены, ☀️ расписание на день, 🌙 расписание на завтра\n"
            + ("Показаны только те, у кого нет доступа.\n" if only_no_access else "")
            + f"Проверка ключа: {'включена 🔒' if acc['required'] else 'выключена 🔓'}\n")
    if not rows:
        return [head + "\nСписок пуст."]
    pages, cur = [], head
    for _, line in rows:
        if len(cur) + len(line) + 2 > 3800:
            pages.append(cur)
            cur = ""
        cur += "\n" + line
    pages.append(cur)
    return pages


async def send_pages(bot, chat_id, pages):
    for pg in pages:
        await bot.send_message(chat_id, pg)
        await asyncio.sleep(0.05)


async def key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/key [кол-во] [срок] - создать ключ доступа (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    args = context.args or []
    dur, count = 0, 1
    if args:
        dur = parse_term(args[0])
        if dur is None:
            await update.message.reply_text(
                "Формат: /key [срок доступа] [сколько ключей]\n\n"
                "/key — один ключ, доступ навсегда\n/key 7d — один ключ, доступ 7 дней\n"
                "/key 30d 10 — десять ключей, каждый даёт 30 дней\n/key навсегда 5 — пять ключей, навсегда\n\n"
                "Срок: m — минуты, h — часы, d — дни. Он считается с момента, когда человек ввёл ключ. "
                "Каждый ключ одноразовый. Или нажми /admin — там кнопки.", reply_markup=KEYBOARD)
            return
    if len(args) > 1:
        if not args[1].isdigit() or not 1 <= int(args[1]) <= 30:
            await update.message.reply_text("Сколько ключей — число от 1 до 30.", reply_markup=KEYBOARD)
            return
        count = int(args[1])
    if count == 1:
        await update.message.reply_text(key_card(create_key(1, dur)), parse_mode="Markdown", reply_markup=KEYBOARD)
        return
    keys = [create_key(1, dur) for _ in range(count)]
    note = "" if DATA["access"]["required"] else "\n\n⚠️ Проверка ключа сейчас выключена. Включить: /access on"
    await update.message.reply_text(
        f"🔑 Ключей: {count}, доступ по каждому: {dur_label(dur)}. Каждый одноразовый.\n\n"
        + "\n".join(f"`{fmt_key(k)}`" for k in keys) + note, parse_mode="Markdown", reply_markup=KEYBOARD)


async def unbankey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unbankey [кол-во] - ключи разбана (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    args = context.args or []
    count = 1
    if args:
        if not args[0].isdigit() or not 1 <= int(args[0]) <= 30:
            await update.message.reply_text("Сколько ключей — число от 1 до 30. Пример: /unbankey 3", reply_markup=KEYBOARD)
            return
        count = int(args[0])
    keys = [create_key(1, 0, "unban") for _ in range(count)]
    await update.message.reply_text(
        f"🔓 Ключей разбана: {count}. Каждый одноразовый, снимает бан, блокировку за спам или мут.\n\n"
        + "\n".join(f"`{fmt_key(k)}`" for k in keys), parse_mode="Markdown", reply_markup=KEYBOARD)


async def grantall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/grantall срок [notify] - выдать и сразу активировать доступ всем, кто пользовался ботом (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    args = [a.lower() for a in (context.args or [])]
    notify = any(a in ("notify", "уведомить", "msg") for a in args)
    args = [a for a in args if a not in ("notify", "уведомить", "msg")]
    dur = parse_term(args[0]) if args else None
    if dur is None:
        todo, already, banned = grant_targets()
        await update.message.reply_text(
            "Формат: /grantall срок [notify]\n\n"
            "/grantall навсегда — доступ навсегда всем, кто пользовался ботом\n"
            "/grantall 30d — на 30 дней (m — минуты, h — часы, d — дни)\n"
            "/grantall 30d notify — то же + каждому придёт короткое сообщение «доступ открыт»\n\n"
            "Ключи не нужны: доступ записывается и работает сразу. У кого доступ уже есть и у заблокированных — пропускаю.\n\n"
            f"Сейчас получили бы доступ: {len(todo)} чел. (уже есть: {len(already)}, в бане: {len(banned)}).",
            reply_markup=KEYBOARD)
        return
    todo, already, banned = grant_all(dur)
    await update.message.reply_text(grant_report(todo, already, banned, dur), reply_markup=KEYBOARD)
    if notify:
        await notify_granted(context.bot, todo, dur)


def grant_report(todo, already, banned, dur) -> str:
    text = (f"✅ Доступ выдан и активирован: {len(todo)} чел. ({dur_label(dur)}).\n"
            f"Уже был доступ (не менял): {len(already)}. Заблокированы (пропущены): {len(banned)}.")
    if DATA["access"]["required"]:
        text += "\n\nПользоваться ботом они могут прямо сейчас, вводить ключ не нужно."
    else:
        text += "\n\nℹ️ Проверка ключа сейчас выключена. Доступ уже записан и начнёт работать после /access on."
    return text


async def notify_granted(bot, ids, dur):
    for uid in ids:
        until = DATA["access"]["allowed"].get(str(uid), {}).get("until", 0)
        await _safe_send(bot, uid, f"✅ Тебе открыт доступ к боту ({until_label(until)}). Вводить ключ не нужно, пользуйся!",
                         KEYBOARD)
        await asyncio.sleep(0.05)


async def keys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/keys - список ключей (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    await update.message.reply_text(keys_text(), reply_markup=KEYBOARD)


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/users [no] - все пользователи бота: доступ, уведомления, активность (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    args = [a.lower() for a in (context.args or [])]
    only_no = bool(args) and args[0] in ("no", "noaccess", "без", "нет")
    await send_pages(context.bot, update.effective_chat.id, users_pages(only_no))


async def delkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delkey КЛЮЧ - удалить ключ (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Напиши ключ: /delkey ABCD-1234", reply_markup=KEYBOARD)
        return
    if DATA["access"]["keys"].pop(norm_key(" ".join(args)), None) is None:
        await update.message.reply_text("Такого ключа нет. Список: /keys", reply_markup=KEYBOARD)
        return
    save_data()
    await update.message.reply_text("🗑 Ключ удалён. Кто уже ввёл его, доступ сохраняет (забрать: /revoke id).",
                                    reply_markup=KEYBOARD)


async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/access on|off - включить/выключить проверку ключа (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    args = [a.lower() for a in (context.args or [])]
    if not args or args[0] not in ("on", "off", "вкл", "выкл"):
        await update.message.reply_text(
            f"Проверка ключа сейчас: {'включена' if DATA['access']['required'] else 'выключена'}.\n\n"
            "/access on — включить (у кого уже есть ключ, не спросит; кто пользовался без ключа, тому ключ нужен)\n"
            "/access on all — включить и сбросить доступ вообще у всех, даже у тех, кто вводил ключ (кроме админов)\n"
            "/access off — выключить, бот открыт для всех", reply_markup=KEYBOARD)
        return
    on = args[0] in ("on", "вкл")
    strict = len(args) > 1 and args[1] in ("all", "все", "всех")
    await update.message.reply_text(set_access(on, strict), reply_markup=KEYBOARD)


async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/revoke id - забрать доступ у пользователя (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Напиши id: /revoke 123456789", reply_markup=KEYBOARD)
        return
    if DATA["access"]["allowed"].pop(str(int(args[0])), None) is None:
        await update.message.reply_text("У этого id нет сохранённого доступа. Список: /users", reply_markup=KEYBOARD)
        return
    set_sub(int(args[0]), False)
    await update.message.reply_text(f"🚫 Доступ у {args[0]} забран (если проверка ключа включена, ему снова нужен ключ).",
                                    reply_markup=KEYBOARD)


async def mykey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mykey - свой статус доступа."""
    uid, acc = update.effective_user.id, DATA["access"]
    if uid in ADMIN_IDS:
        text = "👑 Ты админ: ключ тебе не нужен."
    elif not acc["required"]:
        text = "Проверка ключа сейчас выключена, бот открыт для всех."
    else:
        text = f"🔑 Твой доступ: {until_label(acc['allowed'][str(uid)]['until'])}.\nПродлить: отправь боту новый ключ."
    await update.message.reply_text(text, reply_markup=KEYBOARD)


# ---------- Панель админа (кнопки) ----------

def admin_markup() -> InlineKeyboardMarkup:
    on, B = DATA["access"]["required"], InlineKeyboardButton
    return InlineKeyboardMarkup([
        [B("🔑 На 1 день", callback_data="adm:key:1d"), B("🔑 На 7 дней", callback_data="adm:key:7d")],
        [B("🔑 На 30 дней", callback_data="adm:key:30d"), B("🔑 Навсегда", callback_data="adm:key:0")],
        [B("🔓 Ключ разбана", callback_data="adm:unban")],
        [B("🎁 Выдать доступ всем", callback_data="adm:ga")],
        [B("📋 Ключи", callback_data="adm:keys"), B("👥 Пользователи", callback_data="adm:users")],
        [B("🔓 Выключить проверку ключа" if on else "🔒 Включить проверку ключа", callback_data="adm:toggle")],
    ])


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin - панель с кнопками (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    on = DATA["access"]["required"]
    await update.message.reply_text(
        f"🛠 Панель администратора\nПроверка ключа: {'включена 🔒' if on else 'выключена 🔓'}\n"
        f"Сохранение данных: {'авто в GitHub Gist ✅' if _gist_ready else 'только диск ⚠️ (Render может стереть)'}\n\n"
        "Кнопки «🔑» создают одноразовый ключ. Сразу несколько: /key 7d 10",
        reply_markup=admin_markup())


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:  # кнопки видит только админ, но проверяем и здесь
        await q.answer("Только для админа", show_alert=True)
        return
    await q.answer()
    action, chat_id = q.data.split(":", 1)[1], q.message.chat_id
    if action.startswith("key:"):
        dur = parse_term(action[4:])
        if dur is not None:
            await context.bot.send_message(chat_id, key_card(create_key(1, dur)), parse_mode="Markdown")
    elif action == "unban":
        await context.bot.send_message(chat_id, key_card(create_key(1, 0, "unban")), parse_mode="Markdown")
    elif action == "keys":
        await context.bot.send_message(chat_id, keys_text())
    elif action == "users":
        await send_pages(context.bot, chat_id, users_pages())
    elif action == "ga":  # шаг 1: подтверждение
        todo, already, banned = grant_targets()
        B = InlineKeyboardButton
        await context.bot.send_message(
            chat_id,
            f"🎁 Выдать доступ всем, кто пользовался ботом, без ключей?\nПолучат: {len(todo)} чел. "
            f"(уже есть доступ: {len(already)}, в бане: {len(banned)}).\nВыбери срок:",
            reply_markup=InlineKeyboardMarkup([
                [B("30 дней", callback_data="adm:gado:30d"), B("Навсегда", callback_data="adm:gado:0")],
                [B("🔔 30 дней + уведомить", callback_data="adm:gado:30dn"), B("🔔 Навсегда + уведомить", callback_data="adm:gado:0n")],
                [B("↩️ Отмена", callback_data="adm:gax")]]))
    elif action == "gax":
        await q.message.edit_text("Отменено.")
    elif action.startswith("gado:"):  # шаг 2: выдача
        term = action[5:]
        notify = term.endswith("n")
        dur = parse_term(term[:-1] if notify else term)
        if dur is None:
            return
        todo, already, banned = grant_all(dur)
        await q.message.edit_text(grant_report(todo, already, banned, dur))
        if notify:
            await notify_granted(context.bot, todo, dur)
    elif action == "toggle":
        text = set_access(not DATA["access"]["required"])
        try:
            await q.message.edit_reply_markup(reply_markup=admin_markup())
        except BadRequest:
            pass
        await context.bot.send_message(chat_id, text)


# ---------- Резервная копия данных ----------
# На бесплатном Render диск стирается при перезапуске: копия в Telegram позволяет вернуть ключи и подписчиков.

BACKUP_CAPTION = "💾 Резервная копия данных бота. Восстановить: отправь этот файл боту с подписью /restore"


async def send_backup(bot, chat_id):
    save_data()
    bio = io.BytesIO(json.dumps(DATA, ensure_ascii=False, indent=1).encode("utf-8"))
    name = f"bot_data_{datetime.now(TZ):%Y-%m-%d_%H%M}.json"
    await bot.send_document(chat_id, bio, filename=name, caption=BACKUP_CAPTION)


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/backup - прислать файл с данными (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    await send_backup(context.bot, update.effective_chat.id)


async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    for admin in ADMIN_IDS:
        try:
            await send_backup(context.bot, admin)
        except Exception:  # noqa: BLE001
            log.exception("не удалось отправить резервную копию %s", admin)


async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Файл резервной копии с подписью /restore - вернуть данные (только для ADMIN_IDS)."""
    if not _admin_only(update):
        await update.message.reply_text(not_admin_text(update))
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 5_000_000:
        await update.message.reply_text("Файл слишком большой для копии бота.", reply_markup=KEYBOARD)
        return
    try:
        raw = bytes(await (await doc.get_file()).download_as_bytearray())
        new = json.loads(raw.decode("utf-8"))
        if not isinstance(new, dict) or not ({"subs", "access", "stats"} & set(new)):
            raise ValueError("не похоже на копию бота")
    except Exception:  # noqa: BLE001
        await update.message.reply_text("❌ Не получилось прочитать файл. Нужен файл, который прислал сам бот (/backup).",
                                        reply_markup=KEYBOARD)
        return
    fix_data(new)
    DATA.clear()
    DATA.update(new)
    save_data()
    await update.message.reply_text(
        f"✅ Данные восстановлены.\nПодписчиков: {len(DATA['subs'])}, людей с доступом: {len(DATA['access']['allowed'])}, "
        f"ключей: {len(DATA['access']['keys'])}.", reply_markup=KEYBOARD)


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


def start_note(uid: int) -> str:
    """Приписка к приветствию: до какого времени доступ (или подсказка админу)."""
    acc = DATA["access"]
    if uid in ADMIN_IDS:
        return "\n\n🛠 Админ: /admin — панель ключей, /key — создать ключ."
    a = acc["allowed"].get(str(uid))
    if acc["required"] and a and a["until"]:
        return f"\n\n🔑 Доступ {until_label(a['until'])}. Продлить: отправь боту новый ключ."
    return ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_sub(update.effective_chat.id, True)
    await update.message.reply_text(
        "«Замены» — замены на завтра. «Есть ли замены?» — выложены ли они. "
        "«Пары завтра» — расписание с учётом замен.\n"
        "«📅 Расписание» — расписание на любой день недели (Пн–Сб), там же можно глянуть замены.\n"
        "Можно уточнять день: «Пары пятница», «Пары пн», «Замены 05.10», «Пары послезавтра».\n\n"
        f"🔔 Я включил уведомления: напишу сам, когда выложат замены, в {get_pref(update.effective_chat.id)['time']} "
        f"пришлю расписание на день, а в {get_pref(update.effective_chat.id)['etime']} — на завтра. "
        "Настроить (замены, расписания, время) и написать админу, если что-то не так, — кнопка «🔔 Уведомления». "
        "Группа " + GROUP_NAME + start_note(update.effective_user.id),
        reply_markup=KEYBOARD,
    )


_last_err = [0.0]


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка в обработчике", exc_info=context.error)
    now = time.time()
    if now - _last_err[0] < 30:  # не чаще раза в 30 секунд
        return
    _last_err[0] = now
    cmd = ""
    if isinstance(update, Update) and update.effective_message and update.effective_message.text:
        cmd = f" при «{update.effective_message.text[:40]}»"
    for admin in ADMIN_IDS:
        await _safe_send(context.bot, admin, f"🐞 Ошибка бота{cmd}: {context.error!r}"[:500])


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())  # нужно для Python 3.14
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: никто не сможет создавать ключи и пользоваться админ-командами")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(MessageHandler(filters.ALL, antispam), group=-2)  # проверка на спам идёт первой
    app.add_handler(TypeHandler(Update, access_gate), group=-1)        # потом проверка ключа доступа
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
    app.add_handler(CommandHandler("key", key_cmd))
    app.add_handler(CommandHandler("keys", keys_cmd))
    app.add_handler(CommandHandler(["grantall", "giveall"], grantall_cmd))
    app.add_handler(CommandHandler("unbankey", unbankey_cmd))
    app.add_handler(CommandHandler("delkey", delkey_cmd))
    app.add_handler(CommandHandler("access", access_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("mykey", mykey_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"(?i)^/restore"), restore_cmd))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(notif_cb, pattern=r"^ntf:"))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(🔔\s*)?уведомлен"), toggle_sub))
    app.add_handler(MessageHandler(filters.Regex(r"^\s*\d{1,2}(\s*[:.\-]\s*\d{2})?\s*$") & _AwaitTime(), time_input))  # время текстом
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*есть ли замен"), check_changes))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*пары"), lessons_day))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(📅\s*)?расписан"), schedule_button))
    app.add_handler(CallbackQueryHandler(schedule_cb, pattern=r"^sch:"))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(замен|debug)"), zameny))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(привет|здаров|здравствуй|хай|ку)\b"), talk(GREET)))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(спасибо|благодарю|спс|сяп)"), talk(THANKS)))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(пока|до свидания|бывай)\b"), talk(BYE)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk(UNKNOWN)))  # всё остальное
    app.add_error_handler(on_error)
    if app.job_queue is None:
        log.warning("JobQueue недоступен: установите python-telegram-bot[job-queue,webhooks]")
    else:
        app.job_queue.run_repeating(watch_job, interval=CHECK_FAST, first=30)
        app.job_queue.run_repeating(morning_job, interval=60, first=20)  # расписание на день, у каждого своё время
        app.job_queue.run_repeating(evening_job, interval=60, first=25)  # расписание на завтра, у каждого своё время
        app.job_queue.run_repeating(access_job, interval=60, first=45)          # конец доступа по ключу
        app.job_queue.run_daily(backup_job, time=dtime(3, 0, tzinfo=TZ))        # копия данных админам
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=BOT_TOKEN,
        webhook_url=f"{os.environ['RENDER_EXTERNAL_URL']}/{BOT_TOKEN}",
    )


if __name__ == "__main__":
    main()

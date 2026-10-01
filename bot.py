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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pdfplumber
import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
DISK_URL = os.environ.get("DISK_URL", "https://disk.yandex.by/d/mfUQ5pAX_ScALw")
GROUP_NAME = os.environ.get("GROUP_NAME", "01-24")  # как в блоке "Информационный час"
GROUP_CODE = os.environ.get("GROUP_CODE", "124")    # как в первой колонке таблицы

TZ = ZoneInfo("Europe/Minsk")
API = "https://cloud-api.yandex.net/v1/disk/public/resources"
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
SKIP = {"√", "✓"}  # значок "как выше"
KEYBOARD = ReplyKeyboardMarkup([["Замены"]], resize_keyboard=True, is_persistent=True)  # кнопка внизу

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
        label = f"Пара {nums[0]}" if len(nums) == 1 else f"Пары {nums[0]}–{nums[-1]}"
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
        day = today + timedelta(days=1)
    debug = text.lower().lstrip().startswith("debug")
    try:
        reply = await asyncio.to_thread(make_reply, day, debug)
    except Exception as e:  # noqa: BLE001
        log.exception("ошибка")
        reply = f"Не получилось разобрать замены: {e}"
    await update.message.reply_text(reply[:4000], reply_markup=KEYBOARD)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Нажмите кнопку «Замены» внизу, и я покажу замены на завтра для " + GROUP_NAME,
        reply_markup=KEYBOARD,
    )


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())  # нужно для Python 3.14
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*(замен|debug)"), zameny))
    app.run_polling()


if __name__ == "__main__":
    main()

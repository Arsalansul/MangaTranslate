"""Веб-интерфейс к главе: запустить прогон, посмотреть результат, поправить
перевод и пересобрать страницу.

Приёмка главы до сих пор выглядела как щёлканье по PNG в проводнике и правка
`translation` в analysis.json блокнотом. Здесь то же самое, но рядом: список
страниц, оригинал против результата и поле ввода возле каждой реплики.

Прогон здесь же, но сервер сам ничего не переводит и не верстает: он держит
одну задачу (`Job`) и зовёт готовые куски run.py — resolve, prepare,
run_chapter, process. Дублировать их логику нельзя: она разъедется с
командной строкой в первый же день.

Задача ровно одна за раз. Photoshop монопольный, и очередь означала бы, что
вторая глава молча ждёт полчаса; отказать сразу честнее.

Только stdlib, как и весь host/: проект отдаётся человеку, у которого есть
Photoshop и Python, и больше ничего ставить он не должен.
"""
import argparse
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge
import envkey
import fontcheck
import project
import run
import translate

HERE = os.path.dirname(os.path.abspath(__file__))
# 8765 занят CV-контейнером, берём соседний.
PORT = 8766
# Тело POST — это правки одной страницы, десятки коротких строк.
MAX_BODY = 1 << 20

# Через /api/img не должно уехать ничего, кроме картинки: расширение решает,
# можно ли файл отдавать вообще, а тип берётся из первых байтов — в главах
# сплошь и рядом лежат JPEG с расширением .png, и заявленный тип врал бы.
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
         (b"RIFF", "image/webp"))

# Всё состояние сервера — здесь. Открытая глава одна: инструмент для одного
# человека за одним столом, вторая вкладка с другой главой смысла не имеет.
STATE = {"chapter": "", "out_dir": "", "font": "", "engine": "",
         "lang": translate.DEFAULT_SOURCE, "target_lang": translate.DEFAULT_TARGET,
         "pages": []}

# Заголовок страницы из run_chapter: "[3/19] 0003.png". Единственное место,
# где прогон сообщает, на чём он сейчас, — своей же строкой лога. Если формат
# изменится, счётчик замрёт, но лог и состояние останутся верными.
PAGE_LINE = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S.*)$")


class ApiError(Exception):
    """Ошибка, которую честно показываем в браузере, а не прячем в 500."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


# --- задача -----------------------------------------------------------

class Job:
    """Прогон, который идёт прямо сейчас, и всё, что о нём знает браузер.

    Хранит лог строками с номерами, а не одним текстом: браузер спрашивает
    «что нового после N-й» и получает хвост, а не всю простыню каждую
    секунду. Номер строки и есть курсор.

    SSE здесь нарочно нет. События приходят раз в десятки секунд, снапшот
    состояния всё равно нужен на перезагрузку страницы, а повисший
    SSE-обработчик держал бы поток до первой неудачной записи в сокет.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self._clear("idle", "", "")

    def _clear(self, state, kind, title):
        self.state = state          # idle / running / done / failed / cancelled
        self.kind = kind            # chapter / retypeset
        self.title = title
        self.lines = []
        self.chapter = ""
        self.out_dir = ""
        self.total = 0
        self.done = 0
        self.page = ""
        self.stop_reason = ""       # почему кончилось раньше запрошенного
        self.error = ""
        self.cancel = False
        self.started = time.time()
        self.finished = 0.0

    # --- то, что видит run.py ------------------------------------------

    def report(self, msg, stage=None, quiet=False, **kw):
        """Ровно та же форма, что у run._plain: подставляется вместо print.

        `**kw` не про запас: run.py оставил его открытым, и добавленный там
        именованный аргумент иначе уронил бы главу посреди прогона.
        """
        text = str(msg)
        with self.lock:
            for line in (text.splitlines() or [""]):
                self._line(line, stage)
            if stage == "page":
                m = PAGE_LINE.match(text)
                if m:
                    # Страница только началась: готовых по-прежнему i-1.
                    self.done, self.page = int(m.group(1)) - 1, m.group(3)
            elif stage == "summary":
                # Единственный канал, которым run_chapter объясняет досрочный
                # конец (три молчания модели подряд, отмена). Без него
                # «запрошено 20, готово 6, состояние: готово» читается как баг.
                self.stop_reason = " ".join(text.split())

    def stopper(self):
        return lambda: self.cancel

    def _line(self, text, stage):
        """Только под self.lock: номер строки обязан быть её же индексом."""
        self.lines.append({"n": len(self.lines), "text": text,
                           "stage": stage or ""})

    # --- то, что видит браузер -----------------------------------------

    def snapshot(self, since=0):
        with self.lock:
            since = max(0, min(int(since), len(self.lines)))
            return {
                "state": self.state, "kind": self.kind, "title": self.title,
                "chapter": self.chapter, "out": self.out_dir,
                "total": self.total, "done": self.done, "page": self.page,
                "stop_reason": self.stop_reason, "error": self.error,
                "elapsed": int((self.finished or time.time()) - self.started),
                "cancelling": self.cancel and self.state == "running",
                "cursor": len(self.lines), "lines": self.lines[since:],
            }

    def start(self, kind, title, work):
        with self.lock:
            if self.state == "running":
                raise ApiError(409, "Уже идёт задача: %s. Дождитесь конца "
                                    "или отмените." % self.title)
            self._clear("running", kind, title)
        self.thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self.thread.start()
        return self.snapshot()

    def request_cancel(self):
        with self.lock:
            if self.state != "running":
                raise ApiError(409, "Сейчас ничего не идёт")
            self.cancel = True
            self._line("отмена запрошена: дорабатываю текущий шаг", "summary")
        return self.snapshot()

    def _run(self, work):
        try:
            work()
        except SystemExit as e:
            # resolve() и prepare() бросают именно его, а SystemExit — не
            # наследник Exception. Без отдельной ветки поток умирал бы молча,
            # и задача навсегда осталась бы «в работе».
            self._finish("failed", str(e) or "прогон остановлен")
        except run.Cancelled:
            # run_chapter ловит отмену сам, но process() зовётся и напрямую
            # (пересборка страницы) — там она долетает сюда.
            self._finish("cancelled", "")
        except Exception as e:
            self._finish("failed", "%s: %s" % (type(e).__name__, e))
        else:
            self._finish("cancelled" if self.cancel else "done", "")

    def _finish(self, state, error):
        with self.lock:
            for line in (error.splitlines() if error else []):
                self._line(line, "error")
            self.state, self.error, self.finished = state, error, time.time()


JOB = Job()


def _idle():
    """Пока идёт задача, писать в analysis.json нельзя.

    Прогон сохраняет тот же файл сам (_save после перевода и перед
    Photoshop). Правка, прилетевшая в середине, была бы затёрта через
    секунду — и человек об этом не узнал бы.
    """
    if JOB.state == "running":
        raise ApiError(409, "Идёт задача: %s. Правка будет затёрта прогоном — "
                            "дождитесь конца или отмените." % JOB.title)


# --- доступ к диску ---------------------------------------------------
# Единственный способ добраться до файла: корень берётся из состояния
# сервера, от браузера приходит голое имя. Никакой склейки пути из
# пользовательской строки, иначе /api/img превращается в чтение диска.

def _inside(root, name):
    if not isinstance(name, str) or not name or name != os.path.basename(name):
        raise ApiError(400, "Ожидается имя файла, а не путь: %r" % (name,))
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, name))
    if path != root and not path.startswith(root + os.sep):
        raise ApiError(400, "Файл вне рабочей папки: %r" % (name,))
    if not os.path.isfile(path):
        raise ApiError(404, "Нет файла: " + name)
    return path


def _opened():
    if not STATE["out_dir"]:
        raise ApiError(400, "Глава не открыта: сначала /api/open")


# --- разбор главы -----------------------------------------------------

def _rows(report, chapter, out_dir):
    """Карточки страниц: что в отчёте плюс то, что реально лежит на диске."""
    rows = []
    for row in report.get("pages", []):
        name = row.get("page") or ""
        stem = os.path.splitext(name)[0]
        out_name = stem + ".png"
        rows.append({
            "stem": stem,
            "src": name if os.path.isfile(os.path.join(chapter, name)) else None,
            "out": out_name if os.path.isfile(os.path.join(out_dir, out_name)) else None,
            "editable": os.path.isfile(os.path.join(out_dir, stem + ".analysis.json")),
            "regions": int(row.get("regions") or 0),
            "translated": int(row.get("translated") or 0),
            "ok": bool(row.get("ok")),
            "note": row.get("note") or "",
        })
    return rows


def _open(out_dir):
    """Отчёт знает обе папки, поэтому от человека нужен ровно один путь."""
    out_dir = run._norm(out_dir)
    path = os.path.join(out_dir, "run.report.json")
    if not os.path.isfile(path):
        raise ApiError(400, "В папке нет run.report.json: " + out_dir)
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    chapter = run._norm(report.get("chapter") or "")
    if not os.path.isdir(chapter):
        # Исходники могли переехать: результат смотреть всё равно можно,
        # просто не с чем сравнивать.
        chapter = ""
    STATE.update({"chapter": chapter, "out_dir": out_dir,
                  "font": report.get("font") or "", "engine": report.get("engine") or "",
                  # lang и target_lang в отчёте появились недавно: в главах,
                  # прогнанных раньше, их нет, а пересборке они нужны.
                  "lang": _known(report.get("lang"), translate.SOURCES, translate.DEFAULT_SOURCE),
                  "target_lang": _known(report.get("target_lang"), translate.TARGETS,
                                        translate.DEFAULT_TARGET),
                  "pages": _rows(report, chapter, out_dir)})
    return _chapter()


def _known(value, table, fallback):
    return value if value in table else fallback


def _chapter():
    return {"chapter": STATE["chapter"], "out": STATE["out_dir"],
            "font": STATE["font"], "engine": STATE["engine"],
            "lang": STATE["lang"], "target_lang": STATE["target_lang"],
            "fonts": _font_names(), "pages": STATE["pages"]}


def _row(stem):
    for row in STATE["pages"]:
        if row["stem"] == stem:
            return row
    raise ApiError(404, "Нет такой страницы в главе: " + str(stem))


def _read(stem):
    """Читаем с диска каждый раз: файл правят и снаружи, кэш бы врал."""
    path = _inside(STATE["out_dir"], stem + ".analysis.json")
    with open(path, encoding="utf-8") as f:
        return path, json.load(f)


def _brief(r):
    """Урезанный регион: mask_polys — сотни координат, браузеру они не нужны."""
    return {
        "id": r.get("id"),
        "bbox": r.get("bbox") or [0, 0, 0, 0],
        "safe_box": r.get("safe_box") or r.get("bbox") or [0, 0, 0, 0],
        "kind": r.get("kind") or "unknown",
        "keep_lines": bool(r.get("keep_lines")),
        "text": r.get("text") or "",
        "translation": r.get("translation") or "",
        "font": r.get("font") or "",
        "font_px": int(r.get("font_px") or 0),
        "line_h_px": int(r.get("line_h_px") or 0),
        "fg": r.get("fg") or [0, 0, 0],
        "bg": r.get("bg") or [255, 255, 255],
        "on_art": bool(r.get("on_art")),
        "erase_only": bool(r.get("erase_only")),
        "manual_text": bool(r.get("manual_text")),
        "typeset_size": r.get("typeset_size"),
        "typeset_leading": r.get("typeset_leading"),
        "typeset_align": r.get("typeset_align") or "",
        "typeset_color": r.get("typeset_color"),
        "typeset_bold": bool(r.get("typeset_bold")),
        "typeset_italic": bool(r.get("typeset_italic")),
        "typeset_underline": bool(r.get("typeset_underline")),
        "typeset_effects": r.get("typeset_effects") or ([{
            "stroke": r.get("typeset_stroke"), "gradient": r.get("typeset_gradient"),
            "shadow": r.get("typeset_shadow")
        }] if r.get("typeset_stroke") or r.get("typeset_gradient") or r.get("typeset_shadow") else []),
        "typeset_blur": r.get("typeset_blur"),
        "conf": round(float(r.get("conf") or 0.0), 2),
        # Правило «что вообще переводится» живёт в translate.py в одном
        # экземпляре; повторять его в JavaScript нельзя — разъедется.
        "translatable": translate.translatable(r),
    }


def _page(stem, analysis):
    row = _row(stem)
    regions = [_brief(r) for r in analysis.get("regions", [])]
    targets = [r for r in regions if r["translatable"]]
    done = sum(1 for r in targets if r["translation"].strip())
    # Счётчики в отчёте — на момент прогона; после правки они врут.
    row["regions"], row["translated"] = len(regions), done
    # PNG мог появиться уже после открытия главы — его только что собрала
    # пересборка страницы. Один stat дешевле, чем показывать «нет файла».
    out_name = stem + ".png"
    out_path = os.path.join(STATE["out_dir"], out_name)
    row["out"] = out_name if os.path.isfile(out_path) else None
    # Пересборка кладёт PNG под тем же именем. Без метки времени в адресе
    # браузер показал бы прежнюю картинку из кэша — и правка выглядела бы
    # как не подействовавшая.
    stamp = int(os.path.getmtime(out_path)) if row["out"] else 0
    return {"stem": stem, "src": row["src"], "out": row["out"], "stamp": stamp,
            "width": int(analysis.get("width") or 0),
            "height": int(analysis.get("height") or 0),
            "warnings": analysis.get("warnings") or [],
            "targets": len(targets), "translated": done, "regions": regions}


# --- маршруты ---------------------------------------------------------

def api_open(body):
    out_dir = body.get("out_dir")
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise ApiError(400, "Нужен out_dir — папка с результатом")
    return _open(out_dir.strip())


def api_pages(query):
    _opened()
    return _chapter()


def api_page(query):
    _opened()
    stem = _one(query, "stem")
    return _page(stem, _read(stem)[1])


def api_save(body):
    """Пишем переводы и вручную поправленные области текста."""
    _opened()
    _idle()
    stem = body.get("stem")
    if not isinstance(stem, str):
        raise ApiError(400, "Нужен stem")
    edits = body.get("edits")
    if not isinstance(edits, dict):
        raise ApiError(400, "edits должен быть объектом {id: перевод}")
    positions = body.get("positions", {})
    if not isinstance(positions, dict):
        raise ApiError(400, "positions должен быть объектом {id: [x, y, w, h]}")
    fonts = body.get("fonts", {})
    if not isinstance(fonts, dict):
        raise ApiError(400, "fonts должен быть объектом {id: шрифт}")
    styles = body.get("styles", {})
    if not isinstance(styles, dict):
        raise ApiError(400, "styles должен быть объектом {id: оформление}")
    creates = body.get("creates", [])
    deletes = body.get("deletes", [])
    if not isinstance(creates, list) or not isinstance(deletes, list):
        raise ApiError(400, "creates и deletes должны быть массивами")

    path, analysis = _read(stem)
    page_w, page_h = int(analysis.get("width") or 0), int(analysis.get("height") or 0)
    used = {r.get("id") for r in analysis.get("regions", [])}
    for item in creates:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ApiError(400, "У новой области нужны id и bbox")
        rid, box = item["id"], item.get("bbox")
        if rid in used or not rid.startswith("manual-"):
            raise ApiError(400, "Некорректный или занятый id новой области: " + rid)
        if (not isinstance(box, list) or len(box) != 4 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in box)):
            raise ApiError(400, "bbox новой области должен быть [x, y, w, h]")
        x, y, w, h = [int(round(v)) for v in box]
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > page_w or y + h > page_h:
            raise ApiError(400, "Новая область выходит за границы страницы: " + rid)
        cleanup = item.get("erase_only", True)
        if not isinstance(cleanup, bool):
            raise ApiError(400, "erase_only новой области должен быть true или false")
        analysis.setdefault("regions", []).append({
            "id": rid, "bbox": [x, y, w, h], "safe_box": [x, y, w, h],
            "mask_poly": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
            "text": "", "translation": "", "kind": "cleanup" if cleanup else "unknown",
            "erase_only": cleanup, "manual_text": not cleanup,
            # Ручная область неизвестна детектору: безопаснее инпейнт, чем
            # залить её белым и уничтожить рисунок.
            "on_art": True, "font_px": 0, "line_h_px": 0,
            "fg": [0, 0, 0], "bg": [255, 255, 255], "conf": 1.0,
        })
        used.add(rid)
    by_id = {r.get("id"): r for r in analysis.get("regions", [])}
    saved, unknown = [], []
    for rid, text in edits.items():
        if not isinstance(text, str):
            raise ApiError(400, "Перевод должен быть строкой: " + str(rid))
        region = by_id.get(rid)
        if region is None:
            # Разметку могли перегенерировать, пока страница висела открытой.
            # Чужой id — повод сказать об этом, а не уронить сохранение.
            unknown.append(rid)
            continue
        # Пустая строка — осмысленное значение: регион не будет стёрт вовсе.
        region["translation"] = text
        saved.append(rid)
    for rid, box in positions.items():
        region = by_id.get(rid)
        if region is None:
            if rid not in unknown:
                unknown.append(rid)
            continue
        if (not isinstance(box, list) or len(box) != 4 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in box)):
            raise ApiError(400, "Позиция должна быть [x, y, w, h]: " + str(rid))
        x, y, w, h = [int(round(v)) for v in box]
        if w <= 0 or h <= 0:
            raise ApiError(400, "Ширина и высота позиции должны быть положительными: " + str(rid))
        # Рамку целиком держим на странице: случайный рывок мыши не должен
        # унести текст за холст и сделать его недоступным для следующей правки.
        w, h = min(w, int(analysis.get("width") or w)), min(h, int(analysis.get("height") or h))
        x = max(0, min(x, int(analysis.get("width") or (x + w)) - w))
        y = max(0, min(y, int(analysis.get("height") or (y + h)) - h))
        region["safe_box"] = [x, y, w, h]
        if rid not in saved:
            saved.append(rid)
    available = set(_font_names())
    for rid, font in fonts.items():
        region = by_id.get(rid)
        if region is None:
            if rid not in unknown:
                unknown.append(rid)
            continue
        if not isinstance(font, str):
            raise ApiError(400, "Шрифт должен быть строкой: " + str(rid))
        if font and font not in available:
            raise ApiError(400, "Нет такого доступного шрифта: " + font)
        if font:
            region["font"] = font
        else:
            region.pop("font", None)
        if rid not in saved:
            saved.append(rid)
    for rid, style in styles.items():
        region = by_id.get(rid)
        if region is None:
            if rid not in unknown:
                unknown.append(rid)
            continue
        if not isinstance(style, dict):
            raise ApiError(400, "Оформление должно быть объектом: " + str(rid))
        size, leading = style.get("size"), style.get("leading")
        align, color = style.get("align"), style.get("color")
        bold, italic = style.get("bold", False), style.get("italic", False)
        underline = style.get("underline", False)
        effects = style.get("effects", [])
        blur = style.get("blur")
        for field, value in (("size", size), ("leading", leading)):
            if value is not None and (isinstance(value, bool) or
                                      not isinstance(value, (int, float)) or value <= 0 or value > 1000):
                raise ApiError(400, "%s должен быть числом от 1 до 1000: %s" % (field, rid))
        if align not in (None, "", "left", "center", "right"):
            raise ApiError(400, "Неизвестное выравнивание: " + str(align))
        if color is not None and (not isinstance(color, list) or len(color) != 3 or
                                  any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                                      v < 0 or v > 255 for v in color)):
            raise ApiError(400, "Цвет должен быть RGB [0..255]: " + str(rid))
        if any(not isinstance(v, bool) for v in (bold, italic, underline)):
            raise ApiError(400, "Жирный, курсив и подчёркивание должны быть true/false: " + str(rid))
        def rgb(value, field):
            if (not isinstance(value, list) or len(value) != 3 or
                    any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                        v < 0 or v > 255 for v in value)):
                raise ApiError(400, "%s должен быть RGB [0..255]: %s" % (field, rid))
            return [int(round(v)) for v in value]
        if not isinstance(effects, list) or len(effects) > 8:
            raise ApiError(400, "Эффекты должны быть массивом максимум из 8 блоков: " + str(rid))
        clean_effects = []
        for effect in effects:
            if not isinstance(effect, dict):
                raise ApiError(400, "Блок эффектов должен быть объектом: " + str(rid))
            stroke, gradient, shadow = effect.get("stroke"), effect.get("gradient"), effect.get("shadow")
            outer_glow, inner_glow = effect.get("outer_glow"), effect.get("inner_glow")
            clean = {"stroke": None, "gradient": None, "shadow": None,
                     "outer_glow": None, "inner_glow": None}
            if stroke is not None:
                if (not isinstance(stroke, dict) or not isinstance(stroke.get("size"), (int, float)) or
                        isinstance(stroke.get("size"), bool) or stroke["size"] <= 0 or stroke["size"] > 100):
                    raise ApiError(400, "Некорректная обводка: " + str(rid))
                clean["stroke"] = {"size": int(round(stroke["size"])),
                                   "color": rgb(stroke.get("color"), "Цвет обводки")}
            if gradient is not None:
                if (not isinstance(gradient, dict) or gradient.get("type") not in ("linear", "radial") or
                        not isinstance(gradient.get("angle", 0), (int, float))):
                    raise ApiError(400, "Некорректный градиент: " + str(rid))
                clean["gradient"] = {"type": gradient["type"],
                    "color1": rgb(gradient.get("color1"), "Цвет градиента"),
                    "color2": rgb(gradient.get("color2"), "Цвет градиента"),
                    "angle": float(gradient.get("angle", 0)) % 360}
            if shadow is not None:
                nums = [shadow.get(k) for k in ("opacity", "x", "y", "blur")] if isinstance(shadow, dict) else []
                if (len(nums) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in nums) or
                        not 0 <= nums[0] <= 100 or not 0 <= nums[3] <= 200):
                    raise ApiError(400, "Некорректная тень: " + str(rid))
                clean["shadow"] = {"color": rgb(shadow.get("color"), "Цвет тени"),
                    "opacity": float(nums[0]), "x": float(nums[1]), "y": float(nums[2]),
                    "blur": float(nums[3])}
            for field, glow, inner in (("outer_glow", outer_glow, False),
                                       ("inner_glow", inner_glow, True)):
                if glow is None:
                    continue
                nums = [glow.get(k) for k in ("opacity", "size", "spread")] if isinstance(glow, dict) else []
                source = glow.get("source", "edge") if isinstance(glow, dict) else "edge"
                if (len(nums) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in nums) or
                        not 0 <= nums[0] <= 100 or not 0 <= nums[1] <= 250 or
                        not 0 <= nums[2] <= 100 or (inner and source not in ("edge", "center"))):
                    raise ApiError(400, "Некорректное свечение: " + str(rid))
                clean[field] = {"color": rgb(glow.get("color"), "Цвет свечения"),
                    "opacity": float(nums[0]), "size": float(nums[1]), "spread": float(nums[2])}
                if inner:
                    clean[field]["source"] = source
            if any(clean.values()):
                clean_effects.append(clean)
        if blur is not None:
            if not isinstance(blur, dict) or blur.get("type") not in ("gaussian", "motion", "radial"):
                raise ApiError(400, "Некорректный тип размытия: " + str(rid))
            kind = blur["type"]
            if kind == "gaussian":
                radius = blur.get("radius")
                if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 0.1 <= radius <= 250:
                    raise ApiError(400, "Радиус размытия должен быть 0.1..250: " + str(rid))
                blur = {"type": kind, "radius": float(radius)}
            elif kind == "motion":
                distance, angle = blur.get("distance"), blur.get("angle")
                if (any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (distance, angle)) or
                        not 1 <= distance <= 2000):
                    raise ApiError(400, "Некорректное размытие в движении: " + str(rid))
                blur = {"type": kind, "distance": float(distance), "angle": float(angle) % 360}
            else:
                amount = blur.get("amount")
                method, quality = blur.get("method"), blur.get("quality")
                if (isinstance(amount, bool) or not isinstance(amount, (int, float)) or
                        not 1 <= amount <= 100 or method not in ("spin", "zoom") or
                        quality not in ("draft", "good", "best")):
                    raise ApiError(400, "Некорректное радиальное размытие: " + str(rid))
                blur = {"type": kind, "amount": int(round(amount)),
                        "method": method, "quality": quality}
        values = {
            "typeset_size": int(round(size)) if size is not None else None,
            "typeset_leading": int(round(leading)) if leading is not None else None,
            "typeset_align": align or None,
            "typeset_color": [int(round(v)) for v in color] if color is not None else None,
            "typeset_bold": True if bold else None,
            "typeset_italic": True if italic else None,
            "typeset_underline": True if underline else None,
            "typeset_effects": clean_effects or None,
            "typeset_blur": blur,
            "typeset_stroke": None, "typeset_gradient": None, "typeset_shadow": None,
        }
        for key, value in values.items():
            if value is None:
                region.pop(key, None)
            else:
                region[key] = value
        if rid not in saved:
            saved.append(rid)
    delete_set = set()
    for rid in deletes:
        if not isinstance(rid, str):
            raise ApiError(400, "id удаляемой области должен быть строкой")
        if rid not in by_id:
            if rid not in unknown:
                unknown.append(rid)
        else:
            delete_set.add(rid)
    if delete_set:
        analysis["regions"] = [r for r in analysis.get("regions", [])
                               if r.get("id") not in delete_set]
        saved.extend(rid for rid in delete_set if rid not in saved)
    for item in creates:
        if item["id"] not in saved:
            saved.append(item["id"])
    if saved:
        run._save(path, analysis)

    out = _page(stem, analysis)
    out["saved"], out["unknown"] = len(saved), unknown
    return out


def api_img(query):
    _opened()
    kind = _one(query, "kind")
    root = {"src": STATE["chapter"], "out": STATE["out_dir"]}.get(kind)
    if not root:
        raise ApiError(400, "kind должен быть src или out")
    name = _one(query, "name")
    if os.path.splitext(name)[1].lower() not in IMG_EXTS:
        raise ApiError(400, "Это не картинка: " + name)
    with open(_inside(root, name), "rb") as f:
        data = f.read()
    for head, ctype in MAGIC:
        if data.startswith(head):
            return ctype, data
    raise ApiError(400, "Файл не похож на картинку: " + name)


def api_font(query):
    """Отдаёт только шрифт из fonts/, выбранный по его PostScript-имени."""
    name = _one(query, "name")
    for path in fontcheck._collect(fontcheck.FONTS_DIR):
        try:
            if fontcheck.inspect(path, sets=())["ps"] != name:
                continue
        except Exception:
            continue
        with open(path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower()
        return ("font/otf" if ext == ".otf" else "font/ttf"), data
    raise ApiError(404, "Нет такого шрифта в fonts/: " + name)


def api_ui(query):
    with open(os.path.join(HERE, "ui.html"), "rb") as f:
        return "text/html; charset=utf-8", f.read()


def api_pick_folder(body):
    """Системный диалог Windows; браузер сам полный путь раскрывать не умеет."""
    try:
        import tkinter
        import tkinter.filedialog
    except ImportError as e:
        raise ApiError(500, "В Python нет компонента выбора папки: " + str(e))
    start = body.get("start") or ""
    if not isinstance(start, str):
        raise ApiError(400, "start должен быть строкой")
    start = os.path.abspath(start) if start and os.path.isdir(start) else os.getcwd()
    root = tkinter.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        path = tkinter.filedialog.askdirectory(
            parent=root, initialdir=start, title="Выберите папку", mustexist=False)
    finally:
        root.destroy()
    return {"path": os.path.abspath(path) if path else ""}


# --- настройки прогона ------------------------------------------------

def _fonts():
    """Шрифты из fonts/: как их назвать Photoshop'у и чего в них нет.

    Разбор двух десятков файлов занимает сотую секунды, поэтому кэша нет:
    шрифт могли поставить минуту назад, и устаревший список врал бы ровно
    тогда, когда на него смотрят. Проверку глифов не повторяем — она целиком
    в fontcheck, здесь только раскладка по целевым языкам.
    """
    installed = fontcheck.installed_files()
    out = []
    for path in fontcheck._collect(fontcheck.FONTS_DIR):
        here = os.path.basename(path).lower() in installed
        try:
            info = fontcheck.inspect(path, sets=fontcheck.ALL_SETS)
        except Exception as e:
            # Битый или незнакомый файл — не повод остаться без списка.
            out.append({"ps": "", "family": os.path.basename(path),
                        "installed": here, "error": str(e), "gaps": {}})
            continue
        out.append({
            "ps": info["ps"], "family": info["family"], "style": info["style"],
            # Не установлен в Windows — Photoshop его не увидит, и вёрстка
            # молча уедет на подменный шрифт.
            "installed": here, "error": "",
            "gaps": {t: sorted(k for k in translate.glyph_sets(t) if info["missing"].get(k))
                     for t in translate.TARGETS},
        })
    return out


def _font_names():
    """PostScript-имена для списков выбора, включая шрифт старого отчёта."""
    names = [f["ps"] for f in _fonts() if f.get("ps")]
    current = STATE.get("font") or bridge.DEFAULT_FONT
    if current and current not in names:
        names.insert(0, current)
    return names


def _providers():
    out = []
    for name in sorted(translate.BACKENDS):
        cfg = translate.BACKENDS[name]
        env = cfg["key_env"]
        out.append({
            "name": name, "model": cfg["model"], "note": cfg["note"],
            # Наружу уходит только факт «ключ задан». Сам ключ в браузере не
            # нужен ни для чего, а во вкладке он живёт и в истории, и в кэше,
            # и на первом же скриншоте.
            "key_env": env or "", "needs_key": bool(env),
            "key_set": bool(env and os.environ.get(env, "").strip()),
            # Ключ мог быть задан `set`'ом в том окне, откуда запущен сервер:
            # тогда он работает, но исчезнет вместе с окном.
            "key_stored": bool(env and envkey.stored(env)),
        })
    return out


def api_key(body):
    """Записать или убрать ключ провайдера.

    Провайдер приходит именем из BACKENDS, а не готовым именем переменной:
    запись в Environment — это исполнение кода при следующем входе в систему
    (PATH, PYTHONSTARTUP), и открывать браузеру произвольное имя нельзя.
    Обратно уходит только обновлённый список провайдеров: сам ключ в ответе
    не нужен ни для чего, а во вкладке он жил бы и в истории, и на первом же
    скриншоте.
    """
    name = _text(body, "provider")
    cfg = translate.BACKENDS.get(name)
    if cfg is None:
        raise ApiError(400, "Нет такого провайдера: " + name)
    if not cfg["key_env"]:
        raise ApiError(400, "%s — модель на этой машине, ключ ей не нужен" % name)
    key = _text(body, "key").strip()
    try:
        if key:
            envkey.save(cfg["key_env"], key)
        else:
            envkey.clear(cfg["key_env"])
    except ValueError as e:
        raise ApiError(400, str(e))
    except OSError as e:
        raise ApiError(500, "Не записалась переменная %s: %s" % (cfg["key_env"], e))
    return {"providers": _providers()}


def _sources():
    """Языки исходника: пересечение словарей образа и того, что знает промпт."""
    try:
        langs = bridge.health().get("langs") or []
        note = ""
    except Exception as e:
        # Контейнер могут поднять через минуту: тогда список из образа
        # неизвестен, и показываем всё, для чего вообще есть промпт.
        langs, note = [], "контейнер не ответил (%s), список языков из промпта" % e
    codes = [c for c in langs if c in translate.SOURCES] or sorted(translate.SOURCES)
    return [{"code": c, "name": translate.SOURCES[c]["name"]} for c in codes], note


def api_config(query):
    langs, note = _sources()
    return {
        "fonts": _fonts(), "default_font": bridge.DEFAULT_FONT,
        "providers": _providers(), "default_provider": translate.DEFAULT_PROVIDER,
        "langs": langs, "langs_note": note, "default_lang": translate.DEFAULT_SOURCE,
        "targets": [{"code": c, "name": translate.TARGETS[c]["name"]}
                    for c in sorted(translate.TARGETS)],
        "default_target": translate.DEFAULT_TARGET,
        "cv_url": bridge.CV_URL,
    }


# --- проект -----------------------------------------------------------

# Открытый проект один, как и открытая глава: инструмент для одного человека
# за одним столом. Держим разобранный объект, но состав глав каждый раз
# перечитываем с диска — иначе список разойдётся с тем, что там лежит.
PROJECT = {"proj": None}


def _project_state():
    proj = PROJECT["proj"]
    if proj is not None and not project.is_project(proj["root"]):
        # Папку унесли или удалили, пока она была открыта.
        proj = PROJECT["proj"] = None
    return {"project": project.info(proj) if proj else None,
            "recent": [{"path": r, "name": os.path.basename(r)}
                       for r in project.recent()]}


def _proj():
    proj = PROJECT["proj"]
    if proj is None:
        raise ApiError(400, "Проект не открыт")
    return proj


def api_project(query):
    return _project_state()


def api_project_open(body):
    path = _text(body, "path")
    if not path:
        raise ApiError(400, "Нужен путь к папке проекта")
    try:
        PROJECT["proj"] = project.load(path)
    except project.ProjectError as e:
        raise ApiError(400, str(e))
    project.remember(PROJECT["proj"]["root"])
    return _project_state()


def api_project_create(body):
    path = _text(body, "path")
    if not path:
        raise ApiError(400, "Нужна папка, в которой завести проект")
    try:
        PROJECT["proj"] = project.create(path, name=_text(body, "name"),
                                         source=_text(body, "source"))
    except project.ProjectError as e:
        raise ApiError(400, str(e))
    except OSError as e:
        raise ApiError(500, "Не создался проект: %s" % e)
    return _project_state()


def api_project_close(body):
    PROJECT["proj"] = None
    return _project_state()


def api_project_settings(body):
    """Настройки проекта из браузера: пишутся на каждое изменение в форме.

    Ключи моделей сюда не попадают: они в переменных среды (вкладка «Ключи»),
    потому что папку проекта копируют и архивируют, а ключ уехал бы с ней.
    """
    proj = _proj()
    patch = {k: v for k, v in (body or {}).items() if k in project.FIELDS}
    name = _text(body, "name")
    source = body.get("source")
    try:
        if patch:
            project.update(proj, patch)
        if name and name != proj["name"]:
            project.rename(proj, name)
        if isinstance(source, str):
            project.set_source(proj, source)
    except project.ProjectError as e:
        raise ApiError(400, str(e))
    except OSError as e:
        raise ApiError(500, "Не записался project.json: %s" % e)
    return _project_state()


def api_project_forget(body):
    """Убрать из списка недавних. Саму папку не трогаем."""
    project.forget(_text(body, "path"))
    return _project_state()


def api_scan(query):
    """Сколько страниц в папке — до того, как запускать получасовую задачу."""
    full = run._norm(_one(query, "path").strip())
    if not os.path.exists(full):
        raise ApiError(400, "Нет такого пути: " + full)
    if os.path.isfile(full):
        full, names = os.path.dirname(full), [os.path.basename(full)]
    else:
        try:
            names = run.list_pages(full)
        except SystemExit as e:
            # list_pages объясняет всё словами, но бросает SystemExit: в HTTP
            # это 400 с текстом, а не трейсбек в консоли сервера.
            raise ApiError(400, str(e))
    return {"path": full, "count": len(names), "names": names}


def api_check(body):
    """То же, что preflight делает перед главой, но без самой главы."""
    problems = []
    try:
        cv = bridge.health()
    except Exception as e:
        cv = None
        problems.append("CV-контейнер недоступен по %s: %s\n"
                        "Поднять: docker compose up -d" % (bridge.CV_URL, e))

    lang = _choice(body, "lang", translate.SOURCES, translate.DEFAULT_SOURCE)
    target_lang = _choice(body, "target_lang", translate.TARGETS, translate.DEFAULT_TARGET)
    provider = _choice(body, "provider", translate.BACKENDS, translate.DEFAULT_PROVIDER)
    langs = (cv or {}).get("langs") or []
    if langs and lang not in langs:
        problems.append("В образе нет словаря '%s'. Есть: %s" % (lang, ", ".join(langs)))

    engine = translate.Engine(provider, model=_text(body, "model") or None,
                              src_lang=lang, target=target_lang)
    try:
        # Для локального сервера это ещё и «что в тебя загружено»: describe()
        # после проверки называет фактическую модель, а не заглушку.
        engine.check()
    except translate.TranslateError as e:
        problems.append(str(e))
    return {"ok": not problems, "engine": engine.describe(), "cv": cv,
            "problems": problems}


# --- запуск -----------------------------------------------------------

def _text(body, key, default=""):
    value = body.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ApiError(400, "Поле %s должно быть строкой" % key)
    return value.strip()


def _flag(body, key):
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ApiError(400, "Поле %s должно быть true или false" % key)
    return value


def _count(body, key):
    value = body.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ApiError(400, "Поле %s должно быть целым больше нуля" % key)
    return value


def _choice(body, key, table, default):
    value = _text(body, key) or default
    if value not in table:
        raise ApiError(400, "Неизвестное значение %s=%r. Есть: %s"
                       % (key, value, ", ".join(sorted(table))))
    return value


def api_run(body):
    """Запуск главы. Настройки собираем в тот же settings(), что и argparse."""
    target = _text(body, "target")
    if not target:
        raise ApiError(400, "Нужна папка с исходниками")
    # Ключ и адрес провайдера из браузера не принимаем: ключ живёт в
    # переменной окружения, адрес — в BACKENDS. Вкладке они не нужны.
    # Открытый проект подставляется тем же with_project, что и в командной
    # строке: куда складывать, какой глоссарий и как понимать имя главы — это
    # его ответы, и второй реализации у них быть не должно.
    proj = PROJECT["proj"]
    kw = dict(
        target=target, project=proj["root"] if proj else None,
        out=_text(body, "out") or None,
        font=_text(body, "font") or bridge.DEFAULT_FONT,
        lang=_choice(body, "lang", translate.SOURCES, translate.DEFAULT_SOURCE),
        target_lang=_choice(body, "target_lang", translate.TARGETS, translate.DEFAULT_TARGET),
        provider=_choice(body, "provider", translate.BACKENDS, translate.DEFAULT_PROVIDER),
        model=_text(body, "model") or None,
        glossary=_text(body, "glossary") or None,
        reuse=_flag(body, "reuse"), no_translate=_flag(body, "no_translate"),
        erase_only=_flag(body, "erase_only"), analyze_only=_flag(body, "analyze_only"),
        limit=_count(body, "limit"), start=_text(body, "start") or None)
    try:
        args = run.settings(**run.with_project(kw))
    except SystemExit as e:
        raise ApiError(400, str(e))
    return JOB.start("chapter", os.path.basename(target.rstrip("/\\")) or target,
                     _chapter_work(args))


def _chapter_work(args):
    """Прогон главы в рабочем потоке — те же пять вызовов, что в run.main()."""
    def work():
        # resolve() и prepare() бросают SystemExit; ловит его Job._run.
        full_dir, names, out_dir = run.resolve(args)
        engine, glossary, health = run.prepare(args)
        with JOB.lock:
            JOB.chapter, JOB.out_dir, JOB.total = full_dir, out_dir, len(names)
        JOB.report("глава:   %s" % full_dir)
        JOB.report("выход:   %s" % out_dir)
        JOB.report("детект:  %s, OCR: tesseract-%s, шрифт: %s"
                   % (health["detector"], args.lang, args.font))
        JOB.report("языки:   %s -> %s" % (args.lang, args.target_lang))
        JOB.report("перевод: %s" % engine.describe())
        if glossary:
            JOB.report("глоссарий: %d записей" % len(glossary))
        JOB.report("страниц: %d" % len(names))

        rows = run.run_chapter(names, full_dir, out_dir, args, engine, glossary,
                               report=JOB.report, should_stop=JOB.stopper())
        run.write_report(out_dir, full_dir, args, engine, rows)
        ok = sum(1 for r in rows if r["ok"])
        with JOB.lock:
            JOB.done, JOB.page = len(rows), ""
        JOB.report("страниц пройдено %d из запрошенных %d, без ошибок %d"
                   % (len(rows), len(names), ok))
    return work


def api_cancel(body):
    return JOB.request_cancel()


def api_status(query):
    values = query.get("since") or []
    try:
        since = int(values[0]) if values else 0
    except ValueError:
        raise ApiError(400, "since должен быть числом")
    return JOB.snapshot(since)


def api_retypeset(body):
    """Пересборка одной страницы: тот же process(), но reuse + no-translate.

    Своего кода здесь нет намеренно. Анализ берётся с диска, ветка перевода
    пропускается целиком — поэтому engine и глоссарий не нужны и не строятся.
    Стирание пропустить нельзя: inpaint стирает только регионы с непустым
    переводом, а пустой перевод — это документированный способ вернуть
    исходный SFX. Значит, поход в контейнер плюс Photoshop, десятки секунд.
    """
    _opened()
    _idle()
    stem = body.get("stem")
    if not isinstance(stem, str):
        raise ApiError(400, "Нужен stem")
    row = _row(stem)
    if not row["editable"]:
        raise ApiError(400, "Нет %s.analysis.json — пересобирать нечего" % stem)
    if not STATE["chapter"] or not row["src"]:
        raise ApiError(400, "Исходник страницы не найден: пересобирать не из чего")
    # Имя пришло из отчёта, но до диска доходит через ту же проверку, что и
    # картинки: отчёт тоже правят руками.
    src = _inside(STATE["chapter"], row["src"])

    args = run.settings(target=src, out=STATE["out_dir"],
                        font=STATE["font"] or bridge.DEFAULT_FONT,
                        lang=STATE["lang"], target_lang=STATE["target_lang"],
                        reuse=True, no_translate=True)
    return JOB.start("retypeset", "пересборка " + stem,
                     _retypeset_work(args, row["src"], STATE["chapter"], STATE["out_dir"]))


def _retypeset_work(args, name, full_dir, out_dir):
    def work():
        with JOB.lock:
            JOB.chapter, JOB.out_dir, JOB.total = full_dir, out_dir, 1
        JOB.report("[1/1] %s" % name, stage="page")
        row = run.process(name, full_dir, out_dir, args, None, None,
                          report=JOB.report, should_stop=JOB.stopper())
        with JOB.lock:
            JOB.done = 1
        JOB.report("страница пересобрана" if row["ok"]
                   else "страница собрана с ошибками: " + (row["note"] or "?"))
    return work


GET = {
    "/": api_ui,
    "/api/pages": api_pages,
    "/api/page": api_page,
    "/api/img": api_img,
    "/api/font": api_font,
    "/api/config": api_config,
    "/api/scan": api_scan,
    "/api/project": api_project,
    "/api/status": api_status,
}
POST = {
    "/api/open": api_open,
    "/api/page": api_save,
    "/api/check": api_check,
    "/api/run": api_run,
    "/api/cancel": api_cancel,
    "/api/retypeset": api_retypeset,
    "/api/pick-folder": api_pick_folder,
    "/api/key": api_key,
    "/api/project/open": api_project_open,
    "/api/project/create": api_project_create,
    "/api/project/close": api_project_close,
    "/api/project/settings": api_project_settings,
    "/api/project/forget": api_project_forget,
}


def _one(query, key):
    values = query.get(key) or []
    if len(values) != 1 or not values[0]:
        raise ApiError(400, "Нужен ровно один параметр %s" % key)
    return values[0]


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# --- транспорт --------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "manga-tl"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._serve(GET, False)

    def do_POST(self):
        self._serve(POST, True)

    def _serve(self, table, is_post):
        try:
            self._check_host()
            url = urllib.parse.urlsplit(self.path)
            handler = table.get(url.path)
            if handler is None:
                raise ApiError(404, "Нет такого маршрута: " + url.path)
            result = handler(self._body() if is_post
                             else urllib.parse.parse_qs(url.query))
        except ApiError as e:
            self._reply(e.code, "application/json; charset=utf-8",
                        _dump({"error": str(e)}))
        except Exception as e:
            self._reply(500, "application/json; charset=utf-8",
                        _dump({"error": "%s: %s" % (type(e).__name__, e)}))
        else:
            if isinstance(result, tuple):
                self._reply(200, result[0], result[1])
            else:
                self._reply(200, "application/json; charset=utf-8", _dump(result))

    def _check_host(self):
        """Слушаем только 127.0.0.1, но этого мало.

        Любая открытая в браузере страница может послать сюда форму или
        подгрузить картинку с нашего адреса. Собственное имя в Host и
        JSON на POST такую отправку отсекают: ни форма, ни <img> их не
        подделают. Токены для инструмента одного человека — перебор.
        """
        host = (self.headers.get("Host") or "").strip()
        port = self.server.server_address[1]
        if host not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            raise ApiError(403, "Запрос не с этого адреса (Host: %s)" % host)

    def _body(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "POST принимается только с Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            raise ApiError(400, "Неверная длина тела запроса")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError as e:
            raise ApiError(400, "Тело не разобралось как JSON: %s" % e)
        if not isinstance(body, dict):
            raise ApiError(400, "Ожидался объект JSON")
        return body

    def _reply(self, code, ctype, data):
        if code >= 400:
            # Тело POST мы могли не дочитать — соединение дальше не годится.
            self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Страница пересобирается под тем же именем: кэш показал бы старое.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write((fmt % args) + "\n")


class Server(http.server.ThreadingHTTPServer):
    """То же, что ThreadingHTTPServer, но без права встать на занятый порт.

    На Windows SO_REUSEADDR разрешает привязаться к порту, который уже кто-то
    слушает: второй serve.py поднимается молча, а дальше соединения делятся
    между двумя процессами через раз. Снаружи это выглядит как «кнопка
    Открыть не работает»: половина запросов попадает в процесс, который про
    открытую главу ничего не знает, и интерфейс отвечает то главой, то
    пустотой. На POSIX флаг такого не позволяет и нужен, чтобы перезапуск не
    спотыкался о TIME_WAIT, — поэтому снимаем его только под Windows.
    """
    allow_reuse_address = os.name != "nt"


def main():
    p = argparse.ArgumentParser(
        prog="serve.py",
        description="Прогнать главу, посмотреть результат и поправить перевод "
                    "в браузере.")
    p.add_argument("out_dir", nargs="?",
                   help="папка с результатом (та, где run.report.json); "
                        "можно не указывать: главу открывают и запускают "
                        "из браузера")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--open", action="store_true",
                   help="открыть интерфейс в браузере (так делает manga-tl.bat)")
    args = p.parse_args()

    if args.out_dir:
        # Ошибку в пути показываем в консоли сразу, а не через браузер.
        try:
            _open(args.out_dir)
        except ApiError as e:
            raise SystemExit(str(e))
        print("глава: %s" % (STATE["chapter"] or "исходники не найдены"))
        print("выход: %s (%d страниц)" % (STATE["out_dir"], len(STATE["pages"])))

    try:
        srv = Server(("127.0.0.1", args.port), Handler)
    except OSError as e:
        raise SystemExit(
            "Порт %d уже занят — похоже, serve.py где-то запущен. Остановите "
            "его (Ctrl+C в том окне) или возьмите другой порт: --port %d.\n%s"
            % (args.port, args.port + 1, e))
    url = "http://127.0.0.1:%d" % args.port
    print("интерфейс: %s   (Ctrl+C — остановить)" % url)
    if args.open:
        # Открывать отсюда, а не из .bat: сокет уже слушает, и запрос браузера
        # дождётся своей очереди, даже если serve_forever ещё не дошёл до цикла.
        # Батник же может только подождать наугад — и попасть в «сайт недоступен»
        # на медленной машине.
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

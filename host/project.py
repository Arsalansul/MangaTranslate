r"""Проект — папка с настройками, глоссарием и результатами одной серии.

Зачем он вообще. Настройки прогона до сих пор жили одним набором на браузер:
шрифт, языки, модель. У корейского вебтуна и английской манги они разные, и
переключение между сериями каждый раз означало перенабрать форму целиком.
Проект — это место, где ответ «чем гнать вот эту серию» лежит рядом с самой
серией, а не в localStorage.

Что внутри:

    Серия\
      project.json     маркер, настройки, путь к оригиналам
      glossary.json    один на проект
      out\
        Глава 1\       PNG, PSD, analysis.json, run.report.json

Маркером служит сам project.json: есть он в папке — значит это проект, и
реестра «где лежат мои проекты» держать не надо. Список недавних (`recent`)
— всего лишь удобство выпадашки, его потеря ничего не стоит.

Оригиналы лежат снаружи: главы приезжают качалкой, копировать гигабайты в
проект незачем. Поэтому `source` — путь к папке, в подпапках которой лежат
главы. Если оригиналы пропадут, проект от этого не ломается: см. `chapters`.

Ключи моделей сюда не попадают и попасть не должны — они в переменных среды
(см. envkey). Папку проекта копируют, архивируют и отправляют «посмотреть».

Только stdlib, как и весь host/.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Картинки и список страниц берём у прогона: папка, которую run.py считает
# главой, и папка, которую проект показывает главой, обязаны быть одной и той
# же папкой. Обратной зависимости нет и быть не должно — run.py импортирует
# project внутри main(), иначе получился бы цикл.
import run

EXTS = run.EXTS

MARKER = "project.json"
GLOSSARY = "glossary.json"
OUT = "out"
VERSION = 1

# Настройки, которые принадлежат серии, а не отдельному прогону. Имена — как
# у полей run.settings(): проект отдаёт их туда напрямую, и лишнее имя здесь
# уронит прогон TypeError'ом сразу, а не потеряется молча.
FIELDS = ("font", "lang", "target_lang", "provider", "model")


class ProjectError(Exception):
    """Проект не открылся или не создался. Текст показывается человеку."""


def _norm(path):
    return os.path.normpath(os.path.abspath(os.path.expanduser(path or "")))


def _write_json(path, obj):
    """Запись через временный файл рядом.

    Настройки сохраняются на каждое изменение в браузере. Запись поверх
    оборвётся на отключении питания ровно один раз — и оставит обрезанный
    JSON вместо проекта.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_NUM = re.compile(r"(\d+)")


def _natural(name):
    """«Глава 2» раньше «Глава 10»: главы нумерованы, а не названы."""
    return [int(p) if p.isdigit() else p.lower() for p in _NUM.split(name)]


def _has_images(path):
    try:
        return any(n.lower().endswith(EXTS) for n in os.listdir(path))
    except OSError:
        return False


# --- файл проекта -----------------------------------------------------

def _blank():
    return {"version": VERSION, "name": "", "source": "",
            "settings": {}, "created": ""}


def path_of(root):
    return os.path.join(root, MARKER)


def is_project(root):
    return os.path.isfile(path_of(root))


def create(root, name="", source=""):
    """Создать проект в папке. Папка может не существовать или быть пустой."""
    root = _norm(root)
    if not root or os.path.dirname(root) == root:
        raise ProjectError("Это корень диска, а не папка проекта: " + root)
    if is_project(root):
        raise ProjectError("Здесь уже есть проект: " + root)
    if os.path.isfile(root):
        raise ProjectError("Это файл, а не папка: " + root)
    try:
        os.makedirs(os.path.join(root, OUT), exist_ok=True)
    except OSError as e:
        raise ProjectError("Не создалась папка проекта: %s" % e)
    gl = os.path.join(root, GLOSSARY)
    if not os.path.exists(gl):
        _write_json(gl, {})
    data = _blank()
    data["name"] = (name or os.path.basename(root)).strip()
    data["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["source"] = _store_path(root, source)
    _write_json(path_of(root), data)
    remember(root)
    return load(root)


def load(root):
    """Прочитать проект. `root` — папка проекта или сам project.json."""
    root = _norm(root)
    if os.path.basename(root).lower() == MARKER:
        root = os.path.dirname(root)
    if not is_project(root):
        raise ProjectError("В папке нет %s: %s" % (MARKER, root))
    try:
        with open(path_of(root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ProjectError("Не читается %s: %s" % (MARKER, e))
    if not isinstance(data, dict):
        raise ProjectError("%s должен быть объектом" % MARKER)
    ver = data.get("version")
    if ver != VERSION:
        # Не «сломался», а «из другой версии»: гадать, чего в нём не хватает,
        # дороже, чем сказать прямо.
        raise ProjectError("Проект версии %r, эта сборка понимает %d"
                           % (ver, VERSION))
    out = _blank()
    out.update({k: data.get(k, out[k]) for k in out})
    out["settings"] = {k: v for k, v in (data.get("settings") or {}).items()
                       if k in FIELDS and isinstance(v, str)}
    out["root"] = root
    return out


def save(proj):
    """Записать проект обратно. `root` в файл не попадает — он и есть путь."""
    root = proj["root"]
    data = {k: proj.get(k) for k in _blank()}
    data["version"] = VERSION
    _write_json(path_of(root), data)
    return proj


def update(proj, patch):
    """Поменять настройки проекта. Всё, чего нет в FIELDS, отбрасывается."""
    known = {k: v.strip() for k, v in (patch or {}).items()
             if k in FIELDS and isinstance(v, str)}
    proj["settings"].update(known)
    save(proj)
    return proj


def rename(proj, name):
    name = (name or "").strip()
    if not name:
        raise ProjectError("Пустое имя проекта")
    proj["name"] = name
    save(proj)
    return proj


def set_source(proj, source):
    """Перепривязать оригиналы: качалка переехала, диск сменился."""
    proj["source"] = _store_path(proj["root"], source)
    save(proj)
    return proj


# --- пути -------------------------------------------------------------

def _store_path(root, path):
    """Внутри проекта — относительно него, снаружи — как есть.

    Папку проекта переименуют или перенесут на другой диск в первый же месяц;
    абсолютный путь на своё же содержимое этого не переживёт. Внешние
    оригиналы — исключение, их относительный путь был бы бессмыслицей.
    """
    path = (path or "").strip()
    if not path:
        return ""
    full = _norm(path)
    try:
        rel = os.path.relpath(full, root)
    except ValueError:
        # Разные диски: relpath на Windows бросает, а не возвращает "..".
        return full
    return rel if not rel.startswith("..") else full


def _full_path(root, stored):
    if not stored:
        return ""
    return stored if os.path.isabs(stored) else _norm(os.path.join(root, stored))


def source_dir(proj):
    return _full_path(proj["root"], proj.get("source"))


def out_root(proj):
    return os.path.join(proj["root"], OUT)


def glossary_path(proj):
    return os.path.join(proj["root"], GLOSSARY)


# --- главы ------------------------------------------------------------

def _report(out_dir):
    path = os.path.join(out_dir, "run.report.json")
    try:
        with open(path, encoding="utf-8") as f:
            rep = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rep, dict):
        return None
    pages = rep.get("pages") or []
    return {"total": len(pages),
            "ok": sum(1 for p in pages if isinstance(p, dict) and p.get("ok")),
            "at": time.strftime("%Y-%m-%d %H:%M",
                                time.localtime(os.path.getmtime(path)))}


def _source_names(src):
    """Главы в папке оригиналов: её подпапки с картинками.

    Если картинки лежат прямо в ней — это одна глава, а не пустой список:
    качалки складывают и так, и так, а «глав нет» на полной папке читается
    как поломка.
    """
    if not src or not os.path.isdir(src):
        return {}
    found = {}
    try:
        names = os.listdir(src)
    except OSError:
        return {}
    for n in sorted(names):
        full = os.path.join(src, n)
        if os.path.isdir(full) and _has_images(full):
            found[n] = full
    if not found and _has_images(src):
        found[os.path.basename(src)] = src
    return found


def chapters(proj):
    """Список глав: что есть в оригиналах, плюс что уже лежит в out.

    Состояние не хранится, а выводится из того, что на диске. Поэтому оно
    не может разойтись с действительностью, а исчезнувшие оригиналы дают
    честное «нет оригинала» вместо пропавшей из списка главы: результат её
    прогона никуда не делся, и смотреть его по-прежнему можно.
    """
    src = source_dir(proj)
    have = _source_names(src)
    root = out_root(proj)
    done = set()
    if os.path.isdir(root):
        try:
            done = {n for n in os.listdir(root)
                    if os.path.isdir(os.path.join(root, n))}
        except OSError:
            done = set()
    rows = []
    for name in sorted(set(have) | done, key=_natural):
        full = have.get(name, "")
        out_dir = os.path.join(root, name)
        rep = _report(out_dir) if name in done else None
        rows.append({
            "name": name,
            "src": full,
            "out": out_dir if name in done else "",
            "pages": len(run.list_pages(full)) if full else 0,
            "missing": not full,
            "done": bool(rep),
            "ok": rep["ok"] if rep else 0,
            "total": rep["total"] if rep else 0,
            "at": rep["at"] if rep else "",
        })
    return rows


def info(proj):
    """Проект целиком — то, что нужно интерфейсу за один запрос."""
    src = source_dir(proj)
    return {"root": proj["root"], "name": proj["name"], "source": src,
            "source_ok": bool(src) and os.path.isdir(src),
            "created": proj.get("created") or "",
            "settings": dict(proj["settings"]),
            "out": out_root(proj), "glossary": glossary_path(proj),
            "chapters": chapters(proj)}


# --- недавние ---------------------------------------------------------

def _store():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "manga-tl")


def _recent_path():
    return os.path.join(_store(), "recent.json")


def recent(limit=12):
    """Недавние проекты — только те, что ещё существуют."""
    try:
        with open(_recent_path(), encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    out = []
    for p in items:
        if isinstance(p, str) and is_project(p) and p not in out:
            out.append(p)
    return out[:limit]


def remember(root):
    """Дописать проект в начало списка недавних.

    Список живёт в профиле пользователя, а не в папке репозитория: репозиторий
    отдаётся другому человеку и обновляется, и его содержимое не должно
    зависеть от того, какие серии здесь открывали.
    """
    root = _norm(root)
    items = [root] + [p for p in recent(limit=50) if p != root]
    try:
        os.makedirs(_store(), exist_ok=True)
        _write_json(_recent_path(), items[:50])
    except OSError:
        pass  # Не открыться из-за списка недавних было бы смешно.
    return items


def forget(root):
    root = _norm(root)
    items = [p for p in recent(limit=50) if p != root]
    try:
        os.makedirs(_store(), exist_ok=True)
        _write_json(_recent_path(), items)
    except OSError:
        pass
    return items

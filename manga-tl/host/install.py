"""Установка и проверка: разложить то, что раскладывается, и честно сказать про остальное.

Установщик делится надвое, и это разделение принципиальное.

Что он ставит сам: шрифты вёрстки из `fonts/`, веса детектора и инпейнта,
контейнер, ключ модели в переменную окружения. Всё это либо файлы в папках
проекта, либо запись в свою ветку реестра — ни прав администратора, ни
установщиков Microsoft для этого не надо.

О чём он только сообщает: Windows, Python, Photoshop, Docker Desktop и
системные шрифты под исходные языки. Это чужие программы; поставить их за
человека нельзя, а сделать вид, что их наличие проверено, — можно, и тогда
первая же глава упадёт на середине. Поэтому каждая проверка печатает, что
именно не так и куда идти.

Важное про шрифты: они ставятся для текущего пользователя, в
%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts. Photoshop их видит (проверено), а
UAC при этом не всплывает. Запустите установщик от администратора — и они
лягут на всю машину, в C:\\Windows\\Fonts; разницы для работы нет.

    python host/install.py            всё целиком, с вопросами
    python host/install.py --check    ничего не трогать, только проверить
    python host/install.py --yes      без вопросов, согласие по умолчанию

Только stdlib, как и весь host/.
"""
import argparse
import ctypes
import getpass
import os
import shutil
import struct
import subprocess
import sys
import urllib.request
import winreg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fontcheck

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(ROOT, "fonts")
MODELS_DIR = os.path.join(ROOT, "container", "models")

# Веса: имя файла -> (адрес, наименьший правдоподобный размер, зачем).
# Размер нужен, чтобы не принять за веса html-страницу с ошибкой: curl и
# urllib одинаково охотно сохранят её под нужным именем и молча закончат.
WEIGHTS = {
    "comictextdetector.onnx": (
        "https://github.com/zyddnys/manga-image-translator/releases/download/"
        "beta-0.2.1/comictextdetector.pt.onnx",
        90 * 1024 * 1024,
        "детектор текста; без него контейнер откатится на морфологию OpenCV"),
    "lama.onnx": (
        "https://huggingface.co/opencv/inpainting_lama/resolve/main/"
        "inpainting_lama_2025jan.onnx",
        85 * 1024 * 1024,
        "стирание поверх рисунка; без него останется Content-Aware Fill"),
}

# Системные шрифты под чтение оригинала в интерфейсе. Содержимое .ttc
# разобрать нечем (fontcheck честно отказывается от коллекций), поэтому
# проверка по именам файлов — так же, как их кладёт сама Windows.
SCRIPT_FONTS = {
    "китайский и японская кана": ("msyh.ttc", "msjh.ttc", "simsun.ttc", "simhei.ttf"),
    "корейский": ("malgun.ttf", "gulim.ttc", "batang.ttc", "dotum.ttc"),
    "японский": ("yugothm.ttc", "yugothic.ttf", "msgothic.ttc", "meiryo.ttc"),
}

HWND_BROADCAST = 0xFFFF
WM_FONTCHANGE = 0x001D
SMTO_ABORTIFHUNG = 0x0002

AUTO = False
CHECK_ONLY = False
PROBLEMS = []


# --- вывод ----------------------------------------------------------------

def head(title):
    print("\n" + title)
    print("-" * len(title))


def ok(msg):
    print("  + " + msg)


def bad(msg, fix=None):
    print("  - " + msg)
    if fix:
        print("      " + fix)
    PROBLEMS.append((msg, fix))


def warn(msg, fix=None):
    print("  ! " + msg)
    if fix:
        print("      " + fix)


def note(msg):
    print("    " + msg)


def few(items, n=6):
    """Список через запятую, но без простыни: шрифтов бывает и три десятка."""
    items = sorted(items)
    if len(items) <= n:
        return ", ".join(items)
    return ", ".join(items[:n]) + " и ещё %d" % (len(items) - n)


def ask(q, default=True):
    if AUTO or CHECK_ONLY:
        return default and not CHECK_ONLY
    tail = "[Д/н] " if default else "[д/Н] "
    try:
        a = input("  ? %s %s" % (q, tail)).strip().lower()
    except EOFError:
        return default
    if not a:
        return default
    return a[0] in ("д", "y")


def run(cmd):
    """Запустить и вернуть (код, вывод). 127 — программы нет вовсе.

    cwd всегда корень проекта: docker compose ищет свой файл от текущей
    папки, а установщик могут запустить откуда угодно.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --- проверки чужих программ ----------------------------------------------

def check_system():
    head("1. Система и Python")
    if os.name != "nt":
        bad("это не Windows", "Photoshop управляется через COM, а COM есть только здесь")
        return
    ok("Windows")
    v = sys.version_info
    if v < (3, 8):
        bad("Python %d.%d — нужен 3.8 или новее" % (v[0], v[1]),
            "поставьте с python.org")
    else:
        ok("Python %d.%d.%d" % (v[0], v[1], v[2]))
    note("права: " + ("администратор, шрифты лягут на всю машину"
                      if is_admin() else
                      "обычные, шрифты лягут текущему пользователю"))


def check_photoshop():
    head("2. Photoshop")
    # Ищем регистрацию COM, а не файл на диске: run.py обращается именно к
    # ней, и версия с диска без регистрации ему всё равно не годится.
    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Photoshop.Application").Close()
    except OSError:
        bad("Photoshop не зарегистрирован в COM",
            "поставьте его и один раз запустите вручную — регистрация "
            "происходит при первом запуске")
        return
    vers = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            k = winreg.OpenKey(root, r"SOFTWARE\Adobe\Photoshop")
        except OSError:
            continue
        with k:
            i = 0
            while True:
                try:
                    vers.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    ok("Photoshop зарегистрирован" + (" (версии: %s)" % ", ".join(sorted(set(vers)))
                                      if vers else ""))
    note("если шрифты ставятся сейчас — Photoshop потом перезапустить, "
         "список шрифтов он читает только на старте")


def check_docker():
    head("3. Docker")
    code, out = run(["docker", "--version"])
    if code == 127:
        bad("Docker не найден",
            "Docker Desktop с docker.com; без него детект и OCR работать не будут")
        return False
    if code != 0:
        bad("docker --version вернул ошибку", out.splitlines()[0] if out else "")
        return False
    ok(out.splitlines()[0])
    code, out = run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if code != 0:
        bad("Docker установлен, но демон не отвечает",
            "запустите Docker Desktop и дождитесь зелёного значка")
        return False
    ok("демон отвечает, сервер " + out.splitlines()[0])
    return True


def check_system_fonts():
    head("4. Системные шрифты под чтение оригинала")
    have = fontcheck.installed_files()
    missing = []
    for script, files in SCRIPT_FONTS.items():
        found = [f for f in files if f in have]
        if found:
            ok("%s — есть (%s)" % (script, ", ".join(found)))
        else:
            missing.append(script)
            warn("%s — нечем нарисовать, в интерфейсе будут пустые квадраты" % script)
    if missing:
        note("это не мешает ни распознаванию, ни вёрстке: текст лежит в "
             "analysis.json целым, его просто нечем показать на экране")
        note("ставится вместе с языком: Параметры -> Время и язык -> "
             "Язык и регион -> Добавить язык")
        note("после установки браузер закрыть полностью и открыть заново — "
             "список шрифтов он читает один раз при старте")


# --- шрифты вёрстки -------------------------------------------------------

def _reg_name(path, info):
    """Имя записи в реестре — по соглашению Windows: «Семейство Начертание (тип)»."""
    with open(path, "rb") as f:
        otto = f.read(4) == b"OTTO"
    label = info["family"] or info["ps"] or os.path.basename(path)
    style = (info["style"] or "").strip()
    if style and style.lower() != "regular":
        label += " " + style
    return label + (" (OpenType)" if otto else " (TrueType)")


def install_fonts():
    head("5. Шрифты вёрстки из fonts/")
    if not os.path.isdir(FONTS_DIR):
        warn("папки fonts/ нет",
             "создайте её и положите туда .otf или .ttf — можно подпапками")
        return
    files = fontcheck._collect(FONTS_DIR)
    if not files:
        warn("в fonts/ нет ни одного .otf или .ttf",
             "положите файлы шрифтов туда — можно подпапками, они обходятся рекурсивно")
        return

    per_user = not is_admin()
    if per_user:
        dest = os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts")
        root, hive = winreg.HKEY_CURRENT_USER, "HKCU"
    else:
        dest = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        root, hive = winreg.HKEY_LOCAL_MACHINE, "HKLM"

    have = fontcheck.installed_files()
    todo = [p for p in files if os.path.basename(p).lower() not in have]
    print("  найдено %d, из них уже установлено %d" % (len(files), len(files) - len(todo)))
    if not todo:
        ok("ставить нечего")
    elif CHECK_ONLY:
        note("поставилось бы: " + few([os.path.basename(p) for p in todo]))
    elif not ask("поставить %d шт. в %s?" % (len(todo), dest)):
        note("пропущено")
    else:
        os.makedirs(dest, exist_ok=True)
        done = 0
        for src in todo:
            name = os.path.basename(src)
            try:
                info = fontcheck.inspect(src, sets=())
            except (ValueError, struct.error) as e:
                bad("%s — не разобрался: %s" % (name, e))
                continue
            tgt = os.path.join(dest, name)
            try:
                shutil.copyfile(src, tgt)
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT"
                                          r"\CurrentVersion\Fonts", 0,
                                    winreg.KEY_SET_VALUE) as k:
                    # Своя ветка хранит полный путь, общесистемная — только имя
                    # файла: там каталог подразумевается, и полный путь ломает
                    # подхват шрифта.
                    winreg.SetValueEx(k, _reg_name(src, info), 0, winreg.REG_SZ,
                                      tgt if per_user else name)
                ctypes.windll.gdi32.AddFontResourceW(tgt)
            except (OSError, PermissionError) as e:
                bad("%s — не поставился: %s" % (name, e))
                continue
            done += 1
            ok("%s -> %s" % (name, info["ps"] or "имя не прочиталось"))
        if done:
            # Без этого уже запущенные программы про новый шрифт не узнают.
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000, None)
            print("  поставлено %d, записи в %s" % (done, hive))

    _report_coverage(files)


def _report_coverage(files):
    """Что из поставленного годится под какой целевой язык."""
    # Какие наборы глифов обязательны для языка, решает translate.TARGETS —
    # спрашиваем там же, где это спросит сам прогон, а не повторяем правило.
    need = {t: fontcheck.sets_for(t) for t in ("ru", "en")}
    fit = {"ru": [], "en": []}
    for p in files:
        try:
            info = fontcheck.inspect(p, sets=fontcheck.ALL_SETS)
        except Exception:
            continue
        for target in ("ru", "en"):
            if not any(info["missing"][k] for k in need[target]):
                fit[target].append(info["ps"] or os.path.basename(p))
    for target in ("ru", "en"):
        if fit[target]:
            ok("под --target-lang %s годятся %d: %s"
               % (target, len(set(fit[target])), few(set(fit[target]))))
        else:
            bad("под --target-lang %s не годится ни один шрифт из fonts/" % target,
                "нужна кириллица и латиница; подробности — python host/fontcheck.py")
    note("в --font передаётся PostScript-имя (слева), а не то, "
         "что Photoshop показывает в списке")


# --- веса -----------------------------------------------------------------

def _download(url, tgt, least):
    tmp = tgt + ".part"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        sys.stdout.write("\r      %d%% (%.1f из %.1f МБ)"
                                         % (got * 100 // total, got / 1048576,
                                            total / 1048576))
                    else:
                        sys.stdout.write("\r      %.1f МБ" % (got / 1048576))
                    sys.stdout.flush()
        sys.stdout.write("\r" + " " * 40 + "\r")
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return "не скачалось: %s" % e
    if os.path.getsize(tmp) < least:
        os.remove(tmp)
        return ("скачалось %d байт вместо хотя бы %d — похоже, вместо файла "
                "пришла страница с ошибкой" % (got, least))
    os.replace(tmp, tgt)
    return None


def weights():
    head("6. Веса моделей")
    os.makedirs(MODELS_DIR, exist_ok=True)
    for name, (url, least, why) in WEIGHTS.items():
        tgt = os.path.join(MODELS_DIR, name)
        if os.path.exists(tgt) and os.path.getsize(tgt) >= least:
            ok("%s — на месте (%.0f МБ)" % (name, os.path.getsize(tgt) / 1048576))
            continue
        if os.path.exists(tgt):
            warn("%s есть, но подозрительно мал — перекачаю" % name)
        print("  %s — %s" % (name, why))
        if CHECK_ONLY:
            bad("%s отсутствует" % name, "скачается при обычном запуске установщика")
            continue
        if not ask("скачать %s (~%d МБ)?" % (name, least // 1048576)):
            bad("%s пропущен" % name, "без него работа хуже, но глава выйдет")
            continue
        err = _download(url, tgt, least)
        if err:
            bad("%s — %s" % (name, err), "можно скачать вручную: " + url)
        else:
            ok("%s — скачан" % name)


# --- контейнер ------------------------------------------------------------

def container(docker_ok):
    head("7. Контейнер")
    if not docker_ok:
        bad("пропущен: Docker недоступен", "разберитесь с шагом 3 и запустите снова")
        return
    code, out = run(["docker", "compose", "ps", "--format", "{{.Name}} {{.State}}"])
    running = [l for l in out.splitlines() if "running" in l]
    if running:
        ok("уже поднят: " + "; ".join(running))
    elif CHECK_ONLY:
        bad("контейнер не запущен", "docker compose up -d")
        return
    elif ask("поднять контейнер (docker compose up -d)?"):
        # Не глушим вывод: первая сборка идёт минутами, и молчащее окно
        # выглядит зависшим.
        code = subprocess.call(["docker", "compose", "up", "-d"], cwd=ROOT)
        if code != 0:
            bad("docker compose up -d вернул %d" % code)
            return
    else:
        note("пропущено")
        return
    code, out = run([sys.executable, os.path.join(ROOT, "host", "bridge.py"), "health"])
    if code != 0:
        bad("контейнер не отвечает на health", out.splitlines()[0] if out else "")
        return
    for field, good in (("comic-text-detector", "детектор"), ("lama", "инпейнт")):
        if field in out:
            ok("%s поднялся" % good)
        else:
            warn("%s не поднялся — проверьте веса из шага 6" % good)


# --- ключ модели ----------------------------------------------------------

def api_key():
    head("8. Ключ модели перевода")
    sys.path.insert(0, os.path.join(ROOT, "host"))
    import translate
    # У локальных провайдеров key_env пуст: модель крутится на этой же
    # машине, платить и авторизоваться не у кого.
    cloud = {n: b for n, b in translate.BACKENDS.items() if b["key_env"]}
    local = [n for n, b in translate.BACKENDS.items() if not b["key_env"]]
    have = [(n, b["key_env"]) for n, b in cloud.items()
            if os.environ.get(b["key_env"], "").strip()]
    if have:
        ok("ключ задан: " + ", ".join("%s (%s)" % (n, e) for n, e in have))
        return
    print("  ключа нет ни у одного облачного провайдера. Без ключа глава")
    print("  прогоняется только с --no-translate: текст сотрётся, перевод")
    print("  не встанет. Либо ключ, либо локальная модель:")
    for n, b in cloud.items():
        print("      %-12s %-20s %s" % (n, b["key_env"], b["note"]))
    for n in local:
        print("      %-12s %-20s %s" % (n, "ключ не нужен",
                                        translate.BACKENDS[n]["note"]))
    if CHECK_ONLY or not ask("вписать ключ сейчас?", default=False):
        bad("ключ модели не задан",
            "переменная окружения из списка выше — и перезапустить терминал; "
            "либо локальная модель, ей ключ не нужен")
        return
    name = input("  ? провайдер [anthropic]: ").strip() or "anthropic"
    if name not in cloud:
        bad("нет такого облачного провайдера: %s" % name,
            "из списка выше: " + ", ".join(cloud))
        return
    env = translate.BACKENDS[name]["key_env"]
    # getpass, чтобы ключ не остался в истории окна и в записи экрана.
    key = getpass.getpass("  ? ключ (не отображается): ").strip()
    if not key:
        note("пусто, пропущено")
        return
    # Куда и почему именно туда — в envkey; тем же кодом ключ пишет и
    # веб-интерфейс, так что расходиться этим двоим нельзя.
    import envkey
    try:
        envkey.save(env, key)
    except (OSError, ValueError) as e:
        bad("не записалась переменная %s: %s" % (env, e))
        return
    ok("%s записан в переменные пользователя" % env)
    note("подхватится в новых окнах терминала; текущее надо перезапустить")


# --- итог -----------------------------------------------------------------

def main():
    global AUTO, CHECK_ONLY
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Поставить то, что ставится, и проверить то, что не ставится.")
    p.add_argument("--check", action="store_true",
                   help="ничего не менять, только проверить")
    p.add_argument("--yes", action="store_true",
                   help="не задавать вопросов, соглашаться по умолчанию")
    args = p.parse_args()
    AUTO, CHECK_ONLY = args.yes, args.check

    print("manga-tl — %s" % ("проверка" if CHECK_ONLY else "установка"))
    print("папка проекта: %s" % ROOT)

    check_system()
    check_photoshop()
    docker_ok = check_docker()
    check_system_fonts()
    install_fonts()
    weights()
    container(docker_ok)
    api_key()

    head("Итог")
    if not PROBLEMS:
        print("  Всё на месте. Запускать — manga-tl.bat в корне проекта.")
        return 0
    print("  Осталось разобраться с этим:\n")
    for i, (msg, fix) in enumerate(PROBLEMS, 1):
        print("  %d. %s" % (i, msg))
        if fix:
            print("     %s" % fix)
    print("\n  Проверить ещё раз, ничего не меняя: python host/install.py --check")
    return 1


if __name__ == "__main__":
    sys.exit(main())

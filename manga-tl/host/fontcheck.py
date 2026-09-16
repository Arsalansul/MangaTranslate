"""Проверка шрифтов перед вёрсткой: имя, покрытие, установлен ли в системе.

Три вещи, каждая из которых ломает страницу молча.

PostScript-имя. В скрипт Photoshop уходит именно оно, а не то, что видно в
списке шрифтов: `NMDozor-Bold`, а не «NM Dozor Bold». По имени файла его не
угадать — оно лежит внутри шрифта.

Покрытие. Большинство комикс-гарнитур латинские. Такой шрифт поставится,
Photoshop покажет его в списке, а на месте русских букв будут квадраты или
подстановка из другого шрифта. Отдельно проверяется пунктуация: кавычки-ёлочки
и тире есть далеко не везде, а в репликах они встречаются постоянно.

Установка. Photoshop видит только установленные в Windows шрифты, файл в
папке проекта он не подхватит.

Разбор шрифта сделан на stdlib намеренно: хостовая часть по уговору живёт на
системном Python без зависимостей, и тянуть ради одной таблицы fontTools
означало бы заводить venv на хосте.

    python host/fontcheck.py [путь к файлу или папке]
"""
import os
import struct
import sys

FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")

CYRILLIC = [chr(c) for c in range(0x410, 0x450)] + ["\u0401", "\u0451"]
LATIN = [chr(c) for c in range(0x41, 0x5B)] + [chr(c) for c in range(0x61, 0x7B)]
DIGITS = [chr(c) for c in range(0x30, 0x3A)]
PUNCT = list(".,!?:;()'\"-\u2013\u2014\u2026\u00ab\u00bb\u201c\u201d")

NAME_FAMILY, NAME_STYLE, NAME_PS = 1, 2, 6


def _tables(buf):
    """Каталог таблиц sfnt: тег -> (смещение, длина)."""
    if buf[:4] == b"ttcf":
        raise ValueError("коллекция шрифтов (.ttc) не поддерживается")
    num = struct.unpack(">H", buf[4:6])[0]
    out = {}
    for i in range(num):
        tag, _, off, ln = struct.unpack(">4sIII", buf[12 + i * 16:28 + i * 16])
        out[tag.decode("latin-1")] = (off, ln)
    return out


def _names(buf, off):
    """Записи таблицы name. Windows-варианты имеют приоритет над Mac."""
    count, str_off = struct.unpack(">HH", buf[off + 2:off + 6])
    base = off + str_off
    out = {}
    for i in range(count):
        rec = off + 6 + i * 12
        pid, eid, _lid, nid, ln, o = struct.unpack(">HHHHHH", buf[rec:rec + 12])
        raw = buf[base + o:base + o + ln]
        if pid == 3:
            try:
                val = raw.decode("utf-16-be")
            except UnicodeDecodeError:
                continue
        elif pid == 1:
            val = raw.decode("latin-1")
        else:
            continue
        # Первым пишем Mac, но Windows должен побеждать: у него шире кодировка.
        if nid not in out or pid == 3:
            out[nid] = val
    return out


def _cmap4(buf, off):
    seg2 = struct.unpack(">H", buf[off + 6:off + 8])[0]
    seg = seg2 // 2
    ends = struct.unpack(">%dH" % seg, buf[off + 14:off + 14 + seg2])
    p = off + 16 + seg2
    starts = struct.unpack(">%dH" % seg, buf[p:p + seg2])
    p += seg2
    deltas = struct.unpack(">%dh" % seg, buf[p:p + seg2])
    p += seg2
    ranges = struct.unpack(">%dH" % seg, buf[p:p + seg2])
    ro_base = p

    covered = set()
    for i in range(seg):
        if starts[i] > ends[i] or starts[i] == 0xFFFF:
            continue
        for c in range(starts[i], ends[i] + 1):
            if ranges[i] == 0:
                gid = (c + deltas[i]) & 0xFFFF
            else:
                gp = ro_base + i * 2 + ranges[i] + (c - starts[i]) * 2
                if gp + 2 > len(buf):
                    continue
                gid = struct.unpack(">H", buf[gp:gp + 2])[0]
                if gid:
                    gid = (gid + deltas[i]) & 0xFFFF
            if gid:
                covered.add(c)
    return covered


def _cmap12(buf, off):
    n = struct.unpack(">I", buf[off + 12:off + 16])[0]
    covered = set()
    for i in range(n):
        s, e, g = struct.unpack(">III", buf[off + 16 + i * 12:off + 28 + i * 12])
        if g and e - s < 0x110000:
            covered.update(range(s, e + 1))
    return covered


def _coverage(buf, off):
    n = struct.unpack(">H", buf[off + 2:off + 4])[0]
    subs = {}
    for i in range(n):
        pid, eid, o = struct.unpack(">HHI", buf[off + 4 + i * 8:off + 12 + i * 8])
        subs[(pid, eid)] = off + o
    # (3,10) — полный Unicode, (3,1) — BMP; для кириллицы хватает любой.
    for key in ((3, 10), (0, 4), (3, 1), (0, 3)):
        if key in subs:
            sub = subs[key]
            fmt = struct.unpack(">H", buf[sub:sub + 2])[0]
            if fmt == 12:
                return _cmap12(buf, sub)
            if fmt == 4:
                return _cmap4(buf, sub)
    raise ValueError("не нашёл пригодной таблицы cmap")


def inspect(path):
    with open(path, "rb") as f:
        buf = f.read()
    t = _tables(buf)
    if "name" not in t or "cmap" not in t:
        raise ValueError("нет таблицы name или cmap")
    nm = _names(buf, t["name"][0])
    cov = _coverage(buf, t["cmap"][0])
    return {
        "file": path,
        "ps": nm.get(NAME_PS, ""),
        "family": nm.get(NAME_FAMILY, ""),
        "style": nm.get(NAME_STYLE, ""),
        "missing": {
            "cyrillic": [c for c in CYRILLIC if ord(c) not in cov],
            "latin": [c for c in LATIN if ord(c) not in cov],
            "digits": [c for c in DIGITS if ord(c) not in cov],
            "punct": [c for c in PUNCT if ord(c) not in cov],
        },
    }


def installed_files():
    """Имена файлов шрифтов, установленных в Windows — системно и для пользователя."""
    out = set()
    for d in (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")):
        if d and os.path.isdir(d):
            try:
                out.update(n.lower() for n in os.listdir(d))
            except OSError:
                pass
    return out


def _collect(target):
    if os.path.isfile(target):
        return [target]
    found = []
    for root, _dirs, files in os.walk(target):
        for n in sorted(files):
            if n.lower().endswith((".otf", ".ttf")):
                found.append(os.path.join(root, n))
    return found


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else FONTS_DIR
    if not os.path.exists(target):
        print("нет такого пути: " + target)
        return 1

    files = _collect(target)
    if not files:
        print("шрифтов (.otf/.ttf) не найдено в " + target)
        return 1

    system = installed_files()
    bad = 0
    for path in files:
        try:
            info = inspect(path)
        except Exception as e:
            print("%-28s ОШИБКА: %s" % (os.path.basename(path), e))
            bad += 1
            continue

        gaps = [(k, v) for k, v in info["missing"].items() if v]
        here = os.path.basename(path).lower() in system
        mark = "ok " if not gaps and here else "!! "
        print("%s%-26s %-22s %-12s %s" % (
            mark, info["ps"] or "(нет PostScript-имени)", info["family"], info["style"],
            "установлен" if here else "НЕ УСТАНОВЛЕН"))
        for k, v in gaps:
            print("      нет глифов (%s): %s" % (k, "".join(v)))
        if gaps or not here:
            bad += 1

    if bad:
        print("")
        print("Шрифт ставится так: выделить файлы, правый клик, «Установить для")
        print("всех пользователей», затем перезапустить Photoshop — список")
        print("шрифтов он читает только на старте.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

"""Сквозная проверка связки: контейнер -> мост -> Photoshop -> PSD.

Смысл именно в сквозном прогоне. Каждое звено по отдельности уже проверено;
ломается обычно шов между ними — координаты, кодировки, пути.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge

# Тестовая глава внутри смонтированного /pages.
CHAPTER = "Вечно регрессирующий рыцарь _ A knight who lives for one day/Том 1_ ._ Глава 0"
PAGE = "0010.jpeg"

HOST_ROOT = os.environ.get("PAGES_HOST", "E:/mwx-json/downloads/mangabuff")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out")


def main():
    ok = True

    print("[1/4] health контейнера")
    try:
        h = bridge.health()
        print("      ", json.dumps(h, ensure_ascii=False))
        if not h.get("pages_mounted"):
            print("       ПРОБЛЕМА: /pages не смонтирован")
            ok = False
    except Exception as e:
        print("       НЕДОСТУПЕН:", e)
        return 1

    print("[2/4] анализ страницы")
    rel = CHAPTER + "/" + PAGE
    a = bridge.analyze(rel)
    print("       страница %sx%s, регионов: %d" % (a["width"], a["height"], len(a["regions"])))
    for w in a.get("warnings", []):
        print("       ! " + w)
    for r in a["regions"][:12]:
        print("       %s %-8s bbox=%-22s строк=%d кегль=%-3d conf=%.2f  %s"
              % (r["id"], r["kind"], str(r["bbox"]), r["lines"], r["font_px"], r["conf"],
                 (r["text"][:40] or "<пусто>")))
    if not a["regions"]:
        print("       ПРОБЛЕМА: детект не нашёл ничего")
        return 1

    # Сохраняем анализ — он же будет входом для перевода.
    os.makedirs(OUT_DIR, exist_ok=True)
    ap = os.path.join(OUT_DIR, PAGE + ".analysis.json")
    with open(ap, "w", encoding="utf-8") as f:
        json.dump(a, f, ensure_ascii=False, indent=2)
    print("       анализ ->", ap)

    print("[3/4] подстановка тестового перевода")
    # Ставим заведомо длинную русскую строку: проверяем и кириллицу,
    # и подгон кегля, и переносы разом.
    probe = ("ЭТО ПРОВЕРОЧНЫЙ ДЛИННЫЙ ТЕКСТ ДЛЯ ПОДГОНА КЕГЛЯ "
             "И ПРОВЕРКИ ПЕРЕНОСОВ ПО СЛОГАМ")
    for r in a["regions"]:
        if r["kind"] in ("bubble", "caption") and r["font_px"] >= 12:
            r["translation"] = probe
            print("       перевод -> %s (%s)" % (r["id"], r["kind"]))
            break
    else:
        print("       ! подходящего региона нет, верстать нечего")

    print("[4/4] Photoshop: стирание + вёрстка")
    src = os.path.join(HOST_ROOT, CHAPTER, PAGE)
    if not os.path.isfile(src):
        print("       ПРОБЛЕМА: нет файла на хосте:", src)
        return 1
    res = bridge.render(a, src, OUT_DIR)
    for s in res["report"]:
        mark = "OK  " if s["ok"] else "FAIL"
        if not s["ok"]:
            ok = False
        print("       %s %-14s %s" % (mark, s["step"], s["info"][:90]))
    print("       PSD ->", res["psd"])
    print("       PNG ->", res["png"])

    print("\nИТОГ:", "связка работает" if ok else "есть падения, см. FAIL выше")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

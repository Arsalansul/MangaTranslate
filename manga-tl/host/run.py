"""Прогон главы: папка с исходниками -> папка с PSD и PNG.

Один вызов на всю главу. Каждая страница проходит четыре шага, и каждый
сохраняется на диск: анализ, перевод, стирание, вёрстка. Промежуточный
<страница>.analysis.json — не отладочный мусор, а точка вмешательства:
перевод в нём можно поправить руками и пересобрать страницу с --reuse,
не тратя ни детект, ни модель.

Только stdlib: тяжёлые зависимости живут в контейнере.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge
import translate

EXTS = (".jpg", ".jpeg", ".png", ".webp")
HERE = os.path.dirname(os.path.abspath(__file__))


def _norm(p):
    return os.path.normpath(os.path.abspath(p)).replace(os.sep, "/")


def list_pages(full_dir):
    if not os.path.isdir(full_dir):
        raise SystemExit("Нет такого каталога: " + full_dir)
    names = sorted(n for n in os.listdir(full_dir) if n.lower().endswith(EXTS))
    if not names:
        raise SystemExit("В каталоге нет картинок (%s): %s" % (", ".join(EXTS), full_dir))
    return names


def preflight(args):
    """Проверяем всё, что можно проверить, до первой страницы.

    Глава — это десятки минут работы. Узнать на двадцатой странице, что не
    задан ключ или не поднят контейнер, дороже, чем проверить заранее.
    """
    try:
        h = bridge.health()
    except Exception as e:
        raise SystemExit(
            "CV-контейнер недоступен по %s: %s\n"
            "Поднять: docker compose up -d" % (bridge.CV_URL, e))

    needs_model = not (args.no_translate or args.erase_only or args.reuse)
    if needs_model and not args.api_key:
        raise SystemExit(
            "Нет ключа для перевода. Задайте ANTHROPIC_API_KEY или --api-key.\n"
            "Без модели: --no-translate (только стирание) или --reuse "
            "(взять переводы из ранее сохранённых analysis.json).")
    return h


def process(name, full_dir, out_dir, args, glossary):
    src = os.path.join(full_dir, name)
    stem = os.path.splitext(name)[0]
    apath = os.path.join(out_dir, stem + ".analysis.json")
    row = {"page": name, "regions": 0, "translated": 0, "ok": False, "note": ""}

    # --- анализ -------------------------------------------------------
    analysis = None
    if args.reuse and os.path.isfile(apath):
        with open(apath, encoding="utf-8") as f:
            analysis = json.load(f)
        row["note"] = "анализ из файла"
    if analysis is None:
        analysis = bridge.analyze_file(src, lang=args.lang)

    row["regions"] = len(analysis["regions"])
    for w in analysis.get("warnings", []):
        print("       ! " + w)
    if not analysis["regions"]:
        row["note"] = "текст не найден"
        return row

    # --- перевод ------------------------------------------------------
    already = sum(1 for r in analysis["regions"] if (r.get("translation") or "").strip())
    if args.erase_only or args.no_translate:
        pass
    elif args.reuse and already:
        row["translated"] = already
    else:
        row["translated"] = translate.translate_page(
            analysis, args.api_key, model=args.model, glossary=glossary)
    print("       регионов %d, с переводом %d" % (row["regions"], row["translated"]))

    # Сохраняем до Photoshop: если он упадёт, перевод не потеряется.
    with open(apath, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)

    if args.analyze_only:
        row["ok"] = True
        row["note"] = "только анализ"
        return row

    # --- стирание и вёрстка -------------------------------------------
    res = bridge.render(analysis, src, out_dir, font=args.font, erase_only=args.erase_only)
    fails = [s for s in res["report"] if not s["ok"]]
    row["ok"] = not fails
    if fails:
        row["note"] = "; ".join("%s: %s" % (s["step"], s["info"][:60]) for s in fails[:3])
        for s in fails:
            print("       FAIL %-14s %s" % (s["step"], s["info"][:90]))
    return row


def main():
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Перевести главу: папка с исходниками -> папка с PSD и PNG.")
    p.add_argument("target", help="папка с картинками (или одна картинка); "
                                  "любая папка на диске, класть никуда не надо")
    p.add_argument("--out", help="куда складывать результат (по умолчанию out/<имя папки>)")
    p.add_argument("--font", default=bridge.DEFAULT_FONT,
                   help="PostScript-имя шрифта; список: python host/fontcheck.py")
    p.add_argument("--lang", default="eng", help="язык OCR (eng)")
    p.add_argument("--model", default=translate.DEFAULT_MODEL, help="модель перевода")
    p.add_argument("--api-key", default=translate.api_key_from_env(),
                   help="ключ Anthropic (по умолчанию из ANTHROPIC_API_KEY)")
    p.add_argument("--glossary", help='JSON {"Enkrid": "Энкрид"} — имена и термины')
    p.add_argument("--reuse", action="store_true",
                   help="брать анализ и перевод из уже сохранённых analysis.json")
    p.add_argument("--no-translate", action="store_true",
                   help="не звать модель: только детект и стирание")
    p.add_argument("--erase-only", action="store_true", help="стереть, но не верстать")
    p.add_argument("--analyze-only", action="store_true", help="не трогать Photoshop вовсе")
    p.add_argument("--limit", type=int, help="первые N страниц")
    p.add_argument("--start", help="начать с этой страницы, например 0007.jpeg")
    args = p.parse_args()

    full_dir = _norm(args.target)
    if not os.path.exists(full_dir):
        raise SystemExit("Нет такого пути: " + full_dir)
    if os.path.isfile(full_dir):
        # Одна страница — тот же прогон, просто список из одного имени.
        full_dir, names = os.path.dirname(full_dir), [os.path.basename(full_dir)]
    else:
        names = list_pages(full_dir)

    if args.start:
        if args.start not in names:
            raise SystemExit("Нет такой страницы в главе: " + args.start)
        names = names[names.index(args.start):]
    if args.limit:
        names = names[:args.limit]

    out_dir = _norm(args.out or os.path.join(HERE, "..", "out", os.path.basename(full_dir)))
    os.makedirs(out_dir, exist_ok=True)

    h = preflight(args)
    glossary = translate.load_glossary(args.glossary)

    print("глава:   %s" % full_dir)
    print("выход:   %s" % out_dir)
    print("детект:  %s, OCR: %s, шрифт: %s" % (h["detector"], h["ocr"], args.font))
    if glossary:
        print("глоссарий: %d записей" % len(glossary))
    print("страниц: %d\n" % len(names))

    rows, t0 = [], time.time()
    for i, name in enumerate(names, 1):
        print("[%d/%d] %s" % (i, len(names), name))
        try:
            row = process(name, full_dir, out_dir, args, glossary)
        except KeyboardInterrupt:
            print("\nпрервано; сделанное лежит в " + out_dir)
            break
        except Exception as e:
            # Одна испорченная страница не должна ронять главу: остальные
            # всё равно нужны, а к этой вернёмся через --start.
            row = {"page": name, "regions": 0, "translated": 0, "ok": False,
                   "note": "%s: %s" % (type(e).__name__, e)}
            print("       ОШИБКА: %s" % e)
        rows.append(row)

    bad = [r for r in rows if not r["ok"]]
    print("\n%-22s %7s %7s  %s" % ("страница", "регион", "перев", "заметка"))
    for r in rows:
        print("%s%-20s %7d %7d  %s" % ("  " if r["ok"] else "! ", r["page"][:20],
                                       r["regions"], r["translated"], r["note"][:50]))
    print("\nготово %d из %d за %d с -> %s"
          % (len(rows) - len(bad), len(rows), int(time.time() - t0), out_dir))

    with open(os.path.join(out_dir, "run.report.json"), "w", encoding="utf-8") as f:
        json.dump({"chapter": full_dir, "out": out_dir, "font": args.font,
                   "model": args.model, "pages": rows}, f, ensure_ascii=False, indent=2)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

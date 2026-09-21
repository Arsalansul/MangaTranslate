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
import types
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge
import translate

EXTS = (".jpg", ".jpeg", ".png", ".webp")
HERE = os.path.dirname(os.path.abspath(__file__))


# Модель, которая не ответила столько страниц подряд, не ответит и на
# остальные: лимит или неверное имя не рассасываются сами.
MAX_TRANSLATE_FAILS = 3


class Cancelled(Exception):
    """Прогон остановлен снаружи, а не сломался.

    Отдельный тип нужен, чтобы отмена не попала в общий except и не была
    записана как «одна упавшая страница», после которой глава едет дальше.
    """


def _plain(msg, stage=None, quiet=False, **kw):
    """Прогресс в консоль: всё, кроме самой строки, здесь не нужно.

    `**kw` не про запас: веб-слой добавляет к вызовам свои поля (номер
    страницы, доля готового), и без него такой вызов уронил бы главу
    TypeError'ом посреди прогона.
    """
    print(msg)


def _stop(should_stop):
    """Точка, в которой прогон соглашается остановиться.

    Жёстко прервать нельзя: DoJavaScript крутится внутри Photoshop и
    переживёт смерть нашего процесса, а следующий вызов упрётся в занятое
    приложение. Поэтому отмена — всегда «доработаю шаг и выйду».
    """
    if should_stop is not None and should_stop():
        raise Cancelled()


def _norm(p):
    return os.path.normpath(os.path.abspath(p)).replace(os.sep, "/")


def _save(path, analysis):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)


def list_pages(full_dir):
    if not os.path.isdir(full_dir):
        raise SystemExit("Нет такого каталога: " + full_dir)
    names = sorted(n for n in os.listdir(full_dir) if n.lower().endswith(EXTS))
    if not names:
        raise SystemExit("В каталоге нет картинок (%s): %s" % (", ".join(EXTS), full_dir))
    return names


def settings(target, out=None, font=None, lang=None, target_lang=None,
             provider=None, model=None, api_key=None,
             api_url=None, glossary=None, reuse=False, no_translate=False,
             erase_only=False, analyze_only=False, limit=None, start=None):
    """Настройки прогона одним объектом — и из argparse, и из кода.

    Единственное место, где перечислены все настройки и их умолчания: иначе
    веб-слою пришлось бы повторять список argparse и расходиться с ним.
    Имена полей — как у флагов, чтобы settings(**vars(p.parse_args()))
    проходил без перекладывания.

    Умолчания подставляются здесь, а не в argparse, ради проекта: между
    разбором флагов и этим вызовом стоит with_project, и он должен отличать
    «человек не задал шрифт» от «человек задал шрифт по умолчанию».
    """
    return types.SimpleNamespace(
        target=target, out=out, font=font or bridge.DEFAULT_FONT,
        lang=lang or translate.DEFAULT_SOURCE,
        target_lang=target_lang or translate.DEFAULT_TARGET,
        provider=provider or translate.DEFAULT_PROVIDER,
        model=model, api_key=api_key, api_url=api_url,
        glossary=glossary, reuse=reuse, no_translate=no_translate,
        erase_only=erase_only, analyze_only=analyze_only, limit=limit, start=start)


def with_project(kw):
    """Подставить настройки проекта туда, где флаг не задан.

    Проект — это ответ на вопрос «чем гнать вот эту серию»: шрифт, языки,
    модель, глоссарий и папка, куда складывать. Явный флаг всегда сильнее:
    проект задаёт умолчания, а не запрещает от них отступить.

    project импортируется здесь, а не наверху: project читает у run список
    расширений и list_pages, и встречный импорт замкнул бы круг.
    """
    path = kw.pop("project", None)
    if not path:
        return kw
    import project
    try:
        proj = project.load(path)
    except project.ProjectError as e:
        raise SystemExit(str(e))

    for key, value in proj["settings"].items():
        if kw.get(key) is None:
            kw[key] = value

    # Главу можно назвать именем, а не путём: где она лежит, знает проект.
    src = project.source_dir(proj)
    target = (kw.get("target") or "").strip()
    if src and target and not os.path.exists(_norm(target)):
        inside = os.path.join(src, target)
        if os.path.exists(inside):
            kw["target"] = target = inside
    if not kw.get("out"):
        name = os.path.basename(os.path.normpath(_norm(target)))
        kw["out"] = os.path.join(project.out_root(proj), name)
    gl = project.glossary_path(proj)
    if not kw.get("glossary") and os.path.isfile(gl):
        kw["glossary"] = gl
    return kw


def resolve(args):
    """Что именно гоним: папка главы, список страниц, куда складывать."""
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
    return full_dir, names, out_dir


def prepare(args):
    """Всё, что должно быть готово до первой страницы: движок, проверки, глоссарий."""
    engine = translate.Engine(args.provider, model=args.model,
                              api_key=args.api_key, url=args.api_url,
                              src_lang=args.lang, target=args.target_lang)
    health = preflight(args, engine)
    glossary = translate.load_glossary(args.glossary)
    return engine, glossary, health


def preflight(args, engine):
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

    # Словаря может не быть в образе: тогда Tesseract молча вернёт пустые
    # регионы, и страница выйдет чистой без единой ошибки. Ловим здесь.
    # Старый контейнер списка не отдаёт — тогда проверять нечего.
    langs = h.get("langs") or []
    if langs and args.lang not in langs:
        raise SystemExit(
            "В образе нет словаря '%s'. Есть: %s\n"
            "Добавить пакет tesseract-ocr-%s в container/Dockerfile и пересобрать."
            % (args.lang, ", ".join(langs), args.lang.replace("_", "-")))

    if not (args.no_translate or args.erase_only or args.reuse):
        try:
            engine.check()
        except translate.TranslateError as e:
            raise SystemExit(str(e))
    return h


def process(name, full_dir, out_dir, args, engine, glossary,
            report=_plain, should_stop=None):
    _stop(should_stop)
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
        report("       ! " + w, stage="detect", quiet=True)
    if not analysis["regions"]:
        row["note"] = "текст не найден"
        return row

    # Детект стоит десяти секунд на страницу, перевод — чужой сети. Кладём
    # анализ на диск сразу, чтобы упавший перевод не заставлял детектить заново:
    # повтор с --reuse возьмёт готовые регионы.
    _save(apath, analysis)

    # --- перевод ------------------------------------------------------
    targets = [r for r in analysis["regions"] if translate.translatable(r)]
    already = sum(1 for r in targets if (r.get("translation") or "").strip())
    if args.erase_only or args.no_translate:
        row["translated"] = already
    elif args.reuse and already == len(targets):
        # Всё переведено — модель не трогаем.
        row["translated"] = already
    else:
        try:
            # В режиме --reuse дозаполняем дыры: страница, где модель
            # ответила наполовину, иначе считалась бы готовой навсегда.
            # Уже заполненное не перезаписываем — там могла быть правка руками.
            row["translated"] = already + engine.translate_page(
                analysis, glossary=glossary, keep_filled=args.reuse)
        except translate.TranslateError as e:
            # Строку отдаём наверх вместе с ошибкой: иначе в итоговой
            # таблице у страницы окажется ноль регионов, хотя детект их нашёл.
            e.row = row
            raise
        # Реплику без перевода не стирают — значит, на готовой странице
        # останется исходный текст. Само по себе это правильно (пустой бабл
        # хуже чужого), но узнавать об этом, разглядывая результат, не дело:
        # «регионов 12, с переводом 3» ничем не отличается от нормы, потому
        # что большинство регионов — звуки, их и не переводят.
        row["blank"] = [r["id"] for r in targets
                        if not (r.get("translation") or "").strip()]
    report("       регионов %d, с переводом %d" % (row["regions"], row["translated"]),
           stage="translate")
    if row.get("blank"):
        report("       ! без перевода, текст остался исходным: %s"
               % ", ".join(row["blank"]), stage="translate", quiet=True)

    # Сохраняем до Photoshop: если он упадёт, перевод не потеряется.
    _save(apath, analysis)

    if args.analyze_only:
        row["ok"] = True
        row["note"] = "только анализ"
        return row

    # --- стирание -----------------------------------------------------
    # Стирает контейнер: ровный фон заливкой, текст поверх рисунка — моделью.
    # Photoshop раньше делал то же Content-Aware Fill'ом, но тот собирает
    # заплатку из кусков страницы, а страница в этот момент ещё в тексте.
    _stop(should_stop)
    page, erase_ids = src, None
    try:
        cl = bridge.clean(analysis, src, os.path.join(out_dir, stem + ".clean.png"),
                          force=args.erase_only)
        page, erase_ids = cl["path"], cl["left"]
        report("       стёрто: ровных %d, поверх арта %d (проходов %d)%s"
               % (cl["flat"], cl["art"], cl["passes"],
                  ", Photoshop'у осталось %d" % len(cl["left"]) if cl["left"] else ""),
               stage="erase")
    except urllib.error.URLError as e:
        # Старый контейнер без /inpaint или он не ответил — стирает Photoshop,
        # как до сих пор. Хуже, но страница всё равно выйдет.
        report("       стирание в контейнере не вышло (%s), стирает Photoshop" % e,
               stage="erase", quiet=True)

    # --- вёрстка -------------------------------------------------------
    _stop(should_stop)
    res = bridge.render(analysis, src, out_dir, font=args.font,
                        erase_only=args.erase_only, page_img=page,
                        erase_ids=erase_ids, lang=args.target_lang)
    fails = [s for s in res["report"] if not s["ok"]]
    row["ok"] = not fails
    if fails:
        row["note"] = "; ".join("%s: %s" % (s["step"], s["info"][:60]) for s in fails[:3])
        for s in fails:
            report("       FAIL %-14s %s" % (s["step"], s["info"][:90]), stage="render")
    return row


def run_chapter(names, full_dir, out_dir, args, engine, glossary,
                report=_plain, should_stop=None):
    rows = []
    tl_fails = 0
    for i, name in enumerate(names, 1):
        try:
            _stop(should_stop)
            report("[%d/%d] %s" % (i, len(names), name), stage="page")
            row = process(name, full_dir, out_dir, args, engine, glossary,
                          report=report, should_stop=should_stop)
            tl_fails = 0
        except KeyboardInterrupt:
            report("\nпрервано; сделанное лежит в " + out_dir, stage="summary")
            break
        except Cancelled:
            # Раньше общего except: иначе отмена станет «упавшей страницей»,
            # и глава поедет дальше — ровно то, чего у нас не просили.
            report("\nотменено; сделанное лежит в " + out_dir, stage="summary")
            break
        except translate.TranslateError as e:
            # Перевод отказал — это про всю главу, а не про эту страницу.
            tl_fails += 1
            row = getattr(e, "row", None) or {"page": name, "regions": 0,
                                              "translated": 0, "ok": False}
            row["note"] = "перевод: " + str(e).splitlines()[0][:60]
            report("       ОШИБКА перевода: %s" % e, stage="page")
        except Exception as e:
            # Одна испорченная страница не должна ронять главу: остальные
            # всё равно нужны, а к этой вернёмся через --start.
            row = {"page": name, "regions": 0, "translated": 0, "ok": False,
                   "note": "%s: %s" % (type(e).__name__, e)}
            report("       ОШИБКА: %s" % e, stage="page")
        rows.append(row)
        if tl_fails >= MAX_TRANSLATE_FAILS:
            report("\nМодель молчит %d страницы подряд — останавливаюсь, чтобы не\n"
                   "перемалывать главу впустую. Смените --model и запустите с\n"
                   "--reuse: разметка уже на диске, детект не повторится."
                   % tl_fails, stage="summary")
            break
    return rows


def write_report(out_dir, full_dir, args, engine, rows):
    with open(os.path.join(out_dir, "run.report.json"), "w", encoding="utf-8") as f:
        json.dump({"chapter": full_dir, "out": out_dir, "font": args.font,
                   "lang": args.lang, "target_lang": args.target_lang,
                   "engine": engine.describe(), "pages": rows}, f, ensure_ascii=False, indent=2)


def main():
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Перевести главу: папка с исходниками -> папка с PSD и PNG.")
    p.add_argument("target", help="папка с картинками (или одна картинка); "
                                  "любая папка на диске, класть никуда не надо")
    p.add_argument("--out", help="куда складывать результат (по умолчанию out/<имя папки>)")
    p.add_argument("--project", help="папка проекта: оттуда шрифт, языки, "
                                     "модель, глоссарий и куда складывать")
    p.add_argument("--font", help="PostScript-имя шрифта (по умолчанию %s); "
                                  "список: python host/fontcheck.py" % bridge.DEFAULT_FONT)
    p.add_argument("--lang", choices=sorted(translate.SOURCES),
                   help="язык исходника: чем распознавать и с чего переводить "
                        "(по умолчанию %s)" % translate.DEFAULT_SOURCE)
    p.add_argument("--target-lang", choices=sorted(translate.TARGETS),
                   help="язык перевода (по умолчанию %s)" % translate.DEFAULT_TARGET)
    p.add_argument("--provider", choices=sorted(translate.BACKENDS),
                   help="кто переводит (по умолчанию %s); "
                        "список: python host/translate.py" % translate.DEFAULT_PROVIDER)
    p.add_argument("--model", help="модель провайдера (по умолчанию его обычная)")
    p.add_argument("--api-key", help="ключ провайдера (по умолчанию из его переменной)")
    p.add_argument("--api-url", help="свой адрес OpenAI-совместимого сервера")
    p.add_argument("--glossary", help='путь к JSON {"Enkrid": "Энкрид"} — '
                                     'имена и термины; в проекте берётся его')
    p.add_argument("--reuse", action="store_true",
                   help="брать анализ и перевод из уже сохранённых analysis.json")
    p.add_argument("--no-translate", action="store_true",
                   help="не звать модель: только детект и стирание")
    p.add_argument("--erase-only", action="store_true", help="стереть, но не верстать")
    p.add_argument("--analyze-only", action="store_true", help="не трогать Photoshop вовсе")
    p.add_argument("--limit", type=int, help="первые N страниц")
    p.add_argument("--start", help="начать с этой страницы, например 0007.jpeg")
    args = settings(**with_project(vars(p.parse_args())))

    full_dir, names, out_dir = resolve(args)
    engine, glossary, h = prepare(args)

    print("глава:   %s" % full_dir)
    print("выход:   %s" % out_dir)
    # OCR из /health отчитывается языком по умолчанию; фактический язык
    # страницы — тот, что мы просим, поэтому показываем его, а не ответ.
    print("детект:  %s, OCR: tesseract-%s, шрифт: %s" % (h["detector"], args.lang, args.font))
    print("языки:   %s -> %s" % (args.lang, args.target_lang))
    print("перевод: %s" % engine.describe())
    if glossary:
        print("глоссарий: %d записей" % len(glossary))
    print("страниц: %d\n" % len(names))

    t0 = time.time()
    rows = run_chapter(names, full_dir, out_dir, args, engine, glossary)

    bad = [r for r in rows if not r["ok"]]
    print("\n%-22s %7s %7s  %s" % ("страница", "регион", "перев", "заметка"))
    for r in rows:
        print("%s%-20s %7d %7d  %s" % ("  " if r["ok"] else "! ", r["page"][:20],
                                       r["regions"], r["translated"], r["note"][:50]))
    print("\nготово %d из %d за %d с -> %s"
          % (len(rows) - len(bad), len(rows), int(time.time() - t0), out_dir))

    write_report(out_dir, full_dir, args, engine, rows)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

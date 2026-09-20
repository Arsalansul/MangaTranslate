"""Распознавание текста в найденных регионах.

v1 — Tesseract. Выбран не потому, что лучший, а потому, что лёгкий:
ставится из apt, не тянет torch и уверенно читает капсовый английский
в бабблах. Для стилизованных SFX он слаб — такие регионы помечаются
низкой уверенностью, и решение остаётся за человеком.

Смена движка не должна менять Region — see app/schema.py.
"""
from typing import List, Optional
import cv2
import numpy as np
import pytesseract
from pytesseract import Output

from .schema import Region

OCR_VERSION = "v1"
DEFAULT_LANG = "eng"

# У этих письменностей пробела между словами нет, и Tesseract отдаёт
# отдельным «словом» чуть ли не каждый знак. Склеенные через пробел, они
# превращаются в текст с дырами — и для перевода, и для вёрстки. Корейский
# сюда не входит: там пробелы настоящие, их надо сохранить.
NO_SPACE_LANGS = ("chi_sim", "chi_tra", "jpn")

# Суффикс вертикальных словарей Tesseract: chi_sim_vert, chi_tra_vert, jpn_vert.
# Отдельного флага в запросе нет намеренно — ориентация это свойство словаря,
# и она сама приезжает с выбранным языком, попадая и в analysis.json.
VERT_SUFFIX = "_vert"

# Tesseract заметно точнее на увеличенном изображении: комикс-текст
# в вебтуне часто мельче, чем то, на чём модель обучалась.
UPSCALE = 2.0
PSM_BLOCK = 6   # единый блок текста
PSM_LINE = 7    # одна строка
PSM_VERT = 5    # блок вертикального текста, колонки справа налево


def ocr_name(lang: str = DEFAULT_LANG) -> str:
    """Имя движка вместе с языком.

    Язык — часть того, чем распознавали: страница, прочитанная корейским
    словарём, не должна отчитываться английской, иначе по сохранённому
    analysis.json не понять, почему текст вышел кашей.
    """
    return "tesseract-%s-%s" % (lang or DEFAULT_LANG, OCR_VERSION)


# Язык страницы в /health ещё неизвестен, там имя движка без него.
OCR_NAME = ocr_name()


def _base_lang(lang: str) -> str:
    """Письменность без вертикального суффикса.

    Язык может прийти связкой ("chi_sim+eng"); ведущий в ней и определяет
    письменность страницы. chi_sim_vert — тот же китайский, и правила
    склейки слов у него те же.
    """
    head = (lang or DEFAULT_LANG).split("+")[0]
    return head[:-len(VERT_SUFFIX)] if head.endswith(VERT_SUFFIX) else head


def is_vertical(lang: str) -> bool:
    """Вертикальный ли набор. Решает и OCR, и сборка строк в детекторе."""
    return (lang or "").split("+")[0].endswith(VERT_SUFFIX)


def _word_sep(lang: str) -> str:
    return "" if _base_lang(lang) in NO_SPACE_LANGS else " "


def _no_dictionary(err: str) -> bool:
    """Отличает «нет словаря» от прочих бед Tesseract'а."""
    low = err.lower()
    return ("tessdata" in low
            or "failed loading language" in low
            or "couldn't load any languages" in low)


def _prep(crop: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    gray = cv2.resize(gray, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
    # Текст должен быть тёмным на светлом — Tesseract этого ждёт.
    if np.median(gray) < 127:
        gray = cv2.bitwise_not(gray)
    _, binimg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.copyMakeBorder(binimg, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)


def read_regions(img_bgr: np.ndarray, regions: List[Region], lang: str = DEFAULT_LANG,
                 warnings: Optional[List[str]] = None) -> List[Region]:
    H, W = img_bgr.shape[:2]
    sep = _word_sep(lang)
    vertical = is_vertical(lang)
    for r in regions:
        x, y, w, h = r.bbox
        x, y = max(0, x), max(0, y)
        crop = img_bgr[y:min(H, y + h), x:min(W, x + w)]
        if crop.size == 0:
            continue

        prepped = _prep(crop)
        # Вертикальному набору psm 7 не подходит даже на одной колонке:
        # «одна строка» для Tesseract горизонтальна, и колонка иероглифов
        # читается как столбик отдельных знаков.
        if vertical:
            psm = PSM_VERT
        else:
            psm = PSM_LINE if r.lines <= 1 else PSM_BLOCK
        cfg = "--oem 3 --psm %d" % psm

        try:
            data = pytesseract.image_to_data(prepped, lang=lang, config=cfg, output_type=Output.DICT)
        except pytesseract.TesseractError as e:
            if _no_dictionary(str(e)):
                # Без словаря падает не эта страница, а все: дальше читать
                # нечем. Молча вернуть пустые регионы с conf 0 — худшее, что
                # можно сделать: выглядит как нечитаемая глава, а не как
                # незакрытый apt-get.
                if warnings is not None:
                    # Вертикальных словарей в apt нет вовсе, и советовать
                    # несуществующий пакет — отправить человека по ложному
                    # следу: они кладутся файлом из tessdata_fast.
                    how = ("Файл %s.traineddata кладётся в образ из "
                           "tessdata_fast — see container/Dockerfile."
                           % lang if is_vertical(lang) else
                           "Нужен пакет tesseract-ocr-%s в образе."
                           % lang.replace("_", "-"))
                    warnings.append(
                        "Tesseract не нашёл словарь языка '%s' — страница не "
                        "распознана. %s" % (lang, how))
                break
            r.text, r.conf = "", 0.0
            continue
        except Exception:
            r.text, r.conf = "", 0.0
            continue

        words, confs, keys = [], [], []
        for i, word in enumerate(data.get("text", [])):
            word = (word or "").strip()
            if not word:
                continue
            try:
                c = float(data["conf"][i])
            except (ValueError, KeyError, IndexError):
                c = -1.0
            if c < 0:
                continue
            words.append(word)
            confs.append(c)
            keys.append(tuple(data[k][i] for k in ("block_num", "par_num", "line_num")))

        if r.keep_lines and len(keys) == len(words):
            # Список, содержание, титры: перенос там смысловой. Слитые в абзац,
            # строки уедут от линеек и от колонки номеров, поэтому границы строк
            # Tesseract'а тащим дальше как есть — через перевод и до вёрстки.
            lines, prev = [], None
            for word, key in zip(words, keys):
                if key != prev:
                    lines.append([])
                    prev = key
                lines[-1].append(word)
            r.text = "\n".join(sep.join(w) for w in lines)
        else:
            r.text = sep.join(words)
        r.conf = round(float(np.mean(confs)) / 100.0, 3) if confs else 0.0

        letters = [ch for ch in r.text if ch.isalpha()]
        r.all_caps = bool(letters) and all(ch.isupper() for ch in letters)
    return regions

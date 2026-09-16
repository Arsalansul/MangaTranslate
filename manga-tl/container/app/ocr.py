"""Распознавание текста в найденных регионах.

v1 — Tesseract. Выбран не потому, что лучший, а потому, что лёгкий:
ставится из apt, не тянет torch и уверенно читает капсовый английский
в бабблах. Для стилизованных SFX он слаб — такие регионы помечаются
низкой уверенностью, и решение остаётся за человеком.

Смена движка не должна менять Region — see app/schema.py.
"""
from typing import List
import cv2
import numpy as np
import pytesseract
from pytesseract import Output

from .schema import Region

OCR_NAME = "tesseract-eng-v1"

# Tesseract заметно точнее на увеличенном изображении: комикс-текст
# в вебтуне часто мельче, чем то, на чём модель обучалась.
UPSCALE = 2.0
PSM_BLOCK = 6   # единый блок текста
PSM_LINE = 7    # одна строка


def _prep(crop: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    gray = cv2.resize(gray, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
    # Текст должен быть тёмным на светлом — Tesseract этого ждёт.
    if np.median(gray) < 127:
        gray = cv2.bitwise_not(gray)
    _, binimg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.copyMakeBorder(binimg, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)


def read_regions(img_bgr: np.ndarray, regions: List[Region], lang: str = "eng") -> List[Region]:
    H, W = img_bgr.shape[:2]
    for r in regions:
        x, y, w, h = r.bbox
        x, y = max(0, x), max(0, y)
        crop = img_bgr[y:min(H, y + h), x:min(W, x + w)]
        if crop.size == 0:
            continue

        prepped = _prep(crop)
        psm = PSM_LINE if r.lines <= 1 else PSM_BLOCK
        cfg = "--oem 3 --psm %d" % psm

        try:
            data = pytesseract.image_to_data(prepped, lang=lang, config=cfg, output_type=Output.DICT)
        except Exception:
            r.text, r.conf = "", 0.0
            continue

        words, confs = [], []
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

        r.text = " ".join(words)
        r.conf = round(float(np.mean(confs)) / 100.0, 3) if confs else 0.0

        letters = [ch for ch in r.text if ch.isalpha()]
        r.all_caps = bool(letters) and all(ch.isupper() for ch in letters)
    return regions

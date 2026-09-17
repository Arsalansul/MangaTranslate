"""HTTP-обёртка над детектом и OCR.

Контейнер намеренно не умеет ни переводить, ни верстать: перевод делает
модель на хосте, вёрстку — Photoshop. Здесь только «глаза» пайплайна.
"""
import io
import os
from typing import List

import cv2
import numpy as np
from fastapi import FastAPI, File, UploadFile, HTTPException
from pydantic import BaseModel

from . import detect as _morph
from . import detect_nn as _nn

# Нейросетевой детектор, если веса примонтированы; иначе морфология.
# Откат нужен не для красоты: без него контейнер не поднимется там, где
# модель ещё не скачана, и связка с Photoshop окажется непроверяемой.
if _nn.available():
    detect, DETECTOR_NAME = _nn.detect, _nn.DETECTOR_NAME
else:
    detect, DETECTOR_NAME = _morph.detect, _morph.DETECTOR_NAME
from .kinds import revise_kinds
from .ocr import read_regions, OCR_NAME
from .schema import PageAnalysis

app = FastAPI(title="manga-tl cv", version="0.1.0")

# Каталог с главами монтируется сюда; хост и контейнер видят одни и те же файлы.
PAGES_ROOT = os.environ.get("PAGES_ROOT", "/pages")


def _analyze(img: np.ndarray, name: str, lang: str) -> PageAnalysis:
    warnings: List[str] = []
    regions = detect(img)
    if not regions:
        warnings.append("Текст не найден: проверьте пороги детекта для этой страницы")
    regions = read_regions(img, regions, lang=lang)

    # Вид региона детектор назначает до OCR, вслепую: текст поверх арта он
    # записывает в звук и тем самым выводит из перевода. Теперь есть чем
    # проверить — прочитанными словами.
    revised = revise_kinds(regions)
    if revised:
        warnings.append("Подписей поверх арта, принятых за звук: %d "
                        "(будут переведены и вписаны по месту)" % revised)

    weak = [r.id for r in regions if r.conf < 0.5]
    if weak:
        warnings.append("Низкая уверенность OCR: " + ", ".join(weak))

    h, w = img.shape[:2]
    return PageAnalysis(
        page=name, width=w, height=h, regions=regions,
        detector=DETECTOR_NAME, ocr=OCR_NAME, warnings=warnings,
    )


@app.get("/health")
def health():
    import pytesseract
    return {
        "ok": True,
        "detector": DETECTOR_NAME,
        "ocr": OCR_NAME,
        "tesseract": str(pytesseract.get_tesseract_version()),
        "pages_root": PAGES_ROOT,
        "pages_mounted": os.path.isdir(PAGES_ROOT),
    }


@app.post("/analyze", response_model=PageAnalysis)
async def analyze(file: UploadFile = File(...), lang: str = "eng"):
    raw = await file.read()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Не удалось прочитать изображение")
    return _analyze(img, file.filename or "upload", lang)


class PathReq(BaseModel):
    path: str      # путь относительно PAGES_ROOT
    lang: str = "eng"


@app.post("/analyze_path", response_model=PageAnalysis)
def analyze_path(req: PathReq):
    # Не выпускаем за пределы смонтированного каталога.
    full = os.path.normpath(os.path.join(PAGES_ROOT, req.path))
    if not full.startswith(os.path.normpath(PAGES_ROOT)):
        raise HTTPException(400, "Путь вне PAGES_ROOT")
    if not os.path.isfile(full):
        raise HTTPException(404, "Нет файла: %s" % req.path)

    img = cv2.imread(full, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Не удалось декодировать: %s" % req.path)
    return _analyze(img, os.path.basename(full), req.lang)


@app.get("/pages")
def list_pages(sub: str = ""):
    base = os.path.normpath(os.path.join(PAGES_ROOT, sub))
    if not os.path.isdir(base):
        raise HTTPException(404, "Нет каталога: %s" % sub)
    exts = (".jpg", ".jpeg", ".png", ".webp")
    return {"root": sub, "files": sorted(f for f in os.listdir(base) if f.lower().endswith(exts))}

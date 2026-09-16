"""Детект текста нейросетью comic-text-detector.

Приходит на смену opencv-morph-v1, который не отличал штрих туши от буквы.
Контракт тот же — Region из app/schema.py, — поэтому ни мост, ни скрипт
Photoshop о замене не знают.

Модель (comictextdetector.pt.onnx из релиза beta-0.2.1 manga-image-translator)
отдаёт три тензора:
    blk [1, 64512, 7]      боксы текстовых блоков, yolov5
    seg [1, 1, 1024, 1024] маска глифов
    det [1, 2, 1024, 1024] карта строк, DBNet

Используется только seg. Голова blk обучена на пузырях манги и на вебтунах
молчит: на тестовой странице она нашла одну вотермарку и пропустила капшен,
который маска взяла целиком. Карта строк потребовала бы постобработки DBNet
(pyclipper, shapely) ради того, что горизонтальная проекция уже чистой маски
даёт точнее и без зависимостей.

Группировка строк в блоки переиспользуется из detect.py: она всегда была
рабочей, ломался только вход — Оцу бинаризует и буквы, и рисунок, а маска
содержит одни буквы.
"""
import os
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .schema import Region
from .detect import _group_lines, _classify, PAD

DETECTOR_NAME = "comic-text-detector-onnx-v1"

MODEL_PATH = os.path.join(os.environ.get("MODELS_DIR", "/models"), "comictextdetector.onnx")

INPUT_SIZE = 1024       # вход модели, фиксированный
MASK_THRESH = 0.3       # порог сигмоиды на маске
TILE_ASPECT = 1.0       # тайл делаем квадратным: модель обучена на страницах
TILE_STEP = 0.8         # шаг тайла, перекрытие 20% — строка не режется пополам
MIN_LINE_H = 6
MIN_LINE_W = 10
KERNEL_FACTOR = 0.9     # ширина ядра склейки строки в долях высоты глифа
DILATE_FACTOR = 0.35    # запас маски стирания в долях высоты глифа
MAX_POLY_PTS = 200      # длиннее полигон Photoshop выделяет заметно медленнее

_session = None


def available() -> bool:
    return os.path.exists(MODEL_PATH)


def _sess():
    global _session
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    return _session


def _infer_tile(tile_bgr: np.ndarray) -> np.ndarray:
    """Маска глифов для одного тайла, в разрешении тайла."""
    h, w = tile_bgr.shape[:2]
    r = min(INPUT_SIZE / h, INPUT_SIZE / w)
    nw, nh = int(round(w * r)), int(round(h * r))

    t = cv2.resize(tile_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    # Паддинг только справа и снизу — так letterbox устроен в оригинале,
    # иначе координаты уедут на половину поля.
    t = cv2.copyMakeBorder(t, 0, INPUT_SIZE - nh, 0, INPUT_SIZE - nw,
                           cv2.BORDER_CONSTANT, value=(0, 0, 0))

    x = cv2.cvtColor(t, cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    seg = _sess().run(["seg"], {"images": x})[0]

    m = (seg[0, 0][:nh, :nw] > MASK_THRESH).astype(np.uint8) * 255
    return cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)


def text_mask(img_bgr: np.ndarray) -> np.ndarray:
    """Маска глифов всей страницы.

    Вебтун-лента в квадрат модели не влезает: 720x3700 сжимается до 199 px
    по ширине, и текст пропадает физически. Поэтому длинное режется на
    квадратные тайлы с перекрытием, а маски складываются по максимуму.
    """
    H, W = img_bgr.shape[:2]
    if H <= W * (TILE_ASPECT + 0.3):
        return _infer_tile(img_bgr)

    tile_h = int(W * TILE_ASPECT)
    step = max(1, int(tile_h * TILE_STEP))
    mask = np.zeros((H, W), np.uint8)
    y = 0
    while True:
        y2 = min(H, y + tile_h)
        mask[y:y2] = np.maximum(mask[y:y2], _infer_tile(img_bgr[y:y2]))
        if y2 >= H:
            break
        y += step
    return mask


def _glyph_scale(mask: np.ndarray) -> int:
    """Медианная высота глифа — от неё считаются все морфологические размеры."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    hs = [stats[i, cv2.CC_STAT_HEIGHT] for i in range(1, n)
          if stats[i, cv2.CC_STAT_AREA] >= 8]
    return int(np.median(hs)) if hs else 12


def _line_boxes(mask: np.ndarray, scale: int) -> List[Tuple[int, int, int, int]]:
    """Склеивает глифы в строки. Ядро шире, чем выше: смыкаем буквы и
    межсловные пробелы, но не слипаем соседние строки."""
    kw = max(6, int(scale * KERNEL_FACTOR))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 3))
    merged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if bh < MIN_LINE_H or bw < MIN_LINE_W:
            continue
        boxes.append((x, y, bw, bh))
    return boxes


def _mask_poly(mask: np.ndarray, x: int, y: int, w: int, h: int, scale: int,
               x2: int, y2: int) -> Optional[List[List[int]]]:
    """Контур стирания по форме текста, а не прямоугольником.

    Для текста поверх рисунка это принципиально: прямоугольная заливка
    затирает арт вокруг букв, а Content-Aware Fill по силуэту — нет.
    Если текст распался на несколько контуров, честнее вернуть None и
    отдать вызывающему прямоугольник, чем склеивать куски наугад.
    """
    H, W = mask.shape
    mx, my = max(0, x - PAD), max(0, y - PAD)
    mx2, my2 = min(W, x2 + PAD), min(H, y2 + PAD)
    crop = mask[my:my2, mx:mx2]
    if crop.size == 0:
        return None

    d = max(3, int(scale * DILATE_FACTOR) | 1)
    grown = cv2.dilate(crop, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d)))

    contours, _ = cv2.findContours(grown, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if len(contours) != 1:
        return None

    approx = cv2.approxPolyDP(contours[0], 2.0, True)
    if len(approx) < 3 or len(approx) > MAX_POLY_PTS:
        return None
    return [[int(p[0][0]) + mx, int(p[0][1]) + my] for p in approx]


def detect(img_bgr: np.ndarray) -> List[Region]:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape

    mask = text_mask(img_bgr)
    scale = _glyph_scale(mask)
    groups = _group_lines(_line_boxes(mask, scale))

    regions: List[Region] = []
    for i, g in enumerate(sorted(groups, key=lambda g: min(p[1] for p in g)), start=1):
        x = min(p[0] for p in g)
        y = min(p[1] for p in g)
        x2 = max(p[0] + p[2] for p in g)
        y2 = max(p[1] + p[3] for p in g)
        w, h = x2 - x, y2 - y

        heights = [p[3] for p in g]
        font_px = int(np.median(heights))
        if len(g) > 1:
            tops = sorted(p[1] for p in g)
            gaps = [tops[j + 1] - tops[j] for j in range(len(tops) - 1)]
            line_h = int(np.median(gaps)) if gaps else int(font_px * 1.2)
        else:
            line_h = int(font_px * 1.2)

        kind, on_art, fg, bg = _classify(gray, x, y, w, h)

        poly = _mask_poly(mask, x, y, w, h, max(font_px, scale), x2, y2)
        if poly is None:
            mx, my = max(0, x - PAD), max(0, y - PAD)
            mx2, my2 = min(W, x2 + PAD), min(H, y2 + PAD)
            poly = [[mx, my], [mx2, my], [mx2, my2], [mx, my2]]

        regions.append(Region(
            id="r%03d" % i,
            bbox=[x, y, w, h],
            mask_poly=poly,
            angle=0.0,
            lines=len(g),
            font_px=font_px,
            line_h_px=line_h,
            kind=kind,
            on_art=on_art,
            fg=fg,
            bg=bg,
        ))
    return regions

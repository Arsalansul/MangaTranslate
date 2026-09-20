"""Детект текстовых блоков на странице манги/вебтуна.

v1 — чистый OpenCV, без нейросетей. Осознанный выбор: он не тянет модели,
поэтому на нём можно сразу проверить связку контейнер <-> Photoshop.
Нейросетевой детектор (comic-text-detector) встанет на его место позже,
отдавая тот же Region, — see app/schema.py.

Логика: глифы -> строки (горизонтальная морфология) -> блоки (группировка
строк по вертикали). Так же, как это делает человек, читая страницу.
"""
from typing import List, Tuple
import cv2
import numpy as np

from .schema import Region

DETECTOR_NAME = "opencv-morph-v1"

# Порогов много, поэтому они собраны здесь, а не размазаны по коду.
MIN_LINE_H = 8          # ниже — это шум или растр, не текст
MAX_LINE_H = 160        # выше — это уже SFX во весь экран или рамка панели
MIN_LINE_W = 12
MAX_FILL_RATIO = 0.92   # почти полностью залитый прямоугольник — это плашка, не текст
LINE_GAP_FACTOR = 0.9   # строки ближе этого (в долях высоты строки) считаем одним блоком
PAD = 6                 # запас маски вокруг bbox: заливать надо шире глифов


def _line_boxes(bin_img: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Склеивает глифы в строки и возвращает рамки строк."""
    h, w = bin_img.shape
    # Ядро шире, чем выше: смыкаем буквы по горизонтали, но не слипаем строки.
    kw = max(6, w // 60)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 3))
    merged = cv2.morphologyEx(bin_img, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if not (MIN_LINE_H <= bh <= MAX_LINE_H):
            continue
        if bw < MIN_LINE_W or bw < bh * 0.6:
            continue
        if cv2.contourArea(c) / float(bw * bh) > MAX_FILL_RATIO:
            continue
        boxes.append((x, y, bw, bh))
    return boxes


def _group_lines(boxes: List[Tuple[int, int, int, int]]) -> List[List[Tuple[int, int, int, int]]]:
    """Собирает строки в блоки: рядом по вертикали и перекрываются по горизонтали."""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
    groups: List[List[Tuple[int, int, int, int]]] = [[boxes[0]]]

    for b in boxes[1:]:
        x, y, bw, bh = b
        placed = False
        for g in groups:
            gx = min(p[0] for p in g)
            gx2 = max(p[0] + p[2] for p in g)
            gy2 = max(p[1] + p[3] for p in g)
            gh = float(np.median([p[3] for p in g]))
            # перекрытие по X
            overlap = min(gx2, x + bw) - max(gx, x)
            if overlap > 0.3 * min(gx2 - gx, bw) and 0 <= y - gy2 <= gh * LINE_GAP_FACTOR:
                g.append(b)
                placed = True
                break
        if not placed:
            groups.append([b])
    return groups


def _classify(gray: np.ndarray, x: int, y: int, w: int, h: int) -> Tuple[str, bool, List[int], List[int]]:
    """Определяет, на чём лежит текст: чистая подложка или рисунок."""
    H, W = gray.shape
    m = 10
    ring = gray[max(0, y - m):min(H, y + h + m), max(0, x - m):min(W, x + w + m)]
    inner = gray[y:y + h, x:x + w]
    if ring.size == 0 or inner.size == 0:
        return "unknown", False, [0, 0, 0], [255, 255, 255]

    bg_val = int(np.median(ring))
    # Разброс фона вокруг блока: ровный фон -> баббл, пёстрый -> поверх арта.
    border = np.concatenate([ring[0, :], ring[-1, :], ring[:, 0], ring[:, -1]])
    on_art = bool(np.std(border) > 28)

    dark_on_light = bg_val > 127
    fg = [0, 0, 0] if dark_on_light else [255, 255, 255]
    bg = [bg_val] * 3

    if on_art:
        kind = "sfx"
    elif h > 0 and w / float(h) > 3.0 and bg_val > 200:
        kind = "caption"
    else:
        kind = "bubble"
    return kind, on_art, fg, bg


def detect(img_bgr: np.ndarray) -> List[Region]:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # Две полярности: тёмный текст на светлом и светлый на тёмном.
    _, dark = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _, light = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    boxes = _line_boxes(dark) + _line_boxes(light)
    groups = _group_lines(boxes)

    regions: List[Region] = []
    H, W = gray.shape
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

        mx, my = max(0, x - PAD), max(0, y - PAD)
        mx2, my2 = min(W, x2 + PAD), min(H, y2 + PAD)

        regions.append(Region(
            id="r%03d" % i,
            bbox=[x, y, w, h],
            mask_poly=[[mx, my], [mx2, my], [mx2, my2], [mx, my2]],
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

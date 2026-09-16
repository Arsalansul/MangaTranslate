"""Детект текста нейросетью comic-text-detector.

Пришёл на смену opencv-morph-v1, который не отличал штрих туши от буквы.
Контракт тот же — Region из app/schema.py, — поэтому ни мост, ни скрипт
Photoshop о замене не знают.

Модель (comictextdetector.pt.onnx из релиза beta-0.2.1 manga-image-translator)
отдаёт три тензора:
    blk [1, 64512, 7]      боксы текстовых блоков, yolov5, 2 класса
    seg [1, 1, 1024, 1024] маска глифов
    det [1, 2, 1024, 1024] карта строк, DBNet

Используются первые два, и каждый за то, в чём силён.

seg даёт геометрию букв: где именно чернила, а где рисунок. На нём строятся
строки и контур стирания.

blk даёт границы балуна — то, чего из одной маски не вывести. Без него
группировка чисто геометрическая и ошибается в обе стороны: склеивает текст
соседних панелей, если те оказались друг под другом, и рвёт балун там, где
межстрочный интервал шире обычного. Зато blk обучен на пузырях манги и на
вебтуне молчит, поэтому он именно подсказка: строки, не попавшие ни в один
бокс, группируются геометрически, как раньше.

det не используется: постобработка DBNet (pyclipper, shapely) нужна ради
того, что горизонтальная проекция уже чистой маски даёт точнее и дешевле.
"""
import os
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .schema import Region
from .detect import _group_lines

DETECTOR_NAME = "comic-text-detector-onnx-v2"

MODEL_PATH = os.path.join(os.environ.get("MODELS_DIR", "/models"), "comictextdetector.onnx")

INPUT_SIZE = 1024       # вход модели, фиксированный
MASK_THRESH = 0.3       # порог сигмоиды на маске глифов
BLK_CONF = 0.4          # порог уверенности боксов
BLK_NMS = 0.3
TILE_ASPECT = 1.0       # тайл делаем квадратным: модель обучена на страницах
TILE_STEP = 0.8         # шаг тайла, перекрытие 20% — строка не режется пополам

MIN_LINE_H = 6
MIN_LINE_W = 10
KERNEL_FACTOR = 0.9     # ширина ядра склейки строки в долях высоты глифа
DILATE_FACTOR = 0.35    # запас маски стирания в долях высоты глифа
MAX_POLY_PTS = 200      # длиннее полигон Photoshop выделяет заметно медленнее
ASSIGN_COVER = 0.6      # какая доля строки должна лежать в боксе, чтобы считать её его
COL_GAP = 0.45          # ширина пустого коридора между колонками в долях высоты глифа
MIN_ORPHAN_PX = 120     # минимум чернил для региона, не подтверждённого боксом
FLAT_BG_STD = 18.0      # разброс фона под текстом: ниже — ровная подложка
PAD = 6                 # запас маски вокруг bbox: заливать надо шире глифов

Box = Tuple[int, int, int, int]

_session = None


def available() -> bool:
    return os.path.exists(MODEL_PATH)


def _sess():
    global _session
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    return _session


def _infer_tile(tile_bgr: np.ndarray) -> Tuple[np.ndarray, List[List[float]]]:
    """Маска глифов и боксы блоков для одного тайла, в координатах тайла."""
    h, w = tile_bgr.shape[:2]
    r = min(INPUT_SIZE / h, INPUT_SIZE / w)
    nw, nh = int(round(w * r)), int(round(h * r))

    t = cv2.resize(tile_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    # Паддинг только справа и снизу — так letterbox устроен в оригинале,
    # иначе координаты уедут на половину поля.
    t = cv2.copyMakeBorder(t, 0, INPUT_SIZE - nh, 0, INPUT_SIZE - nw,
                           cv2.BORDER_CONSTANT, value=(0, 0, 0))

    x = cv2.cvtColor(t, cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    seg, blk = _sess().run(["seg", "blk"], {"images": x})

    m = (seg[0, 0][:nh, :nw] > MASK_THRESH).astype(np.uint8) * 255
    m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)

    rows = blk[0]
    rows = rows[rows[:, 4] > BLK_CONF]
    boxes = []
    for row in rows:
        cx, cy, bw, bh = row[:4] / r
        boxes.append([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2, float(row[4])])
    return m, boxes


def _nms(boxes: Sequence[Sequence[float]]) -> List[Box]:
    if not boxes:
        return []
    a = np.asarray(boxes, dtype=np.float32)
    rects = [[float(p[0]), float(p[1]), float(p[2] - p[0]), float(p[3] - p[1])] for p in a]
    idx = cv2.dnn.NMSBoxes(rects, a[:, 4].tolist(), BLK_CONF, BLK_NMS)
    if idx is None or len(idx) == 0:
        return []
    keep = np.asarray(idx).reshape(-1)
    return [(int(a[i][0]), int(a[i][1]), int(a[i][2]), int(a[i][3])) for i in keep]


def analyze_page(img_bgr: np.ndarray) -> Tuple[np.ndarray, List[Box]]:
    """Маска глифов и боксы блоков для всей страницы.

    Вебтун-лента в квадрат модели не влезает: 720x3700 сжимается до 199 px
    по ширине, и текст пропадает физически. Поэтому длинное режется на
    квадратные тайлы с перекрытием, маски складываются по максимуму,
    а боксы собираются со всех тайлов и прореживаются общим NMS.
    """
    H, W = img_bgr.shape[:2]
    if H <= W * (TILE_ASPECT + 0.3):
        mask, boxes = _infer_tile(img_bgr)
        return mask, _nms(boxes)

    tile_h = int(W * TILE_ASPECT)
    step = max(1, int(tile_h * TILE_STEP))
    mask = np.zeros((H, W), np.uint8)
    boxes: List[List[float]] = []
    y = 0
    while True:
        y2 = min(H, y + tile_h)
        m, b = _infer_tile(img_bgr[y:y2])
        mask[y:y2] = np.maximum(mask[y:y2], m)
        for p in b:
            boxes.append([p[0], p[1] + y, p[2], p[3] + y, p[4]])
        if y2 >= H:
            break
        y += step
    return mask, _nms(boxes)


# Совместимость: раньше наружу торчала только маска.
def text_mask(img_bgr: np.ndarray) -> np.ndarray:
    return analyze_page(img_bgr)[0]


def _glyph_scale(mask: np.ndarray) -> int:
    """Медианная высота глифа — от неё считаются все морфологические размеры."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    hs = [stats[i, cv2.CC_STAT_HEIGHT] for i in range(1, n)
          if stats[i, cv2.CC_STAT_AREA] >= 8]
    return int(np.median(hs)) if hs else 12


def _line_boxes(mask: np.ndarray, scale: int) -> List[Box]:
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


def _cover(line: Box, blk: Box) -> float:
    """Какая доля строки лежит внутри бокса."""
    x, y, w, h = line
    ix = min(x + w, blk[2]) - max(x, blk[0])
    iy = min(y + h, blk[3]) - max(y, blk[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    return (ix * iy) / float(w * h)


def _assign(lines: List[Box], blks: List[Box]) -> Tuple[List[List[Box]], List[Box]]:
    """Раскладывает строки по балунам. Не попавшие никуда — отдельно."""
    buckets: Dict[int, List[Box]] = {}
    orphans: List[Box] = []
    for lb in lines:
        best, best_cov = -1, 0.0
        for i, bb in enumerate(blks):
            cov = _cover(lb, bb)
            if cov > best_cov:
                best, best_cov = i, cov
        if best_cov >= ASSIGN_COVER:
            buckets.setdefault(best, []).append(lb)
        else:
            orphans.append(lb)
    return list(buckets.values()), orphans


def _split_columns(group: List[Box], scale: int) -> List[List[Box]]:
    """Режет блок на колонки по пустому вертикальному коридору.

    Два перекрывающихся балуна blk отдаёт одним боксом, и OCR потом читает
    строки поперёк обоих — реплики перемешиваются посимвольно и перевести
    их уже нельзя. Геометрическая группировка так ошибиться не может: она
    требует перекрытия по X, поэтому проверка нужна только для блоков.

    Текст с выключкой по центру всегда сливается в один интервал по X,
    так что пустой коридор внутри колонки не возникает.
    """
    if len(group) < 4:
        return [group]

    x0 = min(p[0] for p in group)
    x1 = max(p[0] + p[2] for p in group)
    cov = np.zeros(x1 - x0, np.int32)
    for x, _, w, _ in group:
        cov[x - x0:x - x0 + w] += 1

    min_gap = max(4, int(scale * COL_GAP))
    cuts: List[int] = []
    run = 0
    for i, c in enumerate(cov):
        if c == 0:
            run += 1
            continue
        if run >= min_gap:
            cuts.append(x0 + i - run // 2)
        run = 0
    if not cuts:
        return [group]

    bounds = [x0] + cuts + [x1]
    cols: List[List[Box]] = []
    for a, b in zip(bounds, bounds[1:]):
        cols.append([p for p in group if a <= p[0] + p[2] // 2 < b])
    # Колонка из одной строки — скорее всего не колонка, а выехавший
    # хвост реплики; тогда честнее оставить блок целым.
    if any(len(c) < 2 for c in cols):
        return [group]
    return cols


def _bg_stats(gray: np.ndarray, mask: np.ndarray, x: int, y: int, x2: int, y2: int,
              scale: int) -> Tuple[int, float]:
    """Яркость и разброс подложки: пиксели рамки за вычетом раздутых глифов.

    Старый вариант мерил кольцо вокруг блока и на манге врал: кольцо
    садится на соседний рисунок или на обводку балуна, и ровный белый
    пузырь приезжает как текст поверх арта.
    """
    crop = gray[y:y2, x:x2]
    mcrop = mask[y:y2, x:x2]
    if crop.size == 0:
        return 255, 0.0
    d = max(3, int(scale * DILATE_FACTOR) | 1)
    grown = cv2.dilate(mcrop, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d)))
    bg = crop[grown == 0]
    if bg.size < 20:
        bg = crop.reshape(-1)
    return int(np.median(bg)), float(np.std(bg))


def _mask_poly(mask: np.ndarray, x: int, y: int, x2: int, y2: int,
               scale: int) -> Optional[List[List[int]]]:
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


def _region(idx: int, group: List[Box], img_bgr: np.ndarray, gray: np.ndarray,
            mask: np.ndarray, scale: int, confirmed: bool) -> Optional[Region]:
    H, W = gray.shape
    x = min(p[0] for p in group)
    y = min(p[1] for p in group)
    x2 = max(p[0] + p[2] for p in group)
    y2 = max(p[1] + p[3] for p in group)
    w, h = x2 - x, y2 - y

    ink = mask[y:y2, x:x2]
    ink_px = int((ink > 0).sum()) if ink.size else 0
    # Балун подтверждён моделью — верим ему. Всё остальное должно доказать,
    # что это текст, а не пара точек растра: иначе Content-Aware Fill
    # пойдёт затирать рисунок.
    if not confirmed and ink_px < MIN_ORPHAN_PX:
        return None

    heights = [p[3] for p in group]
    font_px = int(np.median(heights))
    if len(group) > 1:
        tops = sorted(p[1] for p in group)
        gaps = [tops[j + 1] - tops[j] for j in range(len(tops) - 1)]
        line_h = int(np.median(gaps)) if gaps else int(font_px * 1.2)
    else:
        line_h = int(font_px * 1.2)

    local = max(font_px, scale)
    bg_val, bg_std = _bg_stats(gray, mask, x, y, x2, y2, local)
    on_art = bg_std > FLAT_BG_STD

    if on_art:
        kind = "sfx"
    elif confirmed:
        kind = "bubble"
    elif h > 0 and w / float(h) > 3.0 and bg_val > 200:
        kind = "caption"
    else:
        kind = "bubble"

    # Цвет букв берём с самих букв, а не угадываем по яркости фона.
    sel = mask[y:y2, x:x2] > 0
    if sel.any():
        px = img_bgr[y:y2, x:x2][sel]
        fg = [int(v) for v in np.median(px, axis=0)][::-1]
    else:
        fg = [0, 0, 0] if bg_val > 127 else [255, 255, 255]

    poly = _mask_poly(mask, x, y, x2, y2, local)
    if poly is None:
        mx, my = max(0, x - PAD), max(0, y - PAD)
        mx2, my2 = min(W, x2 + PAD), min(H, y2 + PAD)
        poly = [[mx, my], [mx2, my], [mx2, my2], [mx, my2]]

    return Region(
        id="r%03d" % idx,
        bbox=[x, y, w, h],
        mask_poly=poly,
        angle=0.0,
        lines=len(group),
        font_px=font_px,
        line_h_px=line_h,
        kind=kind,
        on_art=on_art,
        fg=fg,
        bg=[bg_val] * 3,
    )


def detect(img_bgr: np.ndarray) -> List[Region]:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    mask, blks = analyze_page(img_bgr)
    scale = _glyph_scale(mask)
    lines = _line_boxes(mask, scale)

    in_blocks, orphans = _assign(lines, blks)
    cols = [c for g in in_blocks for c in _split_columns(g, scale)]
    groups = [(g, True) for g in cols] + [(g, False) for g in _group_lines(orphans)]
    groups.sort(key=lambda gc: min(p[1] for p in gc[0]))

    regions: List[Region] = []
    for g, confirmed in groups:
        r = _region(len(regions) + 1, g, img_bgr, gray, mask, scale, confirmed)
        if r is not None:
            regions.append(r)
    return regions

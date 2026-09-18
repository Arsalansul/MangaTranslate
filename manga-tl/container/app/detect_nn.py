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
NARROW_LINE_H = 0.7     # доля кегля, при которой узкий столбик всё-таки буква
KERNEL_FACTOR = 0.9     # ширина ядра склейки строки в долях высоты глифа
DILATE_FACTOR = 0.35    # запас маски стирания в долях высоты глифа
MAX_POLY_PTS = 200      # длиннее полигон Photoshop выделяет заметно медленнее
ASSIGN_COVER = 0.6      # какая доля строки должна лежать в боксе, чтобы считать её его
COL_GAP = 0.45          # ширина пустого коридора между колонками в долях высоты глифа
STITCH_GAP = 0.6        # разрыв, который сшивается в строку, в долях её высоты
STITCH_ALIGN = 0.6      # какая доля высоты обязана совпасть, чтобы счесть строки одной
MIN_ORPHAN_PX = 120     # минимум чернил для региона, не подтверждённого боксом
GROUP_NEST = 0.5        # доля меньшей группы внутри большей, при которой это один балун
FLAT_BG_STD = 18.0      # разброс фона под текстом: ниже — ровная подложка
FLAT_BG_TOL = 12        # допуск яркости, в пределах которого пиксель — тот же фон
FLAT_BG_SHARE = 0.85    # доля подложки этого цвета: тоже признак ровной
SFX_FONT_RATIO = 1.6    # кегль крупнее страничного во столько раз — рисованный звук
SFX_MIN_LINES = 3       # и набран он в одну-две строки, а не абзацем
RAGGED_MIN_LINES = 3    # по двум строкам о выключке судить нельзя
RAGGED_LEFT = 0.3       # разброс левых краёв, при котором край ровный
RAGGED_TIMES = 3        # во столько раз центры гуляют сильнее краёв
PAD = 6                 # запас маски вокруг bbox: заливать надо шире глифов
LINE_TOL = 0.15         # допуск вокруг бокса строки в долях её высоты
LINE_MIN_RATIO = 0.4    # строка ниже этой доли медианы — не строка набора
LINE_MAX_RATIO = 2.5    # а выше — нарисованный звук, наехавший на рамку
SAFE_TOL = 55           # допуск яркости, в пределах которого пиксель считается подложкой
SAFE_STEP = 4           # шаг, которым рамка растёт в стороны
SAFE_CLEAR = 0.97       # какая доля прирастающей полосы обязана быть подложкой
SAFE_MARGIN = 0.02      # поля страницы, в которые вёрстка не заходит
SAFE_PAD = 0.2          # отступ от препятствия, в долях высоты строки
SAFE_GROW = 2.0         # шире этого рамку текста не раздуваем
SAFE_LINES = 2.5        # и не выше, чем на столько лишних строк

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
        if bh < MIN_LINE_H:
            continue
        # Узкая колонка чернил — либо крапина, либо строка из одной буквы:
        # «I», «А», «?». Отличаем по росту: буква набора ровно такая же
        # высокая, как соседние строки, а крапина втрое ниже. Потерянная
        # строка дорого стоит: её не переводят и не стирают, и в пузыре
        # поверх русского текста остаётся английская буква.
        if bw < MIN_LINE_W and bh < scale * NARROW_LINE_H:
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


def _assign(lines: List[Box],
            blks: List[Box]) -> Tuple[List[Tuple[List[Box], Box]], List[Box]]:
    """Раскладывает строки по балунам. Не попавшие никуда — отдельно.

    Бокс балуна возвращается вместе со строками: он нужен дальше как потолок
    для свободного поля вёрстки.
    """
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
    return [(g, blks[i]) for i, g in buckets.items()], orphans


def _bounds(group: Sequence[Box]) -> Box:
    x = min(b[0] for b in group)
    y = min(b[1] for b in group)
    x2 = max(b[0] + b[2] for b in group)
    y2 = max(b[1] + b[3] for b in group)
    return (x, y, x2 - x, y2 - y)


def _nest(a: Box, b: Box) -> float:
    """Какая доля рамки a лежит внутри рамки b; обе в виде x, y, w, h."""
    ix = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    iy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    return (ix * iy) / float(a[2] * a[3])


def _merge_nested(groups: List[Tuple[List[Box], Optional[Box]]]
                  ) -> List[Tuple[List[Box], Optional[Box]]]:
    """Сливает группы, сидящие одна в другой: это один балун, разбитый боксами.

    Детектор иногда отдаёт на пузырь два бокса — общий и ещё один на строку
    внутри. Строка уходит тому, чей бокс попался первым, остальные — другому,
    и на один пузырь выходит два региона. Само по себе это полбеды, но OCR
    читает регион по рамке, а рамка большего накрывает и чужую строку: её
    переводят дважды и дважды кладут поверх пузыря.
    """
    out = list(groups)
    merged = True
    while merged:
        merged = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = _bounds(out[i][0]), _bounds(out[j][0])
                if max(_nest(a, b), _nest(b, a)) < GROUP_NEST:
                    continue
                big = out[i] if a[2] * a[3] >= b[2] * b[3] else out[j]
                out[i] = (out[i][0] + out[j][0], big[1])
                del out[j]
                merged = True
                break
            if merged:
                break
    return out


def _stitch(boxes: List[Box]) -> List[Box]:
    """Сшивает обрывки одной строки, разъехавшиеся по межсловному пробелу.

    Глифы смыкаются в строку морфологией с ядром в долях медианной высоты
    глифа по всей странице. На манге страница набрана одним кеглем, и мерка
    годится; на ленте вебтуна в шесть тысяч пикселей кегли разные, медиана
    уезжает к мелким, и у крупного капшена ядро оказывается уже пробела.
    «МЕНЯ ЗОВУТ ЭНКРИД» распадалось на «МЕНЯ» и «ЗОВУТ ЭНКРИД» — два региона,
    каждый со своим переводом, и собрать из них фразу уже нельзя.

    Мерка здесь местная: разрыв сравнивается с высотой самих обрывков, а не
    страницы. Сшиваются только строки, не попавшие ни в один бокс модели:
    внутри бокса строки собираются по нему, а лишняя склейка через колонку
    помешала бы развести слипшиеся балуны.
    """
    out = sorted(boxes, key=lambda b: b[0])
    joined = True
    while joined:
        joined = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                h = min(a[3], b[3])
                if not h or float(h) / max(a[3], b[3]) < STITCH_ALIGN:
                    continue
                over = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                gap = max(a[0], b[0]) - min(a[0] + a[2], b[0] + b[2])
                if over < h * STITCH_ALIGN or gap > h * STITCH_GAP:
                    continue
                x, y = min(a[0], b[0]), min(a[1], b[1])
                out[i] = (x, y, max(a[0] + a[2], b[0] + b[2]) - x,
                          max(a[1] + a[3], b[1] + b[3]) - y)
                del out[j]
                joined = True
                break
            if joined:
                break
    return out


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
              scale: int) -> Tuple[int, float, float]:
    """Яркость, разброс и однородность подложки: рамка за вычетом раздутых глифов.

    Старый вариант мерил кольцо вокруг блока и на манге врал: кольцо
    садится на соседний рисунок или на обводку балуна, и ровный белый
    пузырь приезжает как текст поверх арта.

    Разброса одного мало и здесь: обводка балуна, линейка под строкой,
    угол соседнего кадра — любая чёрная деталь в рамке раздувает std,
    хотя подложка под текстом белая. Поэтому считаем ещё долю пикселей
    одного цвета: меньшинству чернил её не испортить.
    """
    crop = gray[y:y2, x:x2]
    mcrop = mask[y:y2, x:x2]
    if crop.size == 0:
        return 255, 0.0, 1.0
    d = max(3, int(scale * DILATE_FACTOR) | 1)
    grown = cv2.dilate(mcrop, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d)))
    bg = crop[grown == 0]
    if bg.size < 20:
        bg = crop.reshape(-1)
    med = int(np.median(bg))
    share = float((np.abs(bg.astype(np.int16) - med) <= FLAT_BG_TOL).mean())
    return med, float(np.std(bg)), share


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


def _mask_polys(mask: np.ndarray, group: List[Box], x: int, y: int, x2: int, y2: int,
                scale: int) -> List[List[List[int]]]:
    """Контуры стирания по каждому куску текста отдельно.

    _mask_poly отдаёт один контур и сдаётся, когда текст распался на
    несколько, — а распадается он почти всегда, стоит строкам разойтись
    шире дилатации. Пока стирал Photoshop, ценой был прямоугольник вместо
    силуэта; теперь стирает модель, и прямоугольник означает съеденный
    рисунок вокруг букв.

    Заодно отсюда выбрасываются чужие глифы. Рамка региона — это габарит
    его строк, и в неё попадает всё, что рядом нарисовано: угол балуна,
    штриховка, нарисованный звук из той же панели. Стирать это нельзя, а
    отличить просто — по строкам: чернила региона лежат в его строках, а
    не между ними.

    Сами строки тоже проверяются. Группировка изредка пришивает к реплике
    обломок нарисованного звука, наехавшего на её рамку, — и тогда звук
    приезжает в регион на правах строки. Выдаёт его рост: строки одного
    набора одной высоты, а звук рисуется в разы крупнее.
    """
    H, W = mask.shape
    mx, my = max(0, x - PAD), max(0, y - PAD)
    mx2, my2 = min(W, x2 + PAD), min(H, y2 + PAD)
    crop = mask[my:my2, mx:mx2]
    if crop.size == 0:
        return []

    binary = (crop > 0).astype(np.uint8)
    _, labels = cv2.connectedComponents(binary, connectivity=8)

    # Строки региона с небольшим допуском: бокс строки обводит тело глифов,
    # а хвосты запятых и точки над i выступают за него.
    rows = np.zeros_like(binary)
    med = float(np.median([b[3] for b in group]))
    own_lines = [b for b in group
                 if LINE_MIN_RATIO * med <= b[3] <= LINE_MAX_RATIO * med] or list(group)
    for bx, by, bw, bh in own_lines:
        tol = max(2, int(bh * LINE_TOL))
        cv2.rectangle(rows,
                      (bx - mx - tol, by - my - tol),
                      (bx - mx + bw + tol, by - my + bh + tol), 1, -1)

    own = set(np.unique(labels[(rows > 0) & (binary > 0)])) - {0}
    keep = np.isin(labels, list(own)).astype(np.uint8) if own else binary

    d = max(3, int(scale * DILATE_FACTOR) | 1)
    grown = cv2.dilate(keep * 255, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d)))
    contours, _ = cv2.findContours(grown, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    polys = []
    for c in contours:
        approx = cv2.approxPolyDP(c, 2.0, True)
        if len(approx) < 3:
            continue
        polys.append([[int(p[0][0]) + mx, int(p[0][1]) + my] for p in approx])
    return polys


def _safe_box(gray: np.ndarray, mask: np.ndarray, x: int, y: int, x2: int, y2: int,
              bg_val: int, line_h: int) -> Optional[List[int]]:
    """Свободное поле вокруг текста: куда можно верстать перевод.

    Рамка исходного текста снята впритык, а перевод длиннее оригинала —
    в неё он влезает только нечитаемым кеглем. Настоящий предел вёрстки не
    бывший текст, а первое препятствие вокруг: стенка пузыря, край панели,
    соседний рисунок.

    Поэтому рамка не вычисляется, а выращивается: по очереди с каждой стороны,
    пока прирастающая полоса почти целиком лежит на подложке. Буквы её дырявят,
    из-за чего рост остановился бы на первой же строке, поэтому маска глифов
    заранее объявляется подложкой.

    Рост честен и там, где границы нет вовсе. Прежний вариант искал связную
    область фона и вписывался в её габарит; на вебтуне белый пузырь сливается
    с белым полем страницы, а капшен и вовсе стоит на голом фоне — область
    разливалась на всю ленту, и проверка отбрасывала её целиком, хотя места
    там как раз вдоволь. Рост же упирается в обводку пузыря там, где она есть,
    и в поля страницы там, где её нет.
    """
    H, W = gray.shape
    glyph = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2) > 0
    flat = ((np.abs(gray.astype(np.int16) - int(bg_val)) <= SAFE_TOL) | glyph)

    # Потолок роста — только поля страницы. Бокс блока от модели, без которого
    # заливка не обходилась, здесь мешает: он снят по тексту, а не по пузырю,
    # и у половины реплик запирал рост в ноль. Препятствия рост видит сам.
    m = int(round(W * SAFE_MARGIN))
    lo_x, lo_y, hi_x, hi_y = m, m, W - m, H - m

    # Поле нужно не «побольше», а ровно настолько, насколько перевод длиннее:
    # по ширине — чтобы слово не рвалось переносом, по высоте — на пару лишних
    # строк. Всё сверх этого уже не запас, а лишний повод уехать не туда.
    w, h = x2 - x, y2 - y
    max_w, max_h = w * SAFE_GROW, h + SAFE_LINES * max(line_h, 1)

    box = [x, y, x2, y2]
    room = [True, True, True, True]
    while any(room):
        for side in range(4):
            if not room[side]:
                continue
            a, b, c, d = box
            if side == 0:
                n = max(lo_x, a - SAFE_STEP)
                ok = n < a and c - n <= max_w and flat[b:d, n:a].mean() >= SAFE_CLEAR
            elif side == 1:
                n = max(lo_y, b - SAFE_STEP)
                ok = n < b and d - n <= max_h and flat[n:b, a:c].mean() >= SAFE_CLEAR
            elif side == 2:
                n = min(hi_x, c + SAFE_STEP)
                ok = n > c and n - a <= max_w and flat[b:d, c:n].mean() >= SAFE_CLEAR
            else:
                n = min(hi_y, d + SAFE_STEP)
                ok = n > d and n - b <= max_h and flat[d:n, a:c].mean() >= SAFE_CLEAR
            if ok:
                box[side] = n
            room[side] = ok

    # Рост останавливается вплотную к препятствию, а текст, прижатый к обводке,
    # читается как ошибка вёрстки. Отступ отсчитывается от строки, но рамку
    # оригинала не режет: хуже, чем было, быть не должно.
    pad = max(2, int(round(max(line_h, 1) * SAFE_PAD)))
    ax, ay = min(x, box[0] + pad), min(y, box[1] + pad)
    ax2, ay2 = max(x2, box[2] - pad), max(y2, box[3] - pad)
    if ax2 - ax <= w and ay2 - ay <= h:
        return None
    return [ax, ay, ax2 - ax, ay2 - ay]


def _deoverlap(regions: Sequence[Region]) -> None:
    """Разводит пересекающиеся свободные поля соседних балунов.

    Два пузыря, нарисованные внахлёст, для заливки — одна белая область, и
    поле вёрстки у них выходит общим: реплики лягут друг на друга.

    Ось реза выбирается не по форме пересечения, а по тому, где разъехались
    сами тексты: у соседних балунов это почти всегда одна ось, и резать по
    другой значит не разделить их вовсе. Если тексты перекрываются по обеим,
    регионы оставляются как есть — ужимать до нечитаемого кегля хуже, чем
    оставить касание.
    """
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            a, b = regions[i], regions[j]
            if not a.safe_box or not b.safe_box:
                continue
            # Звук не верстается, и ужимать ради него соседа не за что.
            if "sfx" in (a.kind, b.kind):
                continue
            ax, ay, aw, ah = a.safe_box
            bx, by, bw, bh = b.safe_box
            if min(ax + aw, bx + bw) <= max(ax, bx):
                continue
            if min(ay + ah, by + bh) <= max(ay, by):
                continue

            gaps = []
            for ax_i in (0, 1):
                p, q = a.bbox[ax_i], b.bbox[ax_i]
                pe, qe = p + a.bbox[ax_i + 2], q + b.bbox[ax_i + 2]
                gaps.append(max(p, q) - min(pe, qe))
            axis = 0 if gaps[0] >= gaps[1] else 1
            if gaps[axis] <= 0:
                continue

            lo, hi = (a, b) if a.bbox[axis] < b.bbox[axis] else (b, a)
            cut = (lo.bbox[axis] + lo.bbox[axis + 2] + hi.bbox[axis]) // 2
            lo.safe_box[axis + 2] = cut - lo.safe_box[axis]
            hi.safe_box[axis + 2] += hi.safe_box[axis] - cut
            hi.safe_box[axis] = cut


def _ragged(group: List[Box], font_px: int) -> bool:
    """Строки выключены влево и обрываются по смыслу, а не по ширине.

    Так набраны содержание, титры, список — там перенос не технический,
    и перевод, слитый в абзац, ляжет мимо линеек и мимо колонки номеров.
    Отличить от реплики можно по геометрии: у реплики строки центрованы,
    поэтому гуляют левые края; у списка ровно наоборот.
    """
    if len(group) < RAGGED_MIN_LINES or font_px <= 0:
        return False
    lefts = np.array([p[0] for p in group], np.float32)
    centers = np.array([p[0] + p[2] * 0.5 for p in group], np.float32)
    sl = float(lefts.std())
    return sl < RAGGED_LEFT * font_px and float(centers.std()) > RAGGED_TIMES * sl


def _region(idx: int, group: List[Box], img_bgr: np.ndarray, gray: np.ndarray,
            mask: np.ndarray, scale: int, blk: Optional[Box]) -> Optional[Region]:
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
    confirmed = blk is not None
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
    bg_val, bg_std, bg_share = _bg_stats(gray, mask, x, y, x2, y2, local)
    # Два признака ровной подложки, и хватает любого: разброс ловит серую
    # растяжку, доля — белый пузырь, которому обводка испортила разброс.
    on_art = bg_std > FLAT_BG_STD and bg_share < FLAT_BG_SHARE

    # Звук — это рисунок, и нарисован он крупно и коротко. Раньше звуком
    # считалось всё, что легло на арт, и под нож шли подтверждённые балуны
    # с девятью строками реплики и страница содержания: их не переводило
    # вовсе. Спрашиваем не про фон, а про сам набор.
    drawn = font_px > scale * SFX_FONT_RATIO or len(group) < SFX_MIN_LINES
    if on_art and drawn and not confirmed:
        kind = "sfx"
    elif confirmed:
        kind = "bubble"
    elif h > 0 and w / float(h) > 3.0 and bg_val > 200:
        kind = "caption"
    else:
        kind = "bubble"

    # Цвет букв берём с самих букв, а не угадываем по яркости фона. Но брать
    # медиану по всей маске нельзя: у мелкого шрифта сглаженных краёв больше,
    # чем тела глифа, и медиана уезжает в серый — чёрная реплика верстается
    # блёклой. Считаем по ядру: четверти пикселей, дальше всего отстоящей от
    # фона по яркости. Так же работает и для белого текста на чёрном.
    sel = mask[y:y2, x:x2] > 0
    if sel.any():
        px = img_bgr[y:y2, x:x2][sel]
        lum = px.astype(np.float32) @ np.array([0.114, 0.587, 0.299], np.float32)
        far = np.abs(lum - float(bg_val))
        core = px[far >= np.percentile(far, 75)]
        if not len(core):
            core = px
        fg = [int(v) for v in np.median(core, axis=0)][::-1]
    else:
        fg = [0, 0, 0] if bg_val > 127 else [255, 255, 255]

    poly = _mask_poly(mask, x, y, x2, y2, local)
    if poly is None:
        mx, my = max(0, x - PAD), max(0, y - PAD)
        mx2, my2 = min(W, x2 + PAD), min(H, y2 + PAD)
        poly = [[mx, my], [mx2, my], [mx2, my2], [mx, my2]]
    polys = _mask_polys(mask, group, x, y, x2, y2, local)
    keep_lines = _ragged(group, font_px)

    # Рамку считаем всем, без оглядки на вид региона. Пустая рамка означает
    # «весь bbox», а в него длинная русская реплика не влезает — так текст и
    # вылезал из балуна. Считать только не-звукам нельзя: вид уточняется уже
    # после OCR (kinds.revise_kinds), и возвращённая в перевод подпись
    # осталась бы без рамки. На рисунке рост всё равно упрётся сразу.
    safe = _safe_box(gray, mask, x, y, x2, y2, bg_val, line_h)

    return Region(
        id="r%03d" % idx,
        bbox=[x, y, w, h],
        safe_box=safe or [],
        mask_poly=poly,
        mask_polys=polys,
        angle=0.0,
        lines=len(group),
        keep_lines=keep_lines,
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
    groups = [(c, blk) for g, blk in in_blocks for c in _split_columns(g, scale)]
    groups += [(g, None) for g in _group_lines(_stitch(orphans))]
    groups = _merge_nested(groups)
    groups.sort(key=lambda gb: min(p[1] for p in gb[0]))

    regions: List[Region] = []
    for g, blk in groups:
        r = _region(len(regions) + 1, g, img_bgr, gray, mask, scale, blk)
        if r is not None:
            regions.append(r)
    _deoverlap(regions)
    return regions

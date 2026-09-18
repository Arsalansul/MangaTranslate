"""Стирание оригинального текста внутри контейнера.

Раньше стирал Photoshop: ровный фон — заливкой цвета подложки, текст
поверх рисунка — Content-Aware Fill. Заливка работает до сих пор и лучше
любой модели: если фон однотонный, его цвет известен точно, и угадывать
нечего. А вот Content-Aware Fill собирает заплатку из кусков той же
страницы, где полно другого текста, и приносит в дыру чужие буквы.

Поэтому здесь остаётся ровно одна замена: поверх рисунка дорисовывает
LaMa (big-lama, экспорт OpenCV, 512x512). Модель видит только вырезку
вокруг региона, а не всю страницу, — так текст стирается в исходном
разрешении, а не в уменьшенном до 512 развороте.

Стирается только то, для чего есть перевод. Регион без перевода не
трогается вовсе: так звуки остаются нарисованными, как и задумано.
"""
import os
from typing import Dict, List, Sequence

import cv2
import numpy as np

INPAINT_NAME = "lama-onnx-512"

MODEL_PATH = os.path.join(os.environ.get("MODELS_DIR", "/models"), "lama.onnx")

SIZE = 512          # вход модели, фиксированный
MARGIN = 0.5        # контекст вокруг региона в долях его большей стороны
MIN_CROP = 96       # меньше вырезать нет смысла: модели нужен контекст
MAX_CROP = SIZE     # вырезку крупнее пришлось бы ужимать, а это мыло по рисунку
FEATHER = 1.5       # размытие края заплатки, px
GROW_RATIO = 0.2    # расширение контура буквы в долях кегля
GROW_MIN = 3        # ореол и перо заплатки должны уместиться внутри дыры
GROW_MAX = 4        # больше — начинает съедать рисунок вплотную к реплике
TONE_SCALE = 81     # окно, которым меряется тон окрестности, px: заметно шире дыры
TONE_MAX_SHIFT = 90  # предел правки уровня
TONE_MIN_SEEN = 0.15  # доля видимого фона в окне, ниже которой правке нет опоры
FLAT_RING = 3         # кольцо вокруг дыры, по которому судят о фоне, px
FLAT_RING_STD = 8.0   # разброс в кольце, ниже которого фон считают ровным

_session = None


def available() -> bool:
    return os.path.isfile(MODEL_PATH)


def _sess():
    global _session
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    return _session


def _io_names():
    """Какой вход картинка, какой маска — по числу каналов, а не по имени."""
    img_name = mask_name = None
    for i in _sess().get_inputs():
        if i.shape and i.shape[1] == 1:
            mask_name = i.name
        else:
            img_name = i.name
    return img_name, mask_name


def _polys(r: Dict) -> List[np.ndarray]:
    """Контуры стирания региона: маска по кускам текста, иначе одна, иначе рамка.

    Порядок именно такой. mask_polys обходит соседний рисунок, mask_poly
    вырождается в прямоугольник на каждом втором регионе, а рамка — то, что
    остаётся, когда маски нет вовсе (правленый вручную analysis.json).
    """
    parts = r.get("mask_polys") or []
    out = [np.array(p, dtype=np.int32) for p in parts if len(p) >= 3]
    if out:
        return out

    pts = r.get("mask_poly") or []
    if len(pts) >= 3:
        return [np.array(pts, dtype=np.int32)]

    x, y, w, h = r["bbox"]
    return [np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)]


def _paint(mask: np.ndarray, r: Dict, off: Sequence[int] = (0, 0)) -> None:
    """Кладёт контуры региона в маску, расширяя их на сглаженный край буквы.

    Контур обводит букву по порогу, а у печатного текста за порогом остаётся
    серый ореол в пиксель-другой. Заливке он даёт грязную кайму, а модель
    принимает его за рисунок и честно дорисовывает по нему призрак стёртой
    строки — ровно то, что видно на странице как «стёрли, но видно».
    Расширяем от кегля: у крупного текста и ореол шире.
    """
    ox, oy = off
    tmp = np.zeros(mask.shape, np.uint8)
    cv2.fillPoly(tmp, [p - [ox, oy] for p in _polys(r)], 255)
    g = int(min(GROW_MAX, max(GROW_MIN, round((r.get("font_px") or 0) * GROW_RATIO))))
    tmp = cv2.dilate(tmp, np.ones((2 * g + 1, 2 * g + 1), np.uint8))
    mask[tmp > 0] = 255


def _crop_box(r: Dict, w_img: int, h_img: int) -> List[int]:
    x, y, w, h = r["bbox"]
    m = int(max(w, h, MIN_CROP) * MARGIN)
    return _square([max(0, x - m), max(0, y - m),
                    min(w_img, x + w + m), min(h_img, y + h + m)], w_img, h_img)


def _square(box: Sequence[int], w_img: int, h_img: int) -> List[int]:
    """Квадратная вырезка: вход модели квадратный, прямоугольник она растянет.

    Растянутая вырезка — растянутые штрихи, и дорисовывает модель их тоже
    под наклоном, которого на странице нет.
    """
    x0, y0, x1, y1 = box
    side = min(max(x1 - x0, y1 - y0), w_img, h_img)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    x0 = max(0, min(cx - side // 2, w_img - side))
    y0 = max(0, min(cy - side // 2, h_img - side))
    return [x0, y0, x0 + side, y0 + side]


def _clusters(regions: Sequence[Dict], w_img: int, h_img: int) -> List[Dict]:
    """Соседние регионы стираются одним проходом модели.

    Вырезки реплик в одной панели всё равно перекрываются, а каждый проход
    стоит секунды. Объединять их приходится с оглядкой: чем больше вырезка,
    тем сильнее она ужимается до 512 и тем мягче выходит заплатка.
    """
    items = [{"box": _crop_box(r, w_img, h_img), "regions": [r]} for r in regions]
    merged = True
    while merged:
        merged = False
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i]["box"], items[j]["box"]
                if a[0] >= b[2] or b[0] >= a[2] or a[1] >= b[3] or b[1] >= a[3]:
                    continue
                u = _square([min(a[0], b[0]), min(a[1], b[1]),
                             max(a[2], b[2]), max(a[3], b[3])], w_img, h_img)
                if u[2] - u[0] > MAX_CROP:
                    continue
                items[i] = {"box": u, "regions": items[i]["regions"] + items[j]["regions"]}
                del items[j]
                merged = True
                break
            if merged:
                break
    return items


def _infer(crop: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Вырезка с дырой -> вырезка, дорисованная моделью. Размер сохраняется."""
    h, w = crop.shape[:2]
    small = cv2.resize(crop, (SIZE, SIZE), interpolation=cv2.INTER_AREA)
    msmall = cv2.resize(mask, (SIZE, SIZE), interpolation=cv2.INTER_NEAREST)

    # Модель обучена на RGB 0..1; OpenCV держит картинку в BGR.
    blob = small[:, :, ::-1].astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    mblob = (msmall > 0).astype(np.float32)[None, None]

    img_name, mask_name = _io_names()
    out = _sess().run(None, {img_name: blob, mask_name: mblob})[0]

    res = np.clip(out[0].transpose(1, 2, 0), 0, 255).astype(np.uint8)[:, :, ::-1]
    return cv2.resize(res, (w, h), interpolation=cv2.INTER_CUBIC)


def _match_tone(crop: np.ndarray, filled: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Подгоняет уровень заплатки под окрестность дыры.

    На растре — серая плашка, небо, любой скринтон — модель кладёт заплатку
    заметно светлее фона: точки растра она усредняет, и средний уровень при
    этом уходит вверх. Рисунок она угадывает верно, промахивается только
    уровнем, и это поправимо.

    Считаем крупным планом: средний уровень окрестности против среднего
    уровня заплатки, оба в окне шире дыры, — и добавляем разницу. Правка
    получается местной, поэтому граница панели или тёмный предмет рядом с
    репликой её не сбивают: каждый пиксель подтягивается к своему соседству,
    а не к среднему по вырезке.
    """
    hole = (mask > 0).astype(np.float32)
    if not hole.any():
        return filled

    # Оба тона — средние по своей области, а не по всему окну: иначе в
    # сравнение попадает целый фон вокруг дыры, разница размазывается по
    # нему, и правка выходит во столько же раз слабее нужной.
    k = (TONE_SCALE, TONE_SCALE)
    out_w = cv2.blur(1.0 - hole, k)
    in_w = cv2.blur(hole, k)
    around = cv2.blur(crop.astype(np.float32) * (1.0 - hole)[:, :, None], k)
    around /= np.maximum(out_w, 1e-3)[:, :, None]
    patch = cv2.blur(filled.astype(np.float32) * hole[:, :, None], k)
    patch /= np.maximum(in_w, 1e-3)[:, :, None]

    # Где одна из областей в окно почти не попала, сравнивать нечего: там
    # правка гасится. Это и края вырезки, и середина дыры шире окна.
    fix = np.clip(around - patch, -TONE_MAX_SHIFT, TONE_MAX_SHIFT)
    fix *= (np.clip(out_w / TONE_MIN_SEEN, 0.0, 1.0)
            * np.clip(in_w / TONE_MIN_SEEN, 0.0, 1.0))[:, :, None]
    return np.clip(filled.astype(np.float32) + fix, 0, 255).astype(np.uint8)


def _flatten(crop: np.ndarray, filled: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Где вокруг дыры ровный фон, кладёт в неё этот фон, а не работу модели.

    Модель и на чистом листе возвращает не ровно белое: остаётся бледное
    пятно по форме стёртого слова. Угадывать там нечего — точный цвет лучше
    любой заплатки. Смотрим по каждому куску маски отдельно: одно слово
    реплики может лежать на белом, а соседнее — на волосах.
    """
    n, lab = cv2.connectedComponents((mask > 0).astype(np.uint8))
    k = np.ones((2 * FLAT_RING + 1, 2 * FLAT_RING + 1), np.uint8)
    for i in range(1, n):
        comp = (lab == i).astype(np.uint8)
        # Соседняя дыра в кольцо не идёт: в ней ещё не стёртый текст.
        ring = (cv2.dilate(comp, k) > 0) & (mask == 0)
        px = crop[ring].reshape(-1, 3)
        if px.size == 0 or px.std(axis=0).max() > FLAT_RING_STD:
            continue
        filled[comp > 0] = np.median(px, axis=0)
    return filled


def erase(img: np.ndarray, regions: Sequence[Dict], force: bool = False) -> Dict:
    """Стирает текст на копии страницы. Возвращает картинку и что было сделано.

    force стирает всё найденное, даже без перевода: режим --erase-only,
    когда страницу чистят под ручную вёрстку.
    """
    out = img.copy()
    h_img, w_img = out.shape[:2]

    targets = [r for r in regions
               if force or (r.get("translation") or "").strip()]
    flat = [r for r in targets if not r.get("on_art")]
    art = [r for r in targets if r.get("on_art")]

    # Ровный фон: заливка его же цветом. Точнее модели и стоит ничего.
    for r in flat:
        rgb = r.get("bg") or [255, 255, 255]
        m = np.zeros((h_img, w_img), np.uint8)
        _paint(m, r)
        out[m > 0] = (int(rgb[2]), int(rgb[1]), int(rgb[0]))

    passes = 0
    if art and available():
        for item in _clusters(art, w_img, h_img):
            x0, y0, x1, y1 = item["box"]
            crop = out[y0:y1, x0:x1]
            mask = np.zeros(crop.shape[:2], np.uint8)
            for r in item["regions"]:
                _paint(mask, r, (x0, y0))
            if not mask.any():
                continue

            filled = _match_tone(crop, _infer(crop, mask), mask)
            filled = _flatten(crop, filled, mask)
            # Заплатка кладётся только на дыру: остальная страница должна
            # остаться попиксельно прежней, её ещё открывать в Photoshop.
            alpha = cv2.GaussianBlur((mask > 0).astype(np.float32), (0, 0), FEATHER)
            alpha = np.clip(alpha, 0.0, 1.0)[:, :, None]
            out[y0:y1, x0:x1] = (crop * (1 - alpha) + filled * alpha).astype(np.uint8)
            passes += 1
    elif art:
        # Весов нет — регионы поверх рисунка остаются Photoshop'у.
        art = []

    return {
        "image": out,
        "flat": len(flat),
        "art": len(art),
        "passes": passes,
        "left": [r["id"] for r in targets if r.get("on_art") and not available()],
    }

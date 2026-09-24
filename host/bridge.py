"""Мост: CV-контейнер <-> Photoshop.

Работает на системном Python без сторонних пакетов — только stdlib и
PowerShell для COM. Это осознанно: тяжёлые зависимости живут в контейнере,
хост остаётся чистым.

Поток данных:
    страница файлом в контейнер -> PageAnalysis (JSON)
    -> сюда вписываются переводы
    -> jsxgen собирает скрипт
    -> Photoshop стирает и верстает
    -> PSD со слоями + PNG на проверку
"""
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jsxgen
import translate  # таблица целевых языков живёт там, где промпт

CV_URL = os.environ.get("MANGA_TL_CV", "http://127.0.0.1:8765")
SEP = os.sep


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        CV_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def health() -> dict:
    with urllib.request.urlopen(CV_URL + "/health", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def analyze(page_rel: str, lang: str = "eng") -> dict:
    """Страница по пути внутри смонтированного /pages.

    Требует, чтобы каталог с главами был примонтирован к контейнеру. Для
    прогона главы это лишнее условие — см. analyze_file.
    """
    return _post("/analyze_path", {"path": page_rel, "lang": lang})


def analyze_file(img_path: str, lang: str = "eng") -> dict:
    """Страница, отправленная в контейнер файлом.

    Так главу можно взять откуда угодно: контейнеру не нужно видеть её на
    диске, и монтировать под неё том не приходится. Через localhost даже
    двенадцатимегабайтная лента уходит за миллисекунды, так что выигрыш
    от чтения с диска мнимый, а неудобство от монтирования — настоящее.
    """
    req = _upload("/analyze?lang=" + urllib.parse.quote(lang), img_path)
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _upload(path: str, img_path: str, fields: dict = None) -> urllib.request.Request:
    """multipart с картинкой: requests в мост не тащим, хост живёт на stdlib."""
    boundary = "----manga-tl-" + os.urandom(8).hex()
    with open(img_path, "rb") as f:
        blob = f.read()

    parts = []
    for key, value in (fields or {}).items():
        parts.append((
            "--%s\r\n"
            'Content-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
            % (boundary, key, value)
        ).encode("utf-8"))
    parts.append((
        "--%s\r\n"
        'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
        % (boundary, os.path.basename(img_path))
    ).encode("utf-8"))

    body = b"".join(parts) + blob + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return urllib.request.Request(
        CV_URL + path, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
    )


def clean(analysis: dict, src_img: str, out_path: str, force: bool = False) -> dict:
    """Стирает оригинальный текст в контейнере, кладёт страницу в out_path.

    Стирание переехало из Photoshop сюда. Там оно умело две вещи: залить
    ровный фон его цветом и вызвать Content-Aware Fill поверх рисунка.
    Первое осталось, второе заменила модель: Content-Aware Fill собирает
    заплатку из кусков той же страницы, а страница в этот момент ещё полна
    текста, и в дыру приезжали буквы из соседних панелей.
    """
    # Область ручной очистки не содержит перевода, но для /inpaint должна
    # выглядеть целью. Маркер живёт только в копии запроса: в analysis.json
    # поле translation остаётся пустым и текстовый слой не создаётся.
    regions = []
    for region in analysis["regions"]:
        item = dict(region)
        if item.get("erase_only"):
            item["translation"] = "__erase__"
        regions.append(item)
    req = _upload("/inpaint", src_img, {
        "regions": json.dumps(regions, ensure_ascii=False),
        "force": "true" if force else "false",
    })
    with urllib.request.urlopen(req, timeout=1800) as resp:
        png, head = resp.read(), resp.headers

    with open(out_path, "wb") as f:
        f.write(png)

    left = (head.get("X-Left-To-Photoshop") or "").strip()
    return {
        "path": out_path,
        "flat": int(head.get("X-Erased-Flat") or 0),
        "art": int(head.get("X-Erased-Art") or 0),
        "passes": int(head.get("X-Inpaint-Passes") or 0),
        "left": [i for i in left.split(",") if i],
    }


def _fwd(p: str) -> str:
    """Photoshop понимает прямые слэши на любой платформе."""
    return os.path.abspath(p).replace(SEP, "/")


def _liquify_map(region: dict, directory: str, index: int) -> dict:
    """Рисует RGB displacement map: красный канал двигает X, зелёный Y."""
    liquid = region.get("typeset_liquify")
    if not liquid or not liquid.get("strokes"):
        return region
    _, _, rw, rh = region.get("safe_box") or region["bbox"]
    width, height = max(8, min(512, int(rw))), max(8, min(512, int(rh)))
    vx, vy = [0.0] * (width * height), [0.0] * (width * height)
    max_x = max(abs(float(s["dx"]) * rw) for s in liquid["strokes"]) or 1.0
    max_y = max(abs(float(s["dy"]) * rh) for s in liquid["strokes"]) or 1.0
    for stroke in liquid["strokes"]:
        cx, cy = float(stroke["x"]) * width, float(stroke["y"]) * height
        radius = max(1.0, float(stroke["radius"]) * min(width, height))
        x0, x1 = max(0, int(cx - radius)), min(width - 1, int(cx + radius))
        y0, y1 = max(0, int(cy - radius)), min(height - 1, int(cy + radius))
        pressure = float(stroke["pressure"])
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                distance = math.hypot(x - cx, y - cy)
                if distance >= radius:
                    continue
                influence = (1.0 - distance / radius) ** 2 * pressure
                at = y * width + x
                vx[at] += float(stroke["dx"]) * rw * influence
                vy[at] += float(stroke["dy"]) * rh * influence
    row_size = (width * 3 + 3) & ~3
    pixels = bytearray(row_size * height)
    for y in range(height):
        dest = (height - 1 - y) * row_size
        for x in range(width):
            at, off = y * width + x, dest + x * 3
            red = max(0, min(255, round(128 + 127 * vx[at] / max_x)))
            green = max(0, min(255, round(128 + 127 * vy[at] / max_y)))
            pixels[off:off + 3] = bytes((128, green, red))
    bmp = os.path.join(directory, "liquify-%d.bmp" % index)
    psd = os.path.join(directory, "liquify-%d.psd" % index)
    size = 54 + len(pixels)
    header = (b"BM" + struct.pack("<IHHI", size, 0, 0, 54) +
              struct.pack("<IIIHHIIIIII", 40, width, height, 1, 24, 0,
                          len(pixels), 2835, 2835, 0, 0))
    with open(bmp, "wb") as file:
        file.write(header); file.write(pixels)
    result = dict(region)
    result.update({"typeset_liquify_map": _fwd(bmp), "typeset_liquify_psd": _fwd(psd),
                   "typeset_liquify_x": max_x, "typeset_liquify_y": max_y})
    return result


def run_jsx(jsx: str, timeout: int = 900) -> str:
    """Отдаёт скрипт Photoshop через COM и возвращает его результат."""
    fd, jsx_path = tempfile.mkstemp(suffix=".jsx", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(jsx)

    ps = (
        "$ErrorActionPreference='Stop';"
        "$app = New-Object -ComObject Photoshop.Application;"
        "$app.DisplayDialogs = 3;"
        "$jsx = Get-Content -Raw -Encoding UTF8 -Path '%s';"
        "Write-Output $app.DoJavaScript($jsx)" % _fwd(jsx_path)
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0:
            raise RuntimeError("Photoshop вернул ошибку: " + (out.stderr or out.stdout).strip())
        return out.stdout.strip()
    finally:
        try:
            os.unlink(jsx_path)
        except OSError:
            pass


# PostScript-имя, а не то, что Photoshop показывает в списке.
# Свериться и вытащить имя: python host/fontcheck.py
DEFAULT_FONT = "NMDozor-Regular"


def render(analysis: dict, src_img: str, out_dir: str, font: str = DEFAULT_FONT,
           erase_only: bool = False, page_img: str = None,
           erase_ids: list = None, lang: str = translate.DEFAULT_TARGET) -> dict:
    """Верстает переводы поверх уже стёртой страницы; пути и отчёт по шагам.

    page_img — что открыть в Photoshop. Обычно это чистая копия из
    контейнера, а src_img остаётся именем: PSD, PNG и отчёт называются по
    странице главы, а не по временной копии.

    erase_ids — что Photoshop всё-таки стирает сам. Пустой список значит
    «всё стёрто до меня», None — стирать здесь всё, как было раньше.

    lang — короткий код целевого языка ("ru" / "en"), как в translate.TARGETS.
    Наружу ходит именно он, а не идентификатор Photoshop: тот длинный, его
    легко перепутать, и Photoshop на опечатку молча оставит прежний язык.
    Фактически выставленный язык видно в отчёте, шаг typeset_all, lang=...
    """
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_img))[0]
    psd = os.path.join(out_dir, stem + ".psd")
    png = os.path.join(out_dir, stem + ".png")
    rep = os.path.join(out_dir, stem + ".report.json")

    map_dir = tempfile.mkdtemp(prefix="manga-tl-liquify-")
    try:
        regions = [_liquify_map(region, map_dir, i) for i, region in enumerate(analysis["regions"])]
        jsx = jsxgen.build(
            src_img=_fwd(page_img or src_img), psd_out=_fwd(psd), png_out=_fwd(png), report_out=_fwd(rep),
            regions=regions, font=font, erase_only=erase_only,
            lang=translate.ps_language(lang), erase_ids=erase_ids,
        )
        result = run_jsx(jsx)
    finally:
        for name in os.listdir(map_dir):
            try:
                os.unlink(os.path.join(map_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(map_dir)
        except OSError:
            pass

    report = []
    if os.path.isfile(rep):
        with open(rep, encoding="utf-8") as f:
            report = json.load(f)
    return {"result": result, "psd": psd, "png": png, "report": report}


def _cli():
    if len(sys.argv) < 2:
        print(__doc__)
        print("команды: health | analyze <путь к картинке> | pages [sub]")
        return 1
    cmd = sys.argv[1]
    if cmd == "health":
        print(json.dumps(health(), ensure_ascii=False, indent=2))
    elif cmd == "analyze":
        a = analyze_file(sys.argv[2])
        print(json.dumps(a, ensure_ascii=False, indent=2))
    elif cmd == "pages":
        sub = sys.argv[2] if len(sys.argv) > 2 else ""
        url = CV_URL + "/pages?sub=" + urllib.request.quote(sub)
        with urllib.request.urlopen(url, timeout=30) as r:
            print(json.dumps(json.loads(r.read().decode("utf-8")), ensure_ascii=False, indent=2))
    else:
        print("неизвестная команда:", cmd)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

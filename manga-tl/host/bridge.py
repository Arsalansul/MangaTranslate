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
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jsxgen

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
    req = _upload("/inpaint", src_img, {
        "regions": json.dumps(analysis["regions"], ensure_ascii=False),
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
           erase_ids: list = None) -> dict:
    """Верстает переводы поверх уже стёртой страницы; пути и отчёт по шагам.

    page_img — что открыть в Photoshop. Обычно это чистая копия из
    контейнера, а src_img остаётся именем: PSD, PNG и отчёт называются по
    странице главы, а не по временной копии.

    erase_ids — что Photoshop всё-таки стирает сам. Пустой список значит
    «всё стёрто до меня», None — стирать здесь всё, как было раньше.
    """
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_img))[0]
    psd = os.path.join(out_dir, stem + ".psd")
    png = os.path.join(out_dir, stem + ".png")
    rep = os.path.join(out_dir, stem + ".report.json")

    jsx = jsxgen.build(
        src_img=_fwd(page_img or src_img), psd_out=_fwd(psd), png_out=_fwd(png), report_out=_fwd(rep),
        regions=analysis["regions"], font=font, erase_only=erase_only,
        erase_ids=erase_ids,
    )
    result = run_jsx(jsx)

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

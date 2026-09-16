"""Мост: CV-контейнер <-> Photoshop.

Работает на системном Python без сторонних пакетов — только stdlib и
PowerShell для COM. Это осознанно: тяжёлые зависимости живут в контейнере,
хост остаётся чистым.

Поток данных:
    контейнер /analyze_path -> PageAnalysis (JSON)
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
    """Просит контейнер найти и распознать текст на странице."""
    return _post("/analyze_path", {"path": page_rel, "lang": lang})


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


def render(analysis: dict, src_img: str, out_dir: str, font: str = "Arial-BoldMT",
           erase_only: bool = False) -> dict:
    """Стирает оригинал и верстает переводы; возвращает пути и отчёт по шагам."""
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_img))[0]
    psd = os.path.join(out_dir, stem + ".psd")
    png = os.path.join(out_dir, stem + ".png")
    rep = os.path.join(out_dir, stem + ".report.json")

    jsx = jsxgen.build(
        src_img=_fwd(src_img), psd_out=_fwd(psd), png_out=_fwd(png), report_out=_fwd(rep),
        regions=analysis["regions"], font=font, erase_only=erase_only,
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
        print("команды: health | analyze <page> | pages [sub]")
        return 1
    cmd = sys.argv[1]
    if cmd == "health":
        print(json.dumps(health(), ensure_ascii=False, indent=2))
    elif cmd == "analyze":
        a = analyze(sys.argv[2])
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

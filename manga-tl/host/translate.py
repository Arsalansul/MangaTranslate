"""Перевод реплик страницы.

Отдельный модуль, а не шаг пайплайна: вёрстка принимает PageAnalysis с уже
заполненным translation и не знает, откуда он взялся — из модели, из файла
или руками. Граница нужна, чтобы движок перевода можно было поменять, не
трогая ни контейнер, ни Photoshop.

Только stdlib: хост остаётся чистым, тяжёлые зависимости живут в контейнере.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"

# Регион переводится, если в нём есть что переводить и он не звук.
# SFX намеренно пропускаем: без перевода регион не стирается вовсе,
# и мазок остаётся на странице как есть.
MIN_FONT_PX = 10


class TranslateError(RuntimeError):
    pass


def translatable(region: dict) -> bool:
    return (
        bool((region.get("text") or "").strip())
        and region.get("kind") != "sfx"
        and int(region.get("font_px") or 0) >= MIN_FONT_PX
    )


PROMPT = """Ты переводишь комикс с английского на русский.

Перед тобой все реплики одной страницы по порядку чтения. Переводи их как
связный диалог, а не по отдельности: реплика получает смысл от соседних.

Правила:
- Живая разговорная речь, а не подстрочник. Это комикс, а не документ.
- Русский длиннее английского, а место в пузыре ограничено. Держись в
  пределах примерно 1.3 длины оригинала; где можно сказать короче — говори.
- Исходник распознан OCR с капса, поэтому в нём бывают подмены: O/0, I/l,
  D/O, склеенные и разорванные слова. Восстанавливай по смыслу молча.
- Многоточия, восклицания и обрывы фраз сохраняй: это интонация.
- Регистр не меняй — его выставит вёрстка.
- Если реплику перевести нельзя (мусор, обрывок одной буквы), верни пустую
  строку: такой регион останется нетронутым.
%(glossary)s
Верни ТОЛЬКО JSON-объект вида {"r001": "перевод", "r002": "перевод"} —
ключи те же, что пришли. Без пояснений и без markdown-ограды.

Реплики:
%(items)s"""


def _payload(regions, glossary):
    items = []
    for r in regions:
        items.append({
            "id": r["id"],
            "kind": r.get("kind") or "unknown",
            "lines": int(r.get("lines") or 1),
            "text": (r.get("text") or "").strip(),
        })
    gl = ""
    if glossary:
        pairs = "\n".join("  %s -> %s" % (k, v) for k, v in glossary.items())
        gl = "\nГлоссарий, соблюдать дословно:\n%s\n" % pairs
    return PROMPT % {
        "glossary": gl,
        "items": json.dumps(items, ensure_ascii=False, indent=1),
    }


def _call(prompt: str, api_key: str, model: str, timeout: int = 180) -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(API_URL, data=body, headers={
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
    })

    # Перегрузку и пятисотки повторяем: прогон главы идёт десятками запросов
    # подряд, и ронять его целиком из-за одного 529 бессмысленно.
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return "".join(b.get("text", "") for b in data.get("content", []))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 529):
                last = "HTTP %d: %s" % (e.code, detail)
                time.sleep(2 ** attempt)
                continue
            raise TranslateError("HTTP %d: %s" % (e.code, detail))
        except urllib.error.URLError as e:
            last = str(e)
            time.sleep(2 ** attempt)
    raise TranslateError("API недоступен после четырёх попыток: %s" % last)


def _parse(raw: str) -> dict:
    """Достаёт JSON из ответа, даже если модель обернула его в ограду."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        out = json.loads(text)
    except ValueError as e:
        raise TranslateError("Ответ модели не разобрался как JSON: %s" % e)
    if not isinstance(out, dict):
        raise TranslateError("Ожидался объект id -> перевод")
    return out


def translate_page(analysis: dict, api_key: str, model: str = DEFAULT_MODEL,
                   glossary: dict = None) -> int:
    """Проставляет translation в подходящих регионах. Возвращает их число."""
    targets = [r for r in analysis["regions"] if translatable(r)]
    if not targets:
        return 0

    got = _parse(_call(_payload(targets, glossary), api_key, model))

    filled = 0
    for r in targets:
        value = (got.get(r["id"]) or "").strip()
        if not value:
            continue
        # Капс восстанавливаем здесь, а не просим у модели: так результат
        # не зависит от того, послушалась она или нет.
        if r.get("all_caps"):
            value = value.upper()
        r["translation"] = value
        filled += 1
    return filled


def load_glossary(path: str) -> dict:
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise TranslateError("Глоссарий должен быть объектом {\"Enkrid\": \"Энкрид\"}")
    return data


def api_key_from_env() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()

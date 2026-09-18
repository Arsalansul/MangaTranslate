"""Перевод реплик страницы.

Отдельный модуль, а не шаг пайплайна: вёрстка принимает PageAnalysis с уже
заполненным translation и не знает, откуда он взялся — из модели, из файла
или руками. Граница нужна, чтобы движок перевода можно было поменять, не
трогая ни контейнер, ни Photoshop.

Движков два вида. Anthropic — свой протокол; всё остальное (Gemini, Groq,
OpenRouter, локальные Ollama и LM Studio) говорит по OpenAI-совместимому
/chat/completions, поэтому один код покрывает их скопом. Меняется только
адрес, имя модели и переменная с ключом.

Только stdlib: хост остаётся чистым, тяжёлые зависимости живут в контейнере.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_VERSION = "2023-06-01"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Пауза перед повтором растёт вдвое: 3, 6, 12, 24 секунды. Бесплатная модель
# отпускает лимит за десятки секунд, и переждать дешевле, чем потерять страницу.
RETRY_BASE_S = 3
ATTEMPTS = 5

# Готовые адреса. Модель у каждого можно переопределить: списки меняются
# чаще, чем этот файл, а у бесплатных провайдеров — особенно часто.
BACKENDS = {
    "anthropic": {
        "kind": "anthropic",
        "url": "https://api.anthropic.com/v1/messages",
        "key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-5",
        "json_mode": False,
        "note": "платный, по токенам; лучшее качество перевода",
    },
    "gemini": {
        "kind": "openai",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-2.5-flash",
        "json_mode": True,
        "note": "бесплатный лимит; ключ в Google AI Studio",
    },
    "groq": {
        "kind": "openai",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
        "json_mode": True,
        "note": "бесплатный лимит; быстрый, по-русски слабее",
    },
    "openrouter": {
        "kind": "openai",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "z-ai/glm-5.2:free",
        "json_mode": True,
        "note": "витрина чужих моделей; список: translate.py openrouter",
    },
    "ollama": {
        "kind": "openai",
        "url": "http://127.0.0.1:11434/v1/chat/completions",
        "key_env": None,
        "model": "qwen2.5:14b",
        "json_mode": True,
        "note": "локально, без ключа и без лимитов; качество ниже",
    },
    "lmstudio": {
        "kind": "openai",
        "url": "http://127.0.0.1:1234/v1/chat/completions",
        "key_env": None,
        "model": "local-model",
        "json_mode": False,
        "note": "локально, модель выбирается в самом LM Studio",
    },
}

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = BACKENDS[DEFAULT_PROVIDER]["model"]

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
- Текст с переносами строк — это список: содержание, титры, подпись. Верни
  столько же строк, в том же порядке, разделённых теми же переносами. Строки
  не сливай и не переставляй; номера страниц и цифры оставь как есть.
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
            "keep_lines": bool(r.get("keep_lines")),
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


def _parse(raw: str) -> dict:
    """Достаёт JSON из ответа, даже если модель обернула его в ограду."""
    text = (raw or "").strip()
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


class Engine:
    """Один провайдер перевода: куда стучаться, чем и под каким ключом."""

    def __init__(self, provider: str = DEFAULT_PROVIDER, model: str = None,
                 api_key: str = None, url: str = None, timeout: int = 180):
        if provider not in BACKENDS:
            raise TranslateError(
                "Неизвестный провайдер %r. Есть: %s"
                % (provider, ", ".join(sorted(BACKENDS))))
        cfg = BACKENDS[provider]
        self.provider = provider
        self.kind = cfg["kind"]
        self.url = url or cfg["url"]
        self.model = model or cfg["model"]
        self.json_mode = cfg["json_mode"]
        self.key_env = cfg["key_env"]
        self.api_key = api_key or (os.environ.get(cfg["key_env"], "").strip()
                                   if cfg["key_env"] else "")
        # Локальная модель на CPU думает минутами, а не секундами.
        self.timeout = timeout if self.key_env else max(timeout, 900)

    def describe(self) -> str:
        return "%s / %s" % (self.provider, self.model)

    def check(self):
        """Ключ проверяем до первой страницы: глава — это десятки минут."""
        if self.key_env and not self.api_key:
            raise TranslateError(
                "Нет ключа для %s. Задайте %s или --api-key.\n"
                "Бесплатные варианты: --provider gemini | groq | openrouter "
                "(свои ключи, бесплатный лимит) или --provider ollama "
                "(локально, без ключа).\n"
                "Совсем без модели: --no-translate (только стирание) или "
                "--reuse (переводы из сохранённых analysis.json)."
                % (self.provider, self.key_env))

    # --- транспорт ----------------------------------------------------

    def _request(self, prompt, json_mode):
        if self.kind == "anthropic":
            body = {
                "model": self.model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }
            headers = {
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
            }
        else:
            body = {
                "model": self.model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            headers = {"content-type": "application/json"}
            if self.api_key:
                headers["authorization"] = "Bearer " + self.api_key
        return urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"), headers=headers)

    def _text(self, data):
        if self.kind == "anthropic":
            return "".join(b.get("text", "") for b in data.get("content", []))
        choices = data.get("choices") or []
        if not choices:
            raise TranslateError("Пустой ответ: %s" % json.dumps(data)[:300])
        return choices[0].get("message", {}).get("content") or ""

    def _call(self, prompt: str) -> str:
        json_mode = self.json_mode
        # Перегрузку и пятисотки повторяем: прогон главы идёт десятками
        # запросов подряд, и ронять его целиком из-за одного 429 бессмысленно.
        last = None
        for attempt in range(ATTEMPTS):
            try:
                with urllib.request.urlopen(self._request(prompt, json_mode),
                                            timeout=self.timeout) as resp:
                    return self._text(json.loads(resp.read().decode("utf-8")))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                # JSON-режим поддерживают не все модели на OpenAI-совместимых
                # витринах. Отказ от него дешевле, чем падение главы: разбор
                # ответа всё равно умеет доставать объект из текста.
                if e.code == 400 and json_mode and "response_format" in detail:
                    json_mode = False
                    continue
                if e.code in (429, 500, 502, 503, 529):
                    last = "HTTP %d: %s" % (e.code, detail)
                    time.sleep(self._pause(attempt, e))
                    continue
                raise TranslateError("HTTP %d: %s" % (e.code, detail))
            except urllib.error.URLError as e:
                last = str(e)
                time.sleep(self._pause(attempt))
        hint = ""
        if "429" in (last or ""):
            # Бесплатная модель делит чужую квоту со всей витриной, и
            # переждать её в рамках одного прогона обычно нельзя.
            hint = ("\nЭто лимит, а не ошибка запроса. У бесплатных моделей он "
                    "общий на всех: возьмите другую (python host/translate.py "
                    "openrouter) или платную.")
        raise TranslateError("%s недоступен после %d попыток: %s%s"
                             % (self.url, ATTEMPTS, last, hint))

    @staticmethod
    def _pause(attempt, err=None):
        """Сколько ждать перед повтором: провайдер часто говорит это сам."""
        after = ""
        if err is not None and getattr(err, "headers", None):
            after = (err.headers.get("retry-after") or "").strip()
        if after.isdigit():
            return min(int(after), 60)
        return RETRY_BASE_S * 2 ** attempt

    # --- то, ради чего всё -------------------------------------------

    def translate_page(self, analysis: dict, glossary: dict = None,
                       keep_filled: bool = False) -> int:
        """Проставляет translation в подходящих регионах. Возвращает их число.

        keep_filled бережёт уже заполненное: перевод в analysis.json могли
        поправить руками, и затирать правку своим вариантом нельзя. В промпт
        такие реплики всё равно идут — соседние реплики дают контекст.
        """
        targets = [r for r in analysis["regions"] if translatable(r)]
        if not targets:
            return 0
        if keep_filled and all((r.get("translation") or "").strip() for r in targets):
            return 0

        got = _parse(self._call(_payload(targets, glossary)))

        filled = 0
        for r in targets:
            if keep_filled and (r.get("translation") or "").strip():
                continue
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


def providers_help() -> str:
    rows = []
    for name in sorted(BACKENDS):
        cfg = BACKENDS[name]
        rows.append("  %-11s %-24s %s" % (name, cfg["model"], cfg["note"]))
    return "\n".join(rows)


def openrouter_free_models(limit: int = 20) -> list:
    """Бесплатные модели OpenRouter живьём.

    Витрина меняется быстрее этого файла: модель, записанная в BACKENDS
    сегодня, через месяц может исчезнуть. Ключ для списка не нужен.
    """
    with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    free = [m for m in data.get("data", []) if (m.get("id") or "").endswith(":free")]
    free.sort(key=lambda m: -(m.get("context_length") or 0))
    return free[:limit]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "openrouter":
        print("Бесплатные модели OpenRouter (--model <id>):")
        for m in openrouter_free_models():
            print("  %-52s контекст %s" % (m["id"], m.get("context_length") or "?"))
    else:
        print("Провайдеры перевода:\n" + providers_help())
        print("\nБесплатные модели OpenRouter меняются; свежий список:"
              "\n  python host/translate.py openrouter")

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
        "no_think": True,
        "note": "локально, без ключа и без лимитов; качество ниже",
    },
    "lmstudio": {
        "kind": "openai",
        "url": "http://127.0.0.1:1234/v1/chat/completions",
        "key_env": None,
        "model": "local-model",
        "json_mode": True,
        "no_think": True,
        "note": "локально, модель берётся та, что загружена в LM Studio",
    },
}

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = BACKENDS[DEFAULT_PROVIDER]["model"]

# Подсказки про ошибки OCR отличаются не по языку, а по письменности: у
# капсовой латиницы путаются O/0 и I/l, у иероглифов — начертания, и один
# набор грабель на всех не натянуть.
_OCR_LATIN = (
    "- Исходник распознан OCR с капса, поэтому в нём бывают подмены: O/0, I/l,\n"
    "  D/O, склеенные и разорванные слова. Восстанавливай по смыслу молча."
)
_OCR_HANGUL = (
    "- Исходник распознан OCR, поэтому в нём бывают ошибки письма: похожие\n"
    "  начертанием чамо и слоги (ㅁ/ㅇ, ㅜ/ㅠ, ㅌ/ㄷ, 己/2), лишние пробелы\n"
    "  внутри слова и потерянные знаки препинания. Восстанавливай по смыслу\n"
    "  молча."
)
_OCR_HAN = (
    "- Исходник распознан OCR, поэтому в нём бывают ошибки письма: визуально\n"
    "  похожие иероглифы (己/已/巳, 千/干, 未/末, 人/入), лишние пробелы между\n"
    "  знаками и потерянные знаки препинания. Восстанавливай по смыслу молча."
)

# Исходники. Ключ — код Tesseract, тот же, что уходит в OCR контейнера.
# name в родительном падеже: он встаёт и в «переводишь с ...», и в
# «... длиннее ...», так что форма нужна одна.
SOURCES = {
    "eng": {"name": "английского", "ocr": _OCR_LATIN},
    "fra": {"name": "французского", "ocr": _OCR_LATIN},
    "deu": {"name": "немецкого", "ocr": _OCR_LATIN},
    "spa": {"name": "испанского", "ocr": _OCR_LATIN},
    "ita": {"name": "итальянского", "ocr": _OCR_LATIN},
    "por": {"name": "португальского", "ocr": _OCR_LATIN},
    "kor": {"name": "корейского", "ocr": _OCR_HANGUL},
    "chi_sim": {"name": "китайского (упрощённого)", "ocr": _OCR_HAN},
    "chi_tra": {"name": "китайского (традиционного)", "ocr": _OCR_HAN},
}

# Целевые языки. КОНТРАКТ: ключи ровно "ru" и "en", поля name / ps_lang /
# glyphs. На эту таблицу смотрят и вёрстка (ps_lang), и проверка шрифта
# (glyphs), поэтому менять имена полей нельзя не предупредив.
#
# ps_lang — идентификатор языка Photoshop для textLanguage. Photoshop на
# неизвестное значение не ругается, а молча оставляет прежний язык, поэтому
# каждое значение здесь должно быть сверено на живом Photoshop через
# languageOf() в jsxgen.py: она кладёт фактический язык в report.json.
TARGETS = {
    "ru": {
        "name": "русский",
        "ps_lang": "russianLanguage",  # сверено: languageOf() отдаёт его же
        # Латиница нужна и для русской страницы: имена, «OK», номера глав.
        "glyphs": ("cyrillic", "latin", "digits", "punct"),
    },
    "en": {
        "name": "английский",
        # Сверено: englishUSALanguage по аналогии с russianLanguage НЕ
        # подходит — Photoshop его молча игнорирует. Контрольный прогон с
        # заведомой чушью дал тот же ответ, что и с englishUSALanguage,
        # то есть оба раза вставало умолчание. Верный идентификатор короче.
        "ps_lang": "englishLanguage",
        "glyphs": ("latin", "digits", "punct"),
    },
}

DEFAULT_SOURCE = "eng"
DEFAULT_TARGET = "ru"

# Во сколько раз перевод длиннее оригинала. Зависит от пары, а не от одного
# языка: с иероглифов растягивает сильнее всего (знак плотнее любой буквенной
# записи), с корейского — заметно, с английского — умеренно. Запасное
# значение для пар, которых тут нет.
DEFAULT_RATIO = 1.3
RATIOS = {
    ("eng", "ru"): 1.3,
    ("kor", "ru"): 1.6,
    ("kor", "en"): 1.5,
    ("chi_sim", "ru"): 1.9,
    ("chi_tra", "ru"): 1.9,
    ("chi_sim", "en"): 1.7,
    ("chi_tra", "en"): 1.7,
}

# Промпт написан по-русски — его читает и правит владелец проекта, а не
# модель-носитель. Для английского результата это опасно: модель тянет
# многословие самого промпта в перевод, поэтому цель проговаривается отдельно.
TARGET_NOTES = {
    "ru": "",
    "en": (
        "\n- Промпт написан по-русски, но переводишь ты на английский:"
        " значения в\n  JSON — английский текст. Не тяни в него русскую"
        " многословность, не\n  удлиняй фразу и не добавляй слов, которых"
        " нет в оригинале."
    ),
}

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


PROMPT = """Ты переводишь комикс с %(src)s на %(dst)s.

Перед тобой все реплики одной страницы по порядку чтения. Переводи их как
связный диалог, а не по отдельности: реплика получает смысл от соседних.

Правила:
- Живая разговорная речь, а не подстрочник. Это комикс, а не документ.
- %(dst_cap)s длиннее %(src)s, а место в пузыре ограничено. Держись в
  пределах примерно %(ratio)s длины оригинала; где можно сказать короче — говори.
%(ocr)s%(note)s
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


def source_info(src_lang: str) -> dict:
    try:
        return SOURCES[src_lang]
    except KeyError:
        raise TranslateError(
            "Неизвестный язык оригинала %r. Есть: %s"
            % (src_lang, ", ".join(sorted(SOURCES))))


def target_info(target: str) -> dict:
    try:
        return TARGETS[target]
    except KeyError:
        raise TranslateError(
            "Неизвестный целевой язык %r. Есть: %s"
            % (target, ", ".join(sorted(TARGETS))))


def ps_language(target: str = DEFAULT_TARGET) -> str:
    """Короткий код целевого языка -> идентификатор языка Photoshop."""
    return target_info(target)["ps_lang"]


def glyph_sets(target: str = DEFAULT_TARGET) -> tuple:
    """Какие наборы глифов обязан покрывать шрифт для этого языка."""
    return tuple(target_info(target)["glyphs"])


def ratio(src_lang: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET) -> float:
    return RATIOS.get((src_lang, target), DEFAULT_RATIO)


def build_prompt(src_lang: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET,
                 glossary: str = "", items: str = "") -> str:
    """Промпт под конкретную пару языков. Сам текст задания остаётся русским."""
    src = source_info(src_lang)
    dst = target_info(target)
    name = dst["name"]
    return PROMPT % {
        "src": src["name"],
        "dst": name,
        "dst_cap": name[:1].upper() + name[1:],
        "ratio": "%g" % ratio(src_lang, target),
        "ocr": src["ocr"],
        "note": TARGET_NOTES.get(target, ""),
        "glossary": glossary,
        "items": items,
    }


def _payload(regions, glossary, src_lang: str = DEFAULT_SOURCE,
             target: str = DEFAULT_TARGET):
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
    return build_prompt(
        src_lang, target,
        glossary=gl,
        items=json.dumps(items, ensure_ascii=False, indent=1),
    )


_PAIR = re.compile(r'"([\w.:-]+)"\s*:\s*"(.*?)"\s*(?=,\s*"|\s*\}|\s*$)', re.S)


def _parse(raw: str) -> dict:
    """Достаёт JSON из ответа, даже если модель обернула его в ограду."""
    text = (raw or "").strip()
    # Локальные reasoning-модели рассуждают вслух перед ответом, и в черновике
    # тоже попадаются фигурные скобки: поиск по первой «{» вытащил бы
    # рассуждение вместо ответа.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
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
        # Локальная модель нет-нет да и поставит внутри реплики живую кавычку
        # («ЛИЛЬ МИТЧ"»), не экранировав её. Из-за одной такой страница целиком
        # оставалась без перевода, хотя остальные два десятка строк в ответе
        # разобрались бы. Подбираем пары «id: строка» вручную: значение тянем
        # до кавычки, за которой идёт запятая с новым id или конец объекта.
        out = dict(_PAIR.findall(text))
        if not out:
            raise TranslateError("Ответ модели не разобрался как JSON: %s" % e)
    if not isinstance(out, dict):
        raise TranslateError("Ожидался объект id -> перевод")
    return out


class Engine:
    """Один провайдер перевода: куда стучаться, чем и под каким ключом."""

    def __init__(self, provider: str = DEFAULT_PROVIDER, model: str = None,
                 api_key: str = None, url: str = None, timeout: int = 180,
                 src_lang: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET):
        if provider not in BACKENDS:
            raise TranslateError(
                "Неизвестный провайдер %r. Есть: %s"
                % (provider, ", ".join(sorted(BACKENDS))))
        # Пара языков живёт на движке, а не на вызове: она одна на всю главу.
        # Проверяем сразу — ошибиться в коде языка дешевле здесь, чем на
        # сотой странице.
        source_info(src_lang)
        target_info(target)
        self.src_lang = src_lang
        self.target = target
        cfg = BACKENDS[provider]
        self.provider = provider
        self.kind = cfg["kind"]
        self.url = url or cfg["url"]
        self.model = model or cfg["model"]
        self.model_given = bool(model)
        self.json_mode = cfg["json_mode"]
        self.no_think = cfg.get("no_think", False)
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
        if not self.key_env:
            self._resolve_local()

    def _resolve_local(self):
        """Спрашивает у локального сервера, что в него загружено.

        Имя в BACKENDS для локального провайдера — заглушка: модель выбирает
        пользователь в LM Studio или Ollama, и угадать её нельзя. Заглушка
        уезжала в запрос как есть, сервер отвечал 404, и глава падала на
        первой же странице. Спросить дешевле, чем угадать, а заодно это
        проверка, что сервер вообще поднят.
        """
        url = self.url.split("/chat/completions")[0] + "/models"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise TranslateError(
                "Локальный сервер %s не отвечает: %s\n"
                "В LM Studio это вкладка Developer, тумблер Status: Running; "
                "порт по умолчанию 1234." % (url, e))
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if self.model in ids:
            return
        if self.model_given:
            raise TranslateError(
                "Модели %r на %s нет. Загружены: %s"
                % (self.model, url, ", ".join(ids) or "ничего"))
        # Эмбеддер LM Studio ставит сама, и он всегда в списке первым.
        chat = [i for i in ids if "embed" not in i.lower()]
        if not chat:
            raise TranslateError(
                "На %s не загружено ни одной языковой модели (есть только: "
                "%s).\nСкачайте и загрузите модель в LM Studio, затем "
                "повторите." % (url, ", ".join(ids) or "ничего"))
        self.model = chat[0]

    # --- транспорт ----------------------------------------------------

    def _request(self, prompt, json_mode, no_think=False):
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
            if no_think:
                # Локальные модели всё чаще reasoning: рассуждение перед
                # ответом стоит сотен токенов на реплику в три слова, а на
                # домашней карте это минуты. Переводу оно не нужно.
                body["reasoning_effort"] = "none"
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
        no_think = self.no_think
        # Перегрузку и пятисотки повторяем: прогон главы идёт десятками
        # запросов подряд, и ронять его целиком из-за одного 429 бессмысленно.
        last = None
        for attempt in range(ATTEMPTS):
            try:
                req = self._request(prompt, json_mode, no_think)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return self._text(json.loads(resp.read().decode("utf-8")))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                # JSON-режим поддерживают не все модели на OpenAI-совместимых
                # витринах. Отказ от него дешевле, чем падение главы: разбор
                # ответа всё равно умеет доставать объект из текста.
                if e.code == 400 and json_mode and "response_format" in detail:
                    json_mode = False
                    continue
                if e.code == 400 and no_think and "reasoning_effort" in detail:
                    no_think = False
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
                       keep_filled: bool = False, src_lang: str = None,
                       target: str = None) -> int:
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

        got = _parse(self._call(_payload(
            targets, glossary,
            src_lang or self.src_lang, target or self.target)))

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


def languages_help() -> str:
    rows = ["  оригинал (--lang): " + ", ".join(sorted(SOURCES)),
            "  перевод:"]
    for code in sorted(TARGETS):
        t = TARGETS[code]
        rows.append("    %-3s %-10s Photoshop: %-18s глифы: %s"
                    % (code, t["name"], t["ps_lang"], ", ".join(t["glyphs"])))
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
        print("\nЯзыки:\n" + languages_help())
        print("\nБесплатные модели OpenRouter меняются; свежий список:"
              "\n  python host/translate.py openrouter")

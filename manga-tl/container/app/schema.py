"""Контракт между CV-контейнером и хостовым тайпсеттером (Photoshop).

Это единственная стабильная точка системы. Модели детекта и OCR внутри
контейнера можно менять как угодно, но форма Region меняться не должна:
на неё завязан скрипт, который стирает и верстает в Photoshop.

Система координат: пиксели исходного изображения, начало в левом верхнем
углу, ось Y вниз. Та же, что у Photoshop при rulerUnits = PIXELS, поэтому
координаты переносятся без пересчёта.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class Region(BaseModel):
    id: str = Field(description="Стабильный в пределах страницы: r001, r002, ...")

    # --- геометрия ---
    bbox: List[int] = Field(description="[x, y, w, h] — рамка текста, без запаса")
    mask_poly: List[List[int]] = Field(
        default_factory=list,
        description="Контур для стирания. Уже расширен (dilate) относительно bbox: "
                    "заливать надо с запасом, иначе остаются хвосты от глифов.",
    )
    angle: float = Field(0.0, description="Наклон строк в градусах, по часовой")
    safe_box: List[int] = Field(
        default_factory=list,
        description="[x, y, w, h] — куда можно верстать перевод: свободное поле "
                    "балуна, а не рамка бывшего текста. Перевод почти всегда "
                    "длиннее оригинала и в bbox помещается только нечитаемым "
                    "кеглем. Пусто — безопасной площади нет, верстать по bbox.",
    )

    # --- содержание ---
    text: str = Field("", description="Распознанный исходный текст")
    conf: float = Field(0.0, description="Уверенность OCR, 0..1")
    lines: int = Field(1, description="Сколько строк было в оригинале")

    # --- стиль, для переноса в перевод ---
    font_px: int = Field(0, description="Оценка кегля по высоте заглавных, px")
    line_h_px: int = Field(0, description="Оценка интерлиньяжа, px")
    all_caps: bool = Field(False, description="Оригинал набран капсом")
    fg: List[int] = Field(default_factory=lambda: [0, 0, 0], description="RGB текста")
    bg: List[int] = Field(default_factory=lambda: [255, 255, 255], description="RGB подложки")

    # --- классификация: определяет стратегию стирания и вёрстки ---
    kind: Literal["bubble", "caption", "sfx", "unknown"] = Field(
        "unknown",
        description="bubble — текст в баббле, чистая заливка. "
                    "caption — прямоугольная плашка. "
                    "sfx — звук поверх арта, нужен аккуратный инпейнт и обводка.",
    )
    on_art: bool = Field(
        False,
        description="Текст лежит на рисунке, а не на однотонном фоне. "
                    "Для таких Content-Aware Fill рискован, нужен контроль глазами.",
    )

    # --- заполняется на стороне хоста ---
    translation: Optional[str] = Field(None, description="Перевод; контейнер его не трогает")


class PageAnalysis(BaseModel):
    page: str
    width: int
    height: int
    regions: List[Region]
    detector: str = Field(description="Чем детектили — для воспроизводимости")
    ocr: str = Field(description="Чем распознавали")
    warnings: List[str] = Field(default_factory=list)

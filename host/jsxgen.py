"""Генератор ExtendScript для Photoshop.

Почему генерация текста, а не нормальный API: в ExtendScript нет ни JSON,
ни надёжной работы с не-ASCII в исходнике. Поэтому весь скрипт собирается
здесь, кириллица уходит u-экранированной, а данные вшиваются
литералами прямо в код.

Твёрдое правило: в генерируемом JS не должно быть ни одного литерального
обратного слэша — они теряются по дороге через COM. Все спецсимволы
строим через String.fromCharCode. Проверено на грабли.
"""
import json
from typing import Dict, List, Any

BS = chr(92)


def esc(s: str) -> str:
    """Строка -> ASCII-безопасное JS-выражение: литерал или их склейка.

    Перенос строки — не символ внутри литерала, а разрыв между ними: конец
    абзаца в Photoshop это CR, и собирается он String.fromCharCode, как и
    все прочие спецсимволы здесь. Переносы приходят из списков (содержание,
    титры), где разбиение смысловое; прочие управляющие символы — шум.
    """
    parts, out = [], ['"']
    for ch in str(s):
        k = ord(ch)
        if ch == chr(10):
            out.append('"')
            parts.append("".join(out))
            parts.append("String.fromCharCode(13)")
            out = ['"']
        elif ch == '"':
            out.append(BS + '"')
        elif k == 92:
            out.append(BS + BS)
        elif k < 32:
            out.append(" ")
        elif k > 126:
            out.append(BS + "u" + format(k, "04X"))
        else:
            out.append(ch)
    out.append('"')
    parts.append("".join(out))
    return " + ".join(parts)


def _region_literal(r: Dict[str, Any]) -> str:
    x, y, w, h = r["bbox"]
    poly = r.get("mask_poly") or [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
    poly_js = "[" + ",".join("[%d,%d]" % (p[0], p[1]) for p in poly) + "]"
    fg = r.get("fg") or [0, 0, 0]
    color = r.get("typeset_color") or fg
    bg = r.get("bg") or [255, 255, 255]
    effects = r.get("typeset_effects")
    if effects is None:
        legacy = {"stroke": r.get("typeset_stroke"), "gradient": r.get("typeset_gradient"),
                  "shadow": r.get("typeset_shadow")}
        effects = [legacy] if any(legacy.values()) else []
    # Стираем по bbox/полигону, а верстаем по safe_box: стереть надо ровно
    # бывший текст, а поставить — с запасом, который даёт балун.
    sx, sy, sw, sh = r.get("safe_box") or [x, y, w, h]
    return (
        "{id:%s,x:%d,y:%d,w:%d,h:%d,sx:%d,sy:%d,sw:%d,sh:%d,poly:%s,txt:%s,"
        "size:%d,lead:%d,fixedSize:%d,fixedLead:%d,kind:%s,font:%s,align:%s,"
        "onArt:%s,keep:%s,forceErase:%s,bold:%s,italic:%s,underline:%s,"
        "effects:%s,blur:%s,fg:[%d,%d,%d],bg:[%d,%d,%d]}"
        % (
            esc(r["id"]), x, y, w, h, sx, sy, sw, sh, poly_js,
            esc(r.get("translation") or ""),
            int(r.get("font_px") or 24),
            int(r.get("line_h_px") or 0),
            int(r.get("typeset_size") or 0),
            int(r.get("typeset_leading") or 0),
            esc(r.get("kind") or "unknown"),
            esc(r.get("font") or ""),
            esc(r.get("typeset_align") or ""),
            "true" if r.get("on_art") else "false",
            "true" if r.get("keep_lines") else "false",
            "true" if r.get("erase_only") else "false",
            "true" if r.get("typeset_bold") else "false",
            "true" if r.get("typeset_italic") else "false",
            "true" if r.get("typeset_underline") else "false",
            json.dumps(effects, separators=(",", ":")),
            json.dumps(r.get("typeset_blur"), separators=(",", ":")),
            color[0], color[1], color[2],
            bg[0], bg[1], bg[2],
        )
    )


HEADER = """
#target photoshop
(function () {
var BS = String.fromCharCode(92), QU = String.fromCharCode(34);
function jstr(s) {
  s = String(s); var o = QU;
  for (var i = 0; i < s.length; i++) {
    var c = s.charAt(i), k = s.charCodeAt(i);
    if (k == 34) o += BS + QU;
    else if (k == 92) o += BS + BS;
    else if (k < 32) o += ' ';
    else if (k > 126) o += BS + 'u' + ('000' + k.toString(16).toUpperCase()).slice(-4);
    else o += c;
  }
  return o + QU;
}
var R = [];
// Пункты на пиксель. Координаты слоя Photoshop принимает в пикселях, а
// размеры текста — кегль, интерлиньяж, рамку абзаца — в пунктах, и делает это
// молча: записанное значение читается обратно как есть. На странице 96 PPI
// рамка выходила на треть шире заказанной, а кегль на треть крупнее, отчего
// текст лез за балун. Всё, что идёт в textItem, кроме position, множится на K.
var K = 1.0;
function step(name, fn) {
  try { var v = fn(); R.push('{"step":' + jstr(name) + ',"ok":true,"info":' + jstr(v) + '}'); return v; }
  catch (e) { R.push('{"step":' + jstr(name) + ',"ok":false,"info":' + jstr(e) + '}'); return null; }
}
function writeFile(p, txt) { var f = new File(p); f.encoding = 'UTF-8'; f.open('w'); f.write(txt); f.close(); }

// indexOf у массивов в ExtendScript нет: движок старый, ES3.
function inList(v, list) {
  for (var i = 0; i < list.length; i++) if (list[i] === v) return true;
  return false;
}

function solidFill(rgb) {
  var c = new SolidColor();
  c.rgb.red = rgb[0]; c.rgb.green = rgb[1]; c.rgb.blue = rgb[2];
  doc.selection.fill(c, ColorBlendMode.NORMAL, 100, false);
}

function contentAwareFill() {
  var d = new ActionDescriptor();
  d.putEnumerated(charIDToTypeID('Usng'), charIDToTypeID('FlCn'), stringIDToTypeID('contentAware'));
  d.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('#Prc'), 100.0);
  d.putEnumerated(charIDToTypeID('Md  '), charIDToTypeID('BlnM'), charIDToTypeID('Nrml'));
  executeAction(charIDToTypeID('Fl  '), d, DialogModes.NO);
}

// Язык текстового слоя: от него зависит словарь переносов, а без переносов
// длинное слово не разбивается и торчит за балун. В DOM ExtendScript русского
// языка нет вовсе (Language.RUSSIAN отсутствует), поэтому только Action Manager.
//
// Идентификатор именно russianLanguage. На 'russian' Photoshop не ругается —
// он ставит английский, то есть ровно противоположное просимому. Умолчание
// зависит от локали интерфейса, так что язык задаётся явно и сверяется.
function setLanguage(lang) {
  var ref = new ActionReference();
  ref.putProperty(charIDToTypeID('Prpr'), charIDToTypeID('TxtS'));
  ref.putEnumerated(charIDToTypeID('TxLr'), charIDToTypeID('Ordn'), charIDToTypeID('Trgt'));
  var d = new ActionDescriptor();
  d.putReference(charIDToTypeID('null'), ref);
  var style = new ActionDescriptor();
  style.putEnumerated(stringIDToTypeID('textLanguage'),
                      stringIDToTypeID('textLanguage'), stringIDToTypeID(lang));
  d.putObject(charIDToTypeID('T   '), charIDToTypeID('TxtS'), style);
  executeAction(charIDToTypeID('setd'), d, DialogModes.NO);
}

// Проверяем, что язык действительно встал: на неизвестный идентификатор
// Photoshop не ругается, а тихо оставляет прежний.
function languageOf() {
  // Читать стиль через Prpr/TxtS нельзя — Photoshop отвечает, что команда
  // недоступна. Язык лежит в textKey, в первом отрезке стиля.
  var ref = new ActionReference();
  ref.putProperty(stringIDToTypeID('property'), stringIDToTypeID('textKey'));
  ref.putEnumerated(stringIDToTypeID('layer'), stringIDToTypeID('ordinal'),
                    stringIDToTypeID('targetEnum'));
  var tk = executeActionGet(ref).getObjectValue(stringIDToTypeID('textKey'));
  var st = tk.getList(stringIDToTypeID('textStyleRange'))
             .getObjectValue(0).getObjectValue(stringIDToTypeID('textStyle'));
  var k = stringIDToTypeID('textLanguage');
  return st.hasKey(k) ? typeIDToStringID(st.getEnumerationValue(k)) : 'unset';
}

function rgbObject(rgb) {
  var d = new ActionDescriptor();
  d.putDouble(stringIDToTypeID('red'), rgb[0]);
  d.putDouble(stringIDToTypeID('green'), rgb[1]);
  d.putDouble(stringIDToTypeID('blue'), rgb[2]);
  return d;
}

function gradientObject(g) {
  var d = new ActionDescriptor();
  d.putString(stringIDToTypeID('name'), 'manga-tl');
  d.putEnumerated(stringIDToTypeID('gradientForm'), stringIDToTypeID('gradientForm'),
                  stringIDToTypeID('customStops'));
  d.putDouble(stringIDToTypeID('interfaceIconFrameDimmed'), 4096);
  var colors = new ActionList();
  function stop(rgb, at) {
    var s = new ActionDescriptor();
    s.putObject(stringIDToTypeID('color'), stringIDToTypeID('RGBColor'), rgbObject(rgb));
    s.putEnumerated(stringIDToTypeID('type'), stringIDToTypeID('colorStopType'),
                    stringIDToTypeID('userStop'));
    s.putInteger(stringIDToTypeID('location'), at);
    s.putInteger(stringIDToTypeID('midpoint'), 50);
    colors.putObject(stringIDToTypeID('colorStop'), s);
  }
  stop(g.color1, 0); stop(g.color2, 4096);
  d.putList(stringIDToTypeID('colors'), colors);
  var transparency = new ActionList();
  function opacityStop(at) {
    var s = new ActionDescriptor();
    s.putUnitDouble(stringIDToTypeID('opacity'), stringIDToTypeID('percentUnit'), 100);
    s.putInteger(stringIDToTypeID('location'), at); s.putInteger(stringIDToTypeID('midpoint'), 50);
    transparency.putObject(stringIDToTypeID('transferSpec'), s);
  }
  opacityStop(0); opacityStop(4096);
  d.putList(stringIDToTypeID('transparency'), transparency);
  return d;
}

function applyLayerEffects(r) {
  var groups = r.effects || [], strokes = [], gradients = [], shadows = [];
  for (var gi = 0; gi < groups.length; gi++) {
    if (groups[gi].stroke) strokes.push(groups[gi].stroke);
    if (groups[gi].gradient) gradients.push(groups[gi].gradient);
    if (groups[gi].shadow) shadows.push(groups[gi].shadow);
  }
  if (!strokes.length && !gradients.length && !shadows.length) return;
  var fx = new ActionDescriptor();
  fx.putUnitDouble(stringIDToTypeID('scale'), stringIDToTypeID('percentUnit'), 100);
  function strokeDesc(value) {
    var st = new ActionDescriptor();
    st.putBoolean(stringIDToTypeID('enabled'), true);
    st.putBoolean(stringIDToTypeID('present'), true);
    st.putBoolean(stringIDToTypeID('showInDialog'), true);
    st.putEnumerated(stringIDToTypeID('style'), stringIDToTypeID('frameStyle'),
                     stringIDToTypeID('outsetFrame'));
    st.putEnumerated(stringIDToTypeID('paintType'), stringIDToTypeID('frameFill'),
                     stringIDToTypeID('solidColor'));
    st.putEnumerated(stringIDToTypeID('mode'), stringIDToTypeID('blendMode'),
                     stringIDToTypeID('normal'));
    st.putUnitDouble(stringIDToTypeID('opacity'), stringIDToTypeID('percentUnit'), 100);
    st.putUnitDouble(stringIDToTypeID('size'), stringIDToTypeID('pixelsUnit'), value.size);
    st.putObject(stringIDToTypeID('color'), stringIDToTypeID('RGBColor'), rgbObject(value.color));
    return st;
  }
  function gradientDesc(value) {
    var gr = new ActionDescriptor();
    gr.putBoolean(stringIDToTypeID('enabled'), true);
    gr.putBoolean(stringIDToTypeID('present'), true);
    gr.putBoolean(stringIDToTypeID('showInDialog'), true);
    gr.putEnumerated(stringIDToTypeID('mode'), stringIDToTypeID('blendMode'), stringIDToTypeID('normal'));
    gr.putUnitDouble(stringIDToTypeID('opacity'), stringIDToTypeID('percentUnit'), 100);
    gr.putObject(stringIDToTypeID('gradient'), stringIDToTypeID('gradientClassEvent'),
                 gradientObject(value));
    gr.putUnitDouble(stringIDToTypeID('angle'), stringIDToTypeID('angleUnit'), value.angle);
    gr.putEnumerated(stringIDToTypeID('type'), stringIDToTypeID('gradientType'),
                     stringIDToTypeID(value.type));
    gr.putBoolean(stringIDToTypeID('reverse'), false);
    gr.putBoolean(stringIDToTypeID('dither'), true);
    gr.putBoolean(stringIDToTypeID('align'), true);
    gr.putUnitDouble(stringIDToTypeID('scale'), stringIDToTypeID('percentUnit'), 100);
    return gr;
  }
  function shadowDesc(value) {
    var sh = new ActionDescriptor();
    sh.putBoolean(stringIDToTypeID('enabled'), true);
    sh.putBoolean(stringIDToTypeID('present'), true);
    sh.putBoolean(stringIDToTypeID('showInDialog'), true);
    sh.putEnumerated(stringIDToTypeID('mode'), stringIDToTypeID('blendMode'), stringIDToTypeID('multiply'));
    sh.putObject(stringIDToTypeID('color'), stringIDToTypeID('RGBColor'), rgbObject(value.color));
    sh.putUnitDouble(stringIDToTypeID('opacity'), stringIDToTypeID('percentUnit'), value.opacity);
    sh.putBoolean(stringIDToTypeID('useGlobalAngle'), false);
    var angle = Math.atan2(value.y, value.x) * 180 / Math.PI;
    var distance = Math.sqrt(value.x * value.x + value.y * value.y);
    sh.putUnitDouble(stringIDToTypeID('localLightingAngle'), stringIDToTypeID('angleUnit'), angle);
    sh.putUnitDouble(stringIDToTypeID('distance'), stringIDToTypeID('pixelsUnit'), distance);
    sh.putUnitDouble(stringIDToTypeID('chokeMatte'), stringIDToTypeID('pixelsUnit'), 0);
    sh.putUnitDouble(stringIDToTypeID('blur'), stringIDToTypeID('pixelsUnit'), value.blur);
    return sh;
  }
  function putMany(singleKey, multiKey, classKey, values, factory) {
    if (!values.length) return;
    if (values.length == 1) fx.putObject(stringIDToTypeID(singleKey), stringIDToTypeID(classKey), factory(values[0]));
    else {
      var list = new ActionList();
      for (var i = 0; i < values.length; i++) list.putObject(stringIDToTypeID(classKey), factory(values[i]));
      fx.putList(stringIDToTypeID(multiKey), list);
    }
  }
  putMany('frameFX', 'frameFXMulti', 'frameFX', strokes, strokeDesc);
  putMany('gradientFill', 'gradientFillMulti', 'gradientFill', gradients, gradientDesc);
  putMany('dropShadow', 'dropShadowMulti', 'dropShadow', shadows, shadowDesc);
  var set = new ActionDescriptor(), ref = new ActionReference();
  ref.putProperty(stringIDToTypeID('property'), stringIDToTypeID('layerEffects'));
  ref.putEnumerated(stringIDToTypeID('layer'), stringIDToTypeID('ordinal'),
                    stringIDToTypeID('targetEnum'));
  set.putReference(stringIDToTypeID('null'), ref);
  set.putObject(stringIDToTypeID('to'), stringIDToTypeID('layerEffects'), fx);
  executeAction(stringIDToTypeID('set'), set, DialogModes.NO);
}

function applyBlur(r) {
  if (!r.blur) return;
  // Фильтр по обычному текстовому слою требует растрирования. Смарт-объект
  // сохраняет исходный текст внутри и превращает размытие в Smart Filter.
  executeAction(stringIDToTypeID('newPlacedLayer'), undefined, DialogModes.NO);
  var layer = doc.activeLayer;
  layer.name = r.id;
  if (r.blur.type == 'gaussian') layer.applyGaussianBlur(r.blur.radius);
  else if (r.blur.type == 'motion') layer.applyMotionBlur(r.blur.angle, r.blur.distance);
  else if (r.blur.type == 'radial') {
    var method = r.blur.method == 'zoom' ? RadialBlurMethod.ZOOM : RadialBlurMethod.SPIN;
    var quality = r.blur.quality == 'best' ? RadialBlurQuality.BEST
      : r.blur.quality == 'draft' ? RadialBlurQuality.DRAFT : RadialBlurQuality.GOOD;
    layer.applyRadialBlur(r.blur.amount, method, quality);
  }
}

// Подгон кегля. Ключевой момент: у абзацного текста лишнее просто
// скрывается, и bounds покажет высоту рамки, а не текста. Поэтому меряем
// в заведомо высокой рамке, и только потом сажаем в настоящую.
//
// Ширину проверяем наравне с высотой, и это не перестраховка: слово, которое
// не влезает в строку целиком и не переносится, Photoshop не ужимает, а
// выносит за рамку. По высоте всё сходится, а на странице текст лежит поверх
// контура балуна. В русском такие слова длиннее и встречаются чаще.
//
// Но сравнивать впритык нельзя: у выключенного по центру абзаца габарит
// глифов и так на пару пикселей гуляет вокруг рамки от кернинга и округления.
// Строгое сравнение заваливало подгон на ровном месте и гнало кегль в минимум,
// поэтому допуск — доля кегля: торчащее слово шире него на порядок.
function fitText(tl, boxW, boxH, maxSize, minSize, fixedSize, fixedLead) {
  var ti = tl.textItem;
  ti.width = boxW * K;
  ti.height = boxH * 6 * K;
  if (fixedSize > 0) {
    ti.size = fixedSize * K;
    ti.leading = (fixedLead > 0 ? fixedLead : Math.round(fixedSize * 1.18)) * K;
    var fb = tl.bounds;
    var fth = parseFloat(fb[3]) - parseFloat(fb[1]);
    var ftw = parseFloat(fb[2]) - parseFloat(fb[0]);
    return {size: fixedSize, textH: (fth <= boxH && ftw <= boxW + Math.max(2, Math.round(fixedSize * 0.3))) ? fth : -1};
  }
  for (var s = maxSize; s >= minSize; s--) {
    ti.size = s * K;
    ti.leading = (fixedLead > 0 ? fixedLead : Math.round(s * 1.18)) * K;
    var b = tl.bounds;
    var th = parseFloat(b[3]) - parseFloat(b[1]);
    var tw = parseFloat(b[2]) - parseFloat(b[0]);
    if (th <= boxH && tw <= boxW + Math.max(2, Math.round(s * 0.3))) {
      return { size: s, textH: th };
    }
  }
  ti.size = minSize * K;
  ti.leading = (fixedLead > 0 ? fixedLead : Math.round(minSize * 1.18)) * K;
  return { size: minSize, textH: -1 };
}
"""

FOOTER = """
step('save_psd', function () {
  var o = new PhotoshopSaveOptions(); o.layers = true;
  doc.saveAs(new File(PSD), o, true, Extension.LOWERCASE);
  return PSD;
});
step('export_png', function () {
  var o = new PNGSaveOptions();
  doc.saveAs(new File(PNG), o, true, Extension.LOWERCASE);
  return PNG;
});
step('close', function () { doc.close(SaveOptions.DONOTSAVECHANGES); return 'closed'; });
writeFile(REPORT, '[' + R.join(',') + ']');
})();
"DONE";
"""


def build(src_img: str, psd_out: str, png_out: str, report_out: str,
          regions: List[Dict[str, Any]], font: str,
          min_size: int = 9, erase_only: bool = False,
          lang: str = "russianLanguage",
          erase_ids: List[str] = None) -> str:
    """Собирает полный .jsx: стереть что осталось, затем сверстать переводы.

    lang — идентификатор языка Photoshop целиком ("russianLanguage"), а не
    короткий код: его даёт translate.TARGETS[...]["ps_lang"], а bridge.render
    переводит один в другой. На неизвестный идентификатор Photoshop не
    ругается, поэтому фактический язык возвращает languageOf() и он попадает
    в отчёт шагом typeset_all.

    erase_ids — список регионов, которые Photoshop должен стереть сам.
    Обычно стирание уже сделано в контейнере, и приходит пустой список:
    тогда проход стирания не выполняется вовсе. None означает старый
    порядок — стирать здесь всё, — и остаётся на случай, когда стереть в
    контейнере не вышло.
    """
    parts = [HEADER]
    parts.append("var SRC = %s, PSD = %s, PNG = %s, REPORT = %s, FONT = %s;"
                 % (esc(src_img), esc(psd_out), esc(png_out), esc(report_out), esc(font)))
    parts.append("var FONT_OK = {}, ERASE_ALL = %s, LANG = %s;"
                 % ("true" if erase_only else "false", esc(lang)))
    parts.append("var ERASE_IDS = %s;"
                 % ("null" if erase_ids is None
                    else "[" + ",".join(esc(i) for i in erase_ids) + "]"))
    parts.append("var REGIONS = [" + ",".join(_region_literal(r) for r in regions) + "];")
    parts.append("""
var doc = null;
step('setup', function () {
  app.preferences.rulerUnits = Units.PIXELS;
  app.preferences.typeUnits = TypeUnits.PIXELS;
  doc = app.open(new File(SRC));
  K = 72.0 / doc.resolution;
  return doc.width + 'x' + doc.height + ' @' + doc.resolution + ' ppi';
});

// Photoshop на неизвестное имя шрифта не ругается, а молча подставляет
// другой: PSD выходит правдоподобным и неправильным одновременно. Поэтому
// имя сверяется со списком установленных до вёрстки, и если шрифта нет,
// текст не ставится вовсе — стирание всё равно останется полезным, а в
// отчёте будет видно, почему страница пустая.
step('font', function () {
  for (var i = 0; i < app.fonts.length; i++) {
    FONT_OK[app.fonts[i].postScriptName] = app.fonts[i].name;
  }
  return app.fonts.length + ' installed; default=' + FONT;
});

// Проход 1: стираем оригинал. Все стирания идут по фоновому слою,
// до того как появятся текстовые слои, — иначе заливка возьмёт их в расчёт.
step('erase_all', function () {
  var done = 0, kept = 0;
  if (ERASE_IDS !== null && ERASE_IDS.length === 0) return 'erased in container';
  doc.activeLayer = doc.layers[doc.layers.length - 1];
  for (var i = 0; i < REGIONS.length; i++) {
    var r = REGIONS[i];
    // Регион без перевода не трогаем: заливка без замены только портит
    // рисунок. Так остаются нетронутыми звуки и мусорные находки.
    if (!ERASE_ALL && !r.forceErase && (!r.txt || r.txt.length === 0)) { kept++; continue; }
    if (ERASE_IDS !== null && !inList(r.id, ERASE_IDS)) { kept++; continue; }
    try {
      doc.selection.select(r.poly);
      // Content-Aware Fill достраивает выделение по остальной странице, а
      // страница в этот момент ещё полна текста — в ровный пузырь он
      // приносит буквы из соседних. Там, где фон ровный, нужна не догадка,
      // а просто его цвет; догадка остаётся для текста поверх рисунка.
      if (r.onArt) { contentAwareFill(); } else { solidFill(r.bg); }
      done++;
    } catch (e) { R.push('{"step":"erase:' + r.id + '","ok":false,"info":' + jstr(e) + '}'); }
  }
  try { doc.selection.deselect(); } catch (e) {}
  return done + ' erased, ' + kept + ' kept of ' + REGIONS.length;
});
""")

    if not erase_only:
        parts.append("""
// Проход 2: вёрстка переводов.
step('typeset_all', function () {
  var placed = 0, overflow = [], lang = '';
  for (var i = 0; i < REGIONS.length; i++) {
    var r = REGIONS[i];
    if (!r.txt || r.txt.length === 0) continue;
    try {
      var tl = doc.artLayers.add();
      tl.kind = LayerKind.TEXT;
      tl.name = r.id;
      var ti = tl.textItem;
      ti.kind = TextType.PARAGRAPHTEXT;
      ti.contents = r.txt;
      var chosenFont = r.font || FONT;
      if (!FONT_OK[chosenFont]) throw new Error('font not installed: ' + chosenFont);
      ti.font = chosenFont;
      try { ti.fauxBold = r.bold; } catch (e) {}
      try { ti.fauxItalic = r.italic; } catch (e) {}
      try { ti.underline = r.underline ? UnderlineType.UNDERLINELEFT : UnderlineType.UNDERLINEOFF; }
      catch (e) {}
      // Реплика центруется, список — нет: у содержания и титров левый край
      // ровный, и переносить его в центр значит разъехаться с линейками.
      ti.justification = r.align == 'left' ? Justification.LEFT
        : r.align == 'right' ? Justification.RIGHT
        : r.align == 'center' ? Justification.CENTER
        : (r.keep ? Justification.LEFT : Justification.CENTER);
      ti.hyphenation = !r.keep;
      try { setLanguage(LANG); if (!lang) lang = languageOf(); }
      catch (e) { if (!lang) lang = 'failed: ' + e; }
      var col = new SolidColor();
      col.rgb.red = r.fg[0]; col.rgb.green = r.fg[1]; col.rgb.blue = r.fg[2];
      ti.color = col;
      ti.position = [r.sx, r.sy];

      var startSize = Math.max(MIN_SIZE + 1, Math.round(r.size * 1.1));
      var fit = fitText(tl, r.sw, r.sh, startSize, MIN_SIZE, r.fixedSize, r.fixedLead);
      if (fit.textH < 0) overflow.push(r.id);

      // Ставим настоящую рамку и центрируем текст по вертикали.
      ti.height = r.sh * K;
      var b = tl.bounds;
      var th = parseFloat(b[3]) - parseFloat(b[1]);
      var dy = r.keep ? 0 : Math.max(0, Math.round((r.sh - th) / 2));
      ti.position = [r.sx, r.sy + dy];
      applyBlur(r);
      applyLayerEffects(r);
      placed++;
    } catch (e) { R.push('{"step":"text:' + r.id + '","ok":false,"info":' + jstr(e) + '}'); }
  }
  return placed + ' placed; lang=' + (lang || 'none')
       + '; overflow=' + (overflow.length ? overflow.join(',') : 'none');
});
""")
    parts.insert(1, "var MIN_SIZE = %d;" % min_size)
    parts.append(FOOTER)
    return "\n".join(parts)

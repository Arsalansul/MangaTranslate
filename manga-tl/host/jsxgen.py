"""Генератор ExtendScript для Photoshop.

Почему генерация текста, а не нормальный API: в ExtendScript нет ни JSON,
ни надёжной работы с не-ASCII в исходнике. Поэтому весь скрипт собирается
здесь, кириллица уходит u-экранированной, а данные вшиваются
литералами прямо в код.

Твёрдое правило: в генерируемом JS не должно быть ни одного литерального
обратного слэша — они теряются по дороге через COM. Все спецсимволы
строим через String.fromCharCode. Проверено на грабли.
"""
from typing import Dict, List, Any

BS = chr(92)


def esc(s: str) -> str:
    """Строка -> ASCII-безопасный JS-литерал в кавычках."""
    out = ['"']
    for ch in str(s):
        k = ord(ch)
        if ch == '"':
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
    return "".join(out)


def _region_literal(r: Dict[str, Any]) -> str:
    x, y, w, h = r["bbox"]
    poly = r.get("mask_poly") or [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
    poly_js = "[" + ",".join("[%d,%d]" % (p[0], p[1]) for p in poly) + "]"
    fg = r.get("fg") or [0, 0, 0]
    return (
        "{id:%s,x:%d,y:%d,w:%d,h:%d,poly:%s,txt:%s,size:%d,lead:%d,"
        "kind:%s,onArt:%s,fg:[%d,%d,%d]}"
        % (
            esc(r["id"]), x, y, w, h, poly_js,
            esc(r.get("translation") or ""),
            int(r.get("font_px") or 24),
            int(r.get("line_h_px") or 0),
            esc(r.get("kind") or "unknown"),
            "true" if r.get("on_art") else "false",
            fg[0], fg[1], fg[2],
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
function step(name, fn) {
  try { var v = fn(); R.push('{"step":' + jstr(name) + ',"ok":true,"info":' + jstr(v) + '}'); return v; }
  catch (e) { R.push('{"step":' + jstr(name) + ',"ok":false,"info":' + jstr(e) + '}'); return null; }
}
function writeFile(p, txt) { var f = new File(p); f.encoding = 'UTF-8'; f.open('w'); f.write(txt); f.close(); }

function contentAwareFill() {
  var d = new ActionDescriptor();
  d.putEnumerated(charIDToTypeID('Usng'), charIDToTypeID('FlCn'), stringIDToTypeID('contentAware'));
  d.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('#Prc'), 100.0);
  d.putEnumerated(charIDToTypeID('Md  '), charIDToTypeID('BlnM'), charIDToTypeID('Nrml'));
  executeAction(charIDToTypeID('Fl  '), d, DialogModes.NO);
}

// Подгон кегля. Ключевой момент: у абзацного текста лишнее просто
// скрывается, и bounds покажет высоту рамки, а не текста. Поэтому меряем
// в заведомо высокой рамке, и только потом сажаем в настоящую.
function fitText(tl, boxW, boxH, maxSize, minSize) {
  var ti = tl.textItem;
  ti.width = boxW;
  ti.height = boxH * 6;
  for (var s = maxSize; s >= minSize; s--) {
    ti.size = s;
    ti.leading = Math.round(s * 1.18);
    var b = tl.bounds;
    var th = parseFloat(b[3]) - parseFloat(b[1]);
    if (th <= boxH) return { size: s, textH: th };
  }
  ti.size = minSize;
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
          min_size: int = 9, erase_only: bool = False) -> str:
    """Собирает полный .jsx: стереть все регионы, затем сверстать переводы."""
    parts = [HEADER]
    parts.append("var SRC = %s, PSD = %s, PNG = %s, REPORT = %s, FONT = %s;"
                 % (esc(src_img), esc(psd_out), esc(png_out), esc(report_out), esc(font)))
    parts.append("var FONT_OK = false;")
    parts.append("var REGIONS = [" + ",".join(_region_literal(r) for r in regions) + "];")
    parts.append("""
var doc = null;
step('setup', function () {
  app.preferences.rulerUnits = Units.PIXELS;
  app.preferences.typeUnits = TypeUnits.PIXELS;
  doc = app.open(new File(SRC));
  return doc.width + 'x' + doc.height;
});

// Photoshop на неизвестное имя шрифта не ругается, а молча подставляет
// другой: PSD выходит правдоподобным и неправильным одновременно. Поэтому
// имя сверяется со списком установленных до вёрстки, и если шрифта нет,
// текст не ставится вовсе — стирание всё равно останется полезным, а в
// отчёте будет видно, почему страница пустая.
step('font', function () {
  for (var i = 0; i < app.fonts.length; i++) {
    if (app.fonts[i].postScriptName == FONT) {
      FONT_OK = true;
      return FONT + ' = ' + app.fonts[i].name;
    }
  }
  throw new Error('font not installed: ' + FONT);
});

// Проход 1: стираем оригинал. Все стирания идут по фоновому слою,
// до того как появятся текстовые слои, — иначе заливка возьмёт их в расчёт.
step('erase_all', function () {
  var done = 0;
  doc.activeLayer = doc.layers[doc.layers.length - 1];
  for (var i = 0; i < REGIONS.length; i++) {
    var r = REGIONS[i];
    try {
      doc.selection.select(r.poly);
      contentAwareFill();
      done++;
    } catch (e) { R.push('{"step":"erase:' + r.id + '","ok":false,"info":' + jstr(e) + '}'); }
  }
  try { doc.selection.deselect(); } catch (e) {}
  return done + '/' + REGIONS.length + ' erased';
});
""")

    if not erase_only:
        parts.append("""
// Проход 2: вёрстка переводов.
step('typeset_all', function () {
  if (!FONT_OK) return 'skipped: font not installed';
  var placed = 0, overflow = [];
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
      ti.font = FONT;
      ti.justification = Justification.CENTER;
      ti.hyphenation = true;
      var col = new SolidColor();
      col.rgb.red = r.fg[0]; col.rgb.green = r.fg[1]; col.rgb.blue = r.fg[2];
      ti.color = col;
      ti.position = [r.x, r.y];

      var startSize = Math.max(MIN_SIZE + 1, Math.round(r.size * 1.25));
      var fit = fitText(tl, r.w, r.h, startSize, MIN_SIZE);
      if (fit.textH < 0) overflow.push(r.id);

      // Ставим настоящую рамку и центрируем текст по вертикали.
      ti.height = r.h;
      var b = tl.bounds;
      var th = parseFloat(b[3]) - parseFloat(b[1]);
      var dy = Math.max(0, Math.round((r.h - th) / 2));
      ti.position = [r.x, r.y + dy];
      placed++;
    } catch (e) { R.push('{"step":"text:' + r.id + '","ok":false,"info":' + jstr(e) + '}'); }
  }
  return placed + ' placed; overflow=' + (overflow.length ? overflow.join(',') : 'none');
});
""")
    parts.insert(1, "var MIN_SIZE = %d;" % min_size)
    parts.append(FOOTER)
    return "\n".join(parts)

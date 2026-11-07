#!/usr/bin/env python3
"""Panel de luz para Kindle: descarga los precios PVPC de REE y dibuja las pantallas.

Para cada hora de hoy (y de mañana, si REE ya los ha publicado) genera una imagen
1072x1448 en gris para un Kindle Paperwhite, las empaqueta en paquete.tar y crea
una página de vista previa. Lo ejecuta GitHub Actions cada tarde.

Uso:
    python generar.py --salida _site
    python generar.py --salida _site --ahora 2026-10-29T18:30 --datos-prueba pruebas/
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

VERSION = "7.4"
# Hueco para el indicador de batería que pinta el Kindle encima (mismas cifras en comun.sh)
BAT_X, BAT_Y, BAT_W, BAT_H = 892, 40, 120, 52  # súbelo en cada cambio del generador
COMMIT = (os.environ.get("GITHUB_SHA") or "local")[:7]
MADRID = ZoneInfo("Europe/Madrid")
W, H = 1072, 1448

# Impuestos aplicados para mostrar un precio parecido al que pagas.
# IEE: impuesto especial sobre la electricidad. IVA: el vigente para la luz.
IEE = float(os.environ.get("IEE", "0.0511269632"))
IVA = float(os.environ.get("IVA", "0.21"))
FACTOR_IMPUESTOS = (1 + IEE) * (1 + IVA)

URL_ARCHIVO = "https://api.esios.ree.es/archives/70/download_json?locale=es&date={d:%Y-%m-%d}"
URL_REDATA = ("https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
              "?start_date={d:%Y-%m-%d}T00:00&end_date={d:%Y-%m-%d}T23:59&time_trunc=hour")

# Periodos de la 2.0TD (laborables); fines de semana y festivos nacionales: todo valle
LAB = ["V"] * 8 + ["L"] * 2 + ["P"] * 4 + ["L"] * 4 + ["P"] * 4 + ["L"] * 2
FESTIVOS = {(1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25)}
NOMBRE_PERIODO = {"P": "punta", "L": "llano", "V": "valle"}
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def periodo(d: date, h: int) -> str:
    if d.weekday() >= 5 or (d.month, d.day) in FESTIVOS:
        return "V"
    return LAB[h]


# --------------------------------------------------------------------------------------
# Descarga y lectura de precios
# --------------------------------------------------------------------------------------

def _num(v) -> float:
    return float(str(v).replace(".", "").replace(",", ".")) if isinstance(v, str) and "," in v else float(v)


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "panel-luz-kindle/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def leer_archivo_ree(data: dict, d: date) -> dict[int, float]:
    """Fichero público diario de PVPC (archivo 70). Valores en €/MWh, campo PCB (península).

    Admite datos horarios (23-25 filas) y cuartohorarios (92-100 filas), que se
    promedian por hora."""
    filas = data["PVPC"]
    dia = datetime.strptime(filas[0]["Dia"], "%d/%m/%Y").date()
    if dia != d:
        raise ValueError(f"el fichero es del {dia}, no del {d}")
    por_hora: dict[int, list[float]] = {}
    cuartos = len(filas) > 30
    for i, f in enumerate(filas):
        m = re.match(r"\s*(\d{1,2})", str(f.get("Hora", "")))
        h = int(m.group(1)) if m else (i // 4 if cuartos else i)
        por_hora.setdefault(h, []).append(_num(f["PCB"]))
    return {h: mean(v) / 1000 for h, v in por_hora.items()}


def leer_redata(data: dict, d: date) -> dict[int, float]:
    """API REData de REE (respaldo). Valores en €/MWh."""
    for inc in data.get("included", []):
        titulo = (inc.get("type") or "") + " " + (inc.get("attributes", {}).get("title") or "")
        if "PVPC" not in titulo.upper():
            continue
        por_hora: dict[int, list[float]] = {}
        for v in inc["attributes"]["values"]:
            t = datetime.fromisoformat(v["datetime"]).astimezone(MADRID)
            if t.date() == d:
                por_hora.setdefault(t.hour, []).append(float(v["value"]))
        if por_hora:
            return {h: mean(v) / 1000 for h, v in por_hora.items()}
    raise ValueError("no hay serie PVPC en la respuesta")


def validar(p: dict[int, float]) -> dict[int, float]:
    if len(p) < 23 or not all(-0.5 < v < 2.0 for v in p.values()):
        raise ValueError(f"datos raros: {len(p)} horas, rango {min(p.values()):.3f}-{max(p.values()):.3f}")
    return p


def precios_dia(d: date, carpeta_prueba: Path | None) -> dict[int, float] | None:
    """Precios del día d en €/kWh SIN impuestos, por hora local. None si no hay."""
    intentos = []
    if carpeta_prueba:
        for nombre, lector in ((f"archivo70_{d}.json", leer_archivo_ree), (f"redata_{d}.json", leer_redata)):
            f = carpeta_prueba / nombre
            if f.exists():
                intentos.append((nombre, lambda f=f, l=lector: validar(l(json.loads(f.read_text()), d))))
    else:
        intentos = [
            ("REE archivo 70", lambda: validar(leer_archivo_ree(_get_json(URL_ARCHIVO.format(d=d)), d))),
            ("REE REData", lambda: validar(leer_redata(_get_json(URL_REDATA.format(d=d)), d))),
        ]
    for nombre, fn in intentos:
        try:
            p = fn()
            print(f"  {d}: {len(p)} horas desde {nombre}")
            return p
        except Exception as e:  # noqa: BLE001
            print(f"  {d}: {nombre} falló ({e})")
    return None


# --------------------------------------------------------------------------------------
# Dibujo
# --------------------------------------------------------------------------------------

def euros(v: float, dec: int = 3) -> str:
    return f"{v:.{dec}f}".replace(".", ",")


def clase(v: float, ref: list[float]) -> str:
    s = sorted(ref)
    t1, t2 = s[len(s) // 3], s[(2 * len(s)) // 3]
    return "barata" if v < t1 else ("cara" if v >= t2 else "normal")


def mejor_franja(serie: list[tuple[date, int, float]], horas: int = 2):
    """Mejor bloque de `horas` seguidas en la serie (fecha, hora, precio)."""
    mejor = None
    for i in range(len(serie) - horas + 1):
        bloque = serie[i:i + horas]
        m = mean(x[2] for x in bloque)
        if mejor is None or m < mejor[0]:
            mejor = (m, bloque[0][0], bloque[0][1], bloque[-1][1] + 1)
    return mejor


def escala_comun(*dias: dict[int, float] | None) -> float:
    """Máximo común (redondeado a 0,05 €) para que varios gráficos sean comparables."""
    vals = [v for d in dias if d for v in d.values()]
    top = max(max(vals), 0.05)
    return (int(top / 0.05) + 1) * 0.05


def grafico(precios: dict[int, float], d: date, marca: int | None, alto: int, etiquetas=True,
            franja: tuple[int, int] | None = None, vmax: float | None = None,
            ref: list[float] | None = None) -> str:
    """Barras horarias. vmax y ref (valores para los umbrales de color) se pueden compartir
    entre gráficos para que la altura y el tono signifiquen lo mismo en todos."""
    vals = list(precios.values())
    vmax = vmax or escala_comun(precios)
    ref = ref or vals
    barras = []
    for h in range(24):
        if h not in precios:
            barras.append('<div class="b vacia"></div>')
            continue
        v = precios[h]
        alto_b = max(4, int(alto * max(v, 0) / vmax))
        c = clase(v, ref)
        mk = '<div class="mk"></div>' if h == marca else ""
        barras.append(f'<div class="b"><div class="r {c}{" ya" if marca is not None and h < marca else ""}" '
                      f'style="height:{alto_b}px"></div>{mk}</div>')
    # Líneas de referencia cada 0,10 €/kWh, iguales en todos los gráficos
    lineas = ""
    paso = 0.10 if vmax > 0.25 else 0.05
    g = paso
    while g < vmax - 0.001:
        y = int(alto * g / vmax)
        lineas += (f'<div class="rej" style="bottom:{y}px"></div>'
                   f'<div class="rejl" style="bottom:{y}px">{euros(g, 2)}</div>')
        g += paso
    eje = ""
    if etiquetas:
        eje = '<div class="eje">' + "".join(f'<span style="left:{h / 24 * 100:.3f}%">{h}</span>'
                                            for h in range(0, 25, 3)) + "</div>"
    extra = "margin-top:52px;" if marca is not None else ""
    banda = ""
    if franja:
        a, b = franja
        banda = (f'<div class="banda" style="left:{a / 24 * 100:.3f}%;width:{(b - a) / 24 * 100:.3f}%">'
                 f'</div>')
    return (f'<div class="graf" style="height:{alto}px;{extra}">{lineas}{"".join(barras)}{banda}</div>'
            f'{eje if not franja else eje.replace("class=\"eje\"", "class=\"eje con-banda\"")}')


CSS = """
@font-face { font-family: Inter; src: local('Inter'); }
* { margin:0; padding:0; box-sizing:border-box; }
html,body { width:1072px; height:1448px; background:#fff; color:#000; font-family:Inter,'DejaVu Sans',sans-serif; }
body { padding:48px 60px; position:relative; }
.top { display:flex; justify-content:space-between; align-items:baseline; font-size:36px; font-weight:600; }
.top .bat { width:144px; }
.top b { font-weight:800; }
.big { margin-top:20px; border:10px solid #000; height:290px; display:flex; flex-direction:column;
  align-items:center; justify-content:center; }
.big .v { font-size:140px; font-weight:900; line-height:1; letter-spacing:-2px; }
.big .v small { font-size:64px; font-weight:700; letter-spacing:0; }
.big .c { font-size:46px; font-weight:700; margin-top:14px; letter-spacing:1px; }
.big.cara { background:#000; color:#fff; } .big.normal { background:#b8b8b8; } .big.barata { background:#fff; }
.sub { margin-top:22px; display:flex; justify-content:space-between; font-size:34px; font-weight:600; }
.sub b { font-weight:800; }
.sec { margin-top:24px; }
.sec h2 { font-size:30px; font-weight:800; text-transform:uppercase; letter-spacing:2px; margin-bottom:8px;
  display:flex; justify-content:space-between; }
.sec h2 span { font-weight:500; text-transform:none; letter-spacing:0; }
.graf { display:flex; align-items:flex-end; gap:5px; border-bottom:4px solid #000; }
.b { flex:1; position:relative; height:100%; display:flex; align-items:flex-end; }
.r { width:100%; border:3px solid #000; border-bottom:none; }
.r.cara { background:#000; } .r.normal { background:#9c9c9c; } .r.barata { background:#fff; }
.r.ya { opacity:.35; }
.mk { position:absolute; top:-46px; left:50%; margin-left:-20px; width:0; height:0;
  border-left:20px solid transparent; border-right:20px solid transparent; border-top:30px solid #000; }
.eje { position:relative; height:38px; margin-top:6px; margin-right:58px; font-size:26px; font-weight:600; }
.eje span { position:absolute; transform:translateX(-50%); }
.eje span:first-child { transform:none; } .eje span:last-child { transform:translateX(-100%); }
.rango { font-size:26px; color:#222; margin-top:2px; }
.tip { margin-top:26px; border:4px solid #000; padding:16px 22px; font-size:31px; font-weight:600; line-height:1.3; }
.tip b { font-weight:800; }
.man-status { height:72px; border:4px solid #000; display:flex; align-items:center; justify-content:space-between;
  padding:0 20px; font-size:28px; font-weight:700; margin-bottom:12px; }
.man-status strong { font-size:31px; font-weight:900; letter-spacing:.5px; }
.man-status.ready { background:#fff; }
.man-status.pending { height:120px; border-style:dashed; margin-bottom:0; }
.man-status .dato { font-size:29px; font-weight:800; }
.nodata { height:120px; border:3px dashed #000; display:flex; align-items:center; justify-content:center;
  font-size:32px; font-weight:600; }
.foot { position:absolute; left:60px; right:60px; bottom:34px; font-size:22px; color:#333;
  display:flex; justify-content:space-between; }
.graf { position:relative; margin-right:58px; }
.rej { position:absolute; left:0; right:0; border-top:2px dashed #8a8a8a; z-index:0; }
.rejl { position:absolute; right:-60px; width:54px; transform:translateY(50%); font-size:22px; font-weight:700; color:#333; text-align:right; }
.b { z-index:1; }
.banda { position:absolute; bottom:-24px; height:16px; background:#000; }
.eje.con-banda { margin-top:30px; }
.prox { margin-top:20px; border:10px solid #000; padding:18px 26px 20px; }
.prox .l { font-size:28px; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; }
.prox .h { font-size:92px; font-weight:900; line-height:1.05; margin-top:6px; white-space:nowrap; }
.prox .h small { font-size:44px; font-weight:800; }
.prox .p { font-size:32px; font-weight:700; margin-top:8px; }
.prox.ya { background:#000; color:#fff; }
.ahora { margin-top:18px; display:flex; align-items:center; justify-content:space-between;
  border:4px solid #000; padding:12px 22px; }
.ahora .v { font-size:56px; font-weight:900; }
.ahora .v small { font-size:30px; font-weight:700; }
.ahora .c { font-size:30px; font-weight:800; letter-spacing:1px; }
.ahora.cara { background:#000; color:#fff; } .ahora.normal { background:#b8b8b8; }
.leyenda { font-size:24px; font-weight:600; margin-top:2px; }
.leyenda i { display:inline-block; width:46px; height:12px; background:#000; vertical-align:middle; margin-right:8px; }
"""


def proxima_franja(d: date, h: int, hoy_i: dict[int, float], man_i: dict[int, float] | None, horas: int = 3):
    """Mejor bloque de `horas` seguidas desde esta hora hasta donde haya datos (hoy y, si hay, mañana)."""
    serie = [(d, k, hoy_i[k]) for k in sorted(hoy_i) if k >= h]
    if man_i:
        serie += [(d + timedelta(days=1), k, man_i[k]) for k in sorted(man_i)]
    n = min(horas, len(serie))
    if n == 0:
        return None
    mejor = None
    for i in range(len(serie) - n + 1):
        bloque = serie[i:i + n]
        m = mean(x[2] for x in bloque)
        if mejor is None or m < mejor[0]:
            mejor = (m, bloque)
    return mejor


def pantalla(d: date, h: int, hoy: dict[int, float], man: dict[int, float] | None, generado: datetime) -> str:
    hoy_i = {k: v * FACTOR_IMPUESTOS for k, v in hoy.items()}
    man_i = {k: v * FACTOR_IMPUESTOS for k, v in man.items()} if man else None
    md = d + timedelta(days=1)
    v = hoy_i[h]
    c = clase(v, list(hoy_i.values()))
    per = NOMBRE_PERIODO[periodo(d, h)]

    pf = proxima_franja(d, h, hoy_i, man_i, 3)
    franja_hoy = franja_man = None
    if pf:
        media, bloque = pf
        (d0, h0, _), (d1, h1, _) = bloque[0], bloque[-1]
        fin = (h1 + 1) % 24
        fin_txt = "24" if (d1 == d0 and h1 == 23) else f"{fin:02d}"
        if d0 == d:
            franja_hoy = (h0, 24 if d1 != d else h1 + 1)
        if d1 == md:
            franja_man = (0 if d0 != md else h0, h1 + 1)
        dia = "hoy" if d0 == d else "mañana"
        if d1 != d0:
            cuando = f"{h0:02d}–{fin:02d} h <small>(cruza medianoche)</small>"
        else:
            cuando = f"{dia} {h0:02d}–{fin_txt} h" if dia == "mañana" else f"{h0:02d}–{fin_txt} h"
        horas_txt = f"{len(bloque)} h" if len(bloque) == 3 else f"{len(bloque)} h, las que quedan con datos"
        if d0 == d and h0 == h:
            acaba = " · acaba mañana" if d1 != d else ""
            prox = (f'<div class="prox ya"><div class="l">Estás en la mejor franja ({horas_txt}){acaba}</div>'
                    f'<div class="h">{"hasta medianoche" if fin == 0 else f"hasta las {fin:02d}:00"}</div>'
                    f'<div class="p">media {euros(media)} €/kWh</div></div>')
        else:
            ahorro = (1 - media / v) * 100
            extra = f" · {ahorro:.0f} % menos que ahora" if ahorro >= 1 else ""
            prox = (f'<div class="prox"><div class="l">Próxima mejor franja ({horas_txt})</div>'
                    f'<div class="h">{cuando}</div>'
                    f'<div class="p">media {euros(media)} €/kWh{extra}</div></div>')
    else:
        prox = ""

    alcance = "hasta mañana a medianoche" if man_i else "hasta medianoche (mañana aún sin precios)"
    vmax = escala_comun(hoy_i, man_i)
    ref = list(hoy_i.values()) + (list(man_i.values()) if man_i else [])
    if man_i:
        man_html = (grafico(man_i, md, None, 250, franja=franja_man, vmax=vmax, ref=ref)
                    + f'<div class="rango">De {euros(min(man_i.values()))} a {euros(max(man_i.values()))} €/kWh</div>')
    else:
        man_html = ('<div class="man-status pending"><strong>PRECIOS DE MAÑANA</strong>'
                    '<span class="dato">Llegan hacia las 21:00</span></div>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<div class="top"><span><b>{DIAS[d.weekday()].capitalize()} {d.day} {MESES[d.month - 1]}</b> · {h:02d}:00–{h + 1:02d}:00</span><span>periodo {per}</span><span class="bat"></span></div>
{prox}
<div class="ahora {c}"><span class="v">Ahora {euros(v)}<small> €/kWh</small></span><span class="c">{c.upper()} PARA HOY</span></div>
<div class="leyenda"><i></i>mejor franja de 3 h, buscando {alcance}</div>
<div class="sec"><h2>Hoy <span>{euros(min(hoy_i.values()))} – {euros(max(hoy_i.values()))} €/kWh</span></h2>{grafico(hoy_i, d, h, 250, franja=franja_hoy, vmax=vmax, ref=ref)}</div>
<div class="sec"><h2>Mañana <span>{DIAS[md.weekday()]} {md.day} {MESES[md.month - 1]}</span></h2>{man_html}</div>
<div class="foot"><span>PVPC 2.0TD · con impuestos (IEE + IVA {int(IVA * 100)} %) · datos REE</span><span>v{VERSION} · {COMMIT} · act. {generado:%d/%m %H:%M}</span></div>
</body></html>"""


CSS_MAN = """
.hd { display:flex; justify-content:space-between; align-items:flex-end; border-bottom:8px solid #000; padding-bottom:14px; }
.hd .t { font-size:96px; font-weight:900; letter-spacing:2px; line-height:1; }
.hd .d { font-size:42px; font-weight:700; }
.franja3 { margin-top:30px; border:8px solid #000; padding:24px 30px 22px; text-align:center; }
.franja3 .l { font-size:30px; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; }
.franja3 .h { font-size:104px; font-weight:900; line-height:1.05; margin-top:8px; }
.franja3 .p { font-size:38px; font-weight:700; margin-top:8px; }
.resumen { margin-top:20px; display:flex; gap:18px; }
.resumen .dato { flex:1; border:4px solid #000; padding:15px 18px; text-align:center; }
.resumen .l { font-size:24px; font-weight:700; text-transform:uppercase; letter-spacing:1px; }
.resumen .n { font-size:46px; font-weight:900; margin-top:5px; }
.cmp { margin-top:18px; font-size:30px; font-weight:700; text-align:center; }
"""


def pantalla_manana(md: date, man: dict[int, float], hoy: dict[int, float] | None, generado: datetime) -> str:
    """Resumen del día siguiente centrado en la mejor franja consecutiva de 3 horas."""
    mi = {k: v * FACTOR_IMPUESTOS for k, v in man.items()}
    media_dia = mean(mi.values())
    serie = [(md, k, mi[k]) for k in sorted(mi)]
    mejor3 = mejor_franja(serie, 3)
    media3, _, h_ini3, h_fin3 = mejor3

    vs_dia = (media3 / media_dia - 1) * 100
    ahorro_dia = f"{abs(vs_dia):.0f} % por debajo de la media del día" if vs_dia < 0 else "similar a la media del día"

    cmp_hoy = ""
    if hoy:
        media_hoy = mean(v * FACTOR_IMPUESTOS for v in hoy.values())
        dif = (media_dia / media_hoy - 1) * 100
        if abs(dif) < 3:
            cmp_hoy = "Mañana será parecido a hoy de media"
        else:
            cmp_hoy = f"Mañana será un {abs(dif):.0f} % más {'caro' if dif > 0 else 'barato'} que hoy de media"

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}{CSS_MAN}</style></head><body>
<div class="hd"><span class="t">MAÑANA</span><span class="d">{DIAS[md.weekday()]} {md.day} {MESES[md.month - 1]}</span></div>
<div class="franja3">
  <div class="l">Mejor franja de 3 h consecutivas</div>
  <div class="h">{h_ini3:02d}–{h_fin3:02d} h</div>
  <div class="p">media {euros(media3)} €/kWh</div>
</div>
<div class="resumen">
  <div class="dato"><div class="l">Franja vs. día</div><div class="n">{ahorro_dia}</div></div>
  <div class="dato"><div class="l">Media del día</div><div class="n">{euros(media_dia)} €/kWh</div></div>
</div>
<div class="cmp">{cmp_hoy}</div>
<div class="sec">{grafico(mi, md, None, 430, franja=(h_ini3, h_fin3))}</div>
<div class="foot"><span>PVPC 2.0TD · con impuestos · datos REE</span><span>v{VERSION} · {COMMIT} · act. {generado:%d/%m %H:%M}</span></div>
</body></html>"""


# Detecta contenido que se sale de la pantalla o que pisa el pie / el hueco de la batería.
COMPROBAR_MAQUETA = """() => {
  const W = 1072, H = 1448, pie = document.querySelector('.foot');
  const tope = pie ? pie.getBoundingClientRect().top : H;
  const bat = {l: %d, t: %d, r: %d, b: %d};
  for (const e of document.body.querySelectorAll('*')) {
    if (pie && (e === pie || pie.contains(e))) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.right > W + 1 || r.bottom > H + 1) return 'se sale: ' + e.className + ' ' + (e.textContent || '').trim().slice(0, 30);
    if (!e.classList.contains('graf') && (e.textContent || '').trim() && e.scrollWidth > e.clientWidth + 2 && e.clientWidth > 0) return 'texto desbordado: ' + (e.textContent || '').trim().slice(0, 30);
    if ((e.textContent || '').trim() && !e.children.length && r.right > W - 54) return 'invade el margen: ' + (e.textContent || '').trim().slice(0, 30);
    if (r.bottom > tope + 1) return 'pisa el pie: ' + e.className + ' ' + (e.textContent || '').trim().slice(0, 30);
    const hoja = !e.children.length && (e.textContent || '').trim();
    if (hoja && r.left < bat.r && r.right > bat.l && r.top < bat.b && r.bottom > bat.t)
      return 'pisa la batería: ' + (e.textContent || '').trim().slice(0, 30);
  }
  return '';
}""" % (BAT_X, BAT_Y, BAT_X + BAT_W, BAT_Y + BAT_H)


def render(paginas: dict[str, str], carpeta: Path):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    carpeta.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception:  # noqa: BLE001
            br = pw.chromium.launch(channel="chrome")
        page = br.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        avisos = 0
        for nombre, html in paginas.items():
            page.set_content(html)
            page.evaluate("document.fonts.ready.then(() => true)")
            fallo = page.evaluate(COMPROBAR_MAQUETA)
            if fallo:
                avisos += 1
                if avisos <= 3:
                    print(f"  AVISO maquetación {nombre}: {fallo}")
            png = page.screenshot()
            Image.open(io.BytesIO(png)).convert("L").save(carpeta / nombre, optimize=True)
        br.close()
    print(f"  maquetación: {len(paginas) - avisos}/{len(paginas)} pantallas sin problemas")


VISTA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Panel luz</title>
<style>:root{{color-scheme:light dark}}body{{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#f4f4f4;color:#111}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#eee}}}}
img{{max-width:100%;width:420px;border:1px solid #888;background:#fff;display:block;margin:12px 0}}
code{{font-size:.9em}}</style></head><body>
<h1>Panel de luz</h1><p>Actualizado: {generado}. Días con precios: {dias}.<br>Generador v{version} · commit {commit}</p>
<p>Ahora el Kindle muestra:</p><img id="i" alt="pantalla actual">
<p>Resumen de mañana:</p><img id="m" alt="resumen de mañana" onerror="this.style.display='none'">
<p><a href="paquete.tar">paquete.tar</a> · <a href="estado.json">estado.json</a></p>
<script>
var d=new Date(),p=function(n){{return(n<10?'0':'')+n}};
var f=new Intl.DateTimeFormat('en-CA',{{timeZone:'Europe/Madrid',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',hourCycle:'h23'}}).formatToParts(d);
var g={{}};f.forEach(function(x){{g[x.type]=x.value}});
var v='?v={marca}';
document.getElementById('i').src='{carpeta}/'+g.year+g.month+g.day+'_'+g.hour+'.png'+v;
var t=new Date(Date.UTC(+g.year,+g.month-1,+g.day+1));
document.getElementById('m').src='{carpeta}/'+t.getUTCFullYear()+p(t.getUTCMonth()+1)+p(t.getUTCDate())+'_manana.png'+v;
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--salida", default="_site")
    ap.add_argument("--ahora", help="fecha/hora de referencia (pruebas), p. ej. 2026-10-29T18:30")
    ap.add_argument("--datos-prueba", help="carpeta con JSON de prueba en lugar de descargar")
    a = ap.parse_args()

    ahora = (datetime.fromisoformat(a.ahora).replace(tzinfo=MADRID) if a.ahora else datetime.now(MADRID))
    prueba = Path(a.datos_prueba) if a.datos_prueba else None
    hoy, man = ahora.date(), ahora.date() + timedelta(days=1)
    print(f"Generando panel ({ahora:%Y-%m-%d %H:%M} Madrid)")

    p_hoy = precios_dia(hoy, prueba)
    p_man = precios_dia(man, prueba)
    if p_hoy is None:
        print("ERROR: no hay precios de hoy; no se publica nada (se mantiene lo anterior).")
        sys.exit(1)

    paginas = {f"{hoy:%Y%m%d}_{h:02d}.png": pantalla(hoy, h, p_hoy, p_man, ahora) for h in sorted(p_hoy)}
    if p_man:
        paginas.update({f"{man:%Y%m%d}_{h:02d}.png": pantalla(man, h, p_man, None, ahora) for h in sorted(p_man)})
        paginas[f"{man:%Y%m%d}_manana.png"] = pantalla_manana(man, p_man, p_hoy, ahora)

    out = Path(a.salida)
    render(paginas, out / "precios")
    (out / "version.txt").write_text(f"generador v{VERSION} {COMMIT} {ahora:%Y-%m-%d %H:%M}\n")
    with tarfile.open(out / "paquete.tar", "w") as tar:
        tar.add(out / "version.txt", arcname="version.txt")
        for nombre in sorted(paginas):
            tar.add(out / "precios" / nombre, arcname=f"precios/{nombre}")
    dias = [str(hoy)] + ([str(man)] if p_man else [])
    estado = {"version": VERSION, "commit": COMMIT,
              "generado": ahora.isoformat(timespec="minutes"), "dias": dias, "imagenes": len(paginas),
              "precios_sin_impuestos_eur_kwh": {str(hoy): p_hoy, **({str(man): p_man} if p_man else {})}}
    (out / "estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=1))
    # Copia de las pantallas en una carpeta única por ejecución: la vista previa nunca sale de caché
    marca = f"{ahora:%Y%m%d%H%M%S}"
    carpeta = f"r/{marca}"
    (out / carpeta).mkdir(parents=True, exist_ok=True)
    for nombre in paginas:
        shutil.copy2(out / "precios" / nombre, out / carpeta / nombre)
    (out / "index.html").write_text(VISTA.format(generado=f"{ahora:%d/%m/%Y %H:%M}", dias=", ".join(dias),
                                                     marca=marca, carpeta=carpeta, version=VERSION, commit=COMMIT))
    (out / ".nojekyll").write_text("")
    print(f"OK: {len(paginas)} pantallas para {', '.join(dias)}")


if __name__ == "__main__":
    main()

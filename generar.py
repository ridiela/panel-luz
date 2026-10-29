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
import sys
import tarfile
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

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


def grafico(precios: dict[int, float], d: date, marca: int | None, alto: int, etiquetas=True) -> str:
    vals = list(precios.values())
    vmax = max(max(vals), 0.01)
    barras = []
    for h in range(24):
        if h not in precios:
            barras.append('<div class="b vacia"></div>')
            continue
        v = precios[h]
        alto_b = max(4, int(alto * max(v, 0) / vmax))
        c = clase(v, vals)
        mk = '<div class="mk"></div>' if h == marca else ""
        barras.append(f'<div class="b"><div class="r {c}{" ya" if marca is not None and h < marca else ""}" '
                      f'style="height:{alto_b}px"></div>{mk}</div>')
    eje = ""
    if etiquetas:
        eje = '<div class="eje">' + "".join(f'<span style="left:{h / 24 * 100:.3f}%">{h}</span>'
                                            for h in range(0, 25, 3)) + "</div>"
    extra = "margin-top:52px;" if marca is not None else ""
    return f'<div class="graf" style="height:{alto}px;{extra}">{"".join(barras)}</div>{eje}'


CSS = """
@font-face { font-family: Inter; src: local('Inter'); }
* { margin:0; padding:0; box-sizing:border-box; }
html,body { width:1072px; height:1448px; background:#fff; color:#000; font-family:Inter,'DejaVu Sans',sans-serif; }
body { padding:48px 60px; position:relative; }
.top { display:flex; justify-content:space-between; align-items:baseline; font-size:36px; font-weight:600; }
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
.eje { position:relative; height:38px; margin-top:6px; font-size:26px; font-weight:600; }
.eje span { position:absolute; transform:translateX(-50%); }
.eje span:first-child { transform:none; } .eje span:last-child { transform:translateX(-100%); }
.rango { font-size:26px; color:#222; margin-top:2px; }
.tip { margin-top:26px; border:4px solid #000; padding:16px 22px; font-size:31px; font-weight:600; line-height:1.3; }
.tip b { font-weight:800; }
.nodata { height:120px; border:3px dashed #000; display:flex; align-items:center; justify-content:center;
  font-size:32px; font-weight:600; }
.foot { position:absolute; left:60px; right:60px; bottom:34px; font-size:22px; color:#333;
  display:flex; justify-content:space-between; }
"""


def pantalla(d: date, h: int, hoy: dict[int, float], man: dict[int, float] | None, generado: datetime) -> str:
    hoy_i = {k: v * FACTOR_IMPUESTOS for k, v in hoy.items()}
    man_i = {k: v * FACTOR_IMPUESTOS for k, v in man.items()} if man else None
    v = hoy_i[h]
    c = clase(v, list(hoy_i.values()))
    sig = hoy_i.get(h + 1) if h + 1 in hoy_i else (man_i or {}).get(0)
    sig_txt = f"Siguiente hora: <b>{euros(sig)} €</b>" if sig is not None else "Siguiente hora: —"
    per = NOMBRE_PERIODO[periodo(d, h)]

    def franja_txt(m):
        return f"<b>{m[2]}–{m[3]} h</b> ({euros(m[0])} €)"

    resto_hoy = [(d, k, hoy_i[k]) for k in sorted(hoy_i) if k >= h]
    mf_hoy = mejor_franja(resto_hoy, 2)
    mf_man = mejor_franja([(d, k, man_i[k]) for k in sorted(man_i)], 2) if man_i else None
    partes = []
    if mf_hoy:
        if mf_hoy[2] == h:
            partes.append(f"<b>Ahora</b> empieza la mejor franja que queda hoy ({euros(mf_hoy[0])} €)")
        else:
            partes.append(f"Hoy: {franja_txt(mf_hoy)}")
    if mf_man:
        partes.append(f"mañana: {franja_txt(mf_man)}")
    tip = ("Para lavadora o lavavajillas (2 h) · " + " · ".join(partes)) if partes else ""

    lo, hi = min(hoy_i.values()), max(hoy_i.values())
    man_html = (grafico(man_i, d + timedelta(days=1), None, 120) +
                f'<div class="rango">De {euros(min(man_i.values()))} a {euros(max(man_i.values()))} €/kWh</div>'
                if man_i else '<div class="nodata">Los precios de mañana salen hacia las 20:30</div>')
    md = d + timedelta(days=1)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<div class="top"><span><b>{DIAS[d.weekday()].capitalize()} {d.day} {MESES[d.month - 1]}</b> · {h}:00–{h + 1}:00</span><span>periodo {per}</span></div>
<div class="big {c}"><div class="v">{euros(v)}<small> €/kWh</small></div><div class="c">{c.upper()} PARA HOY</div></div>
<div class="sub"><span>{sig_txt}</span><span>Hoy: {euros(lo)} – {euros(hi)} €</span></div>
<div class="sec"><h2>Hoy</h2>{grafico(hoy_i, d, h, 250)}</div>
<div class="sec"><h2>Mañana <span>{DIAS[md.weekday()]} {md.day} {MESES[md.month - 1]}</span></h2>{man_html}</div>
<div class="tip">{tip}</div>
<div class="foot"><span>PVPC 2.0TD · con impuestos (IEE + IVA {int(IVA * 100)} %) · datos REE</span><span>act. {generado:%d/%m %H:%M}</span></div>
</body></html>"""


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
        for nombre, html in paginas.items():
            page.set_content(html)
            png = page.screenshot()
            Image.open(io.BytesIO(png)).convert("L").save(carpeta / nombre, optimize=True)
        br.close()


VISTA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Panel luz</title>
<style>:root{{color-scheme:light dark}}body{{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#f4f4f4;color:#111}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#eee}}}}
img{{max-width:100%;width:420px;border:1px solid #888;background:#fff;display:block;margin:12px 0}}
code{{font-size:.9em}}</style></head><body>
<h1>Panel de luz</h1><p>Actualizado: {generado}. Días con precios: {dias}.</p>
<p>Ahora el Kindle muestra:</p><img id="i" alt="pantalla actual">
<p><a href="paquete.tar">paquete.tar</a> · <a href="estado.json">estado.json</a></p>
<script>
var d=new Date(),p=function(n){{return(n<10?'0':'')+n}};
var f=new Intl.DateTimeFormat('en-CA',{{timeZone:'Europe/Madrid',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',hourCycle:'h23'}}).formatToParts(d);
var g={{}};f.forEach(function(x){{g[x.type]=x.value}});
document.getElementById('i').src='precios/'+g.year+g.month+g.day+'_'+g.hour+'.png';
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

    out = Path(a.salida)
    render(paginas, out / "precios")
    with tarfile.open(out / "paquete.tar", "w") as tar:
        for nombre in sorted(paginas):
            tar.add(out / "precios" / nombre, arcname=f"precios/{nombre}")
    dias = [str(hoy)] + ([str(man)] if p_man else [])
    estado = {"generado": ahora.isoformat(timespec="minutes"), "dias": dias, "imagenes": len(paginas),
              "precios_sin_impuestos_eur_kwh": {str(hoy): p_hoy, **({str(man): p_man} if p_man else {})}}
    (out / "estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=1))
    (out / "index.html").write_text(VISTA.format(generado=f"{ahora:%d/%m/%Y %H:%M}", dias=", ".join(dias)))
    (out / ".nojekyll").write_text("")
    print(f"OK: {len(paginas)} pantallas para {', '.join(dias)}")


if __name__ == "__main__":
    main()

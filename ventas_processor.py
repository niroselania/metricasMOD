"""Convierte la planilla de ventas (VTAS S_IVA) en el JSON que usa el tablero.

Por cada mes toma:
  - "DB <MES>": detalle de ventas (A Tienda, B Fecha, G Comprobante, K Descripción,
    L Cantidad, O Monto de ventas).
  - "<MES>": hoja resumen; de las filas "TOTAL - <CANAL>" lee FCT ($) (col. E)
    y FCT (U) (col. H).
"""
import calendar
import datetime as dt
from collections import defaultdict

from openpyxl import load_workbook

CANALES = ["RETIRO", "BARILOCHE", "E-COMMERCE", "MAYORISTA"]
MESES = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6, "JUL": 7,
         "AGO": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DIC": 12}
NOMBRES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
           "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _find_fct_cols(rows):
    """Ubica las columnas de FCT ($) y FCT (U) por su encabezado (el formato cambia entre meses)."""
    for r in rows[:12]:
        heads = [str(v).strip().upper() if v is not None else "" for v in r]
        money = next((i for i, h in enumerate(heads) if h in ("FCT ($)", "FCT VENTA", "FCT VENTAS", "FCT (ARS)")), None)
        units = next((i for i, h in enumerate(heads) if h in ("FCT (U)", "FCT UNIDADES")), None)
        if money is not None and units is not None:
            return money, units
    return 4, 7  # columnas E y H (formato de NOV)


def _read_fct(ws):
    rows = [r for r in ws.iter_rows(min_row=1, max_row=1000, max_col=20, values_only=True)]
    cm, cu = _find_fct_cols(rows)
    fct = {}
    for r in rows:
        label = next((v for v in r[:3] if isinstance(v, str) and v.strip().upper().startswith("TOTAL - ")), None)
        if label and len(r) > max(cm, cu):
            fct[label.strip()[8:].strip().upper()] = {"fct": _num(r[cm]), "fctU": _num(r[cu])}
    return fct


def _read_month(ws_db, ws_sum, code):
    day = defaultdict(lambda: [0.0, 0, set()])
    lin = defaultdict(lambda: [0.0, 0])
    year = None
    month = MESES[code]
    for r in ws_db.iter_rows(min_row=2, max_col=15, values_only=True):
        if len(r) < 15:
            continue
        tienda, fecha = r[0], r[1]
        if not tienda or not isinstance(fecha, (dt.datetime, dt.date)):
            continue
        tienda = str(tienda).strip().upper()
        if tienda not in CANALES or fecha.month != month:
            continue
        year = year or fecha.year
        k = (tienda, fecha.day)
        monto, cant = _num(r[14]), int(_num(r[11]))
        day[k][0] += monto
        day[k][1] += cant
        if r[6]:
            day[k][2].add(str(r[6]).strip())
        lk = (tienda, str(r[10] or "Sin línea").strip())
        lin[lk][0] += monto
        lin[lk][1] += cant
    if year is None:
        return None
    days = calendar.monthrange(year, month)[1]
    fct = _read_fct(ws_sum) if ws_sum is not None else {}
    canales = {}
    for c in CANALES:
        canales[c] = {
            "fct": fct.get(c, {}).get("fct", 0),
            "fctU": fct.get(c, {}).get("fctU", 0),
            "diario": [[round(day[(c, d)][0], 2), day[(c, d)][1], len(day[(c, d)][2])]
                       for d in range(1, days + 1)],
            "lineas": sorted([[k[1], round(v[0], 2), v[1]] for k, v in lin.items() if k[0] == c],
                             key=lambda x: -x[1]),
        }
    return {"mes": f"{NOMBRES[month]} {year}", "year": year, "month": month, "days": days,
            "fuente": f"{ws_db.title.strip()} / {code}", "canales": canales}


def build_payload(path, filename):
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        names = {n.strip().upper(): n for n in wb.sheetnames}
        meses = {}
        for key, real in names.items():
            parts = key.split()
            if len(parts) != 2 or parts[0] != "DB" or parts[1] not in MESES:
                continue
            code = parts[1]
            ws_sum = wb[names[code]] if code in names else None
            m = _read_month(wb[real], ws_sum, code)
            if m:
                meses[f"{m['year']}-{m['month']:02d}"] = m
    finally:
        wb.close()
    if not meses:
        raise ValueError("No encontré pestañas 'DB <MES>' con ventas (por ejemplo 'DB NOV').")
    return {"archivo": filename,
            "actualizado": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "meses": dict(sorted(meses.items()))}

#!/usr/bin/env python3
"""Actualiza data/navs.json con el histórico diario de NAV de cada fondo.

Fuentes, por orden:
  1. Morningstar (librería mstarpy): histórico completo por ISIN, en la divisa
     exacta de la clase que tienes. Se instala sola si falta.
  2. Yahoo Finance (ticker de funds.json o el que resuelva el ISIN): respaldo.
  3. data/manual/<ISIN>.csv: histórico que descargues tú (Investing, Yahoo,
     Morningstar, tu bróker...). Se mezcla y manda sobre lo descargado.

Cada ejecución imprime, por fondo, de dónde salieron los datos, el rango de
fechas y el hueco típico entre datos, para poder comprobar que son diarios.
"""
import csv
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
FUNDS = ROOT / "funds.json"
NAVS = ROOT / "data" / "navs.json"
TICKERS = ROOT / "data" / "tickers.json"
MANUAL = ROOT / "data" / "manual"
UA = {"User-Agent": "Mozilla/5.0 (compatible; cartera-nav/2.0)"}
SCHEMA = 2  # v2: fechas de Yahoo en hora local del mercado (antes iban desplazadas un día)


# ---------------------------------------------------------------- red / Yahoo
def get_json(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:  # no existe: reintentar no sirve
                break
            time.sleep(2 * (i + 1))
        except Exception as e:  # red, 429, JSON inválido...
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url} -> {last}")


def search_symbols(isin):
    """Símbolos que Yahoo asocia a un ISIN, fondos primero."""
    q = urllib.parse.quote(isin)
    data = get_json(
        f"https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=8&newsCount=0"
    )
    quotes = data.get("quotes") or []
    quotes.sort(key=lambda x: 0 if x.get("quoteType") == "MUTUALFUND" else 1)
    return [x["symbol"] for x in quotes if x.get("symbol")]


def parse_chart(payload):
    """{fecha ISO: nav} desde la respuesta v8/chart de Yahoo.

    Yahoo da los instantes en UTC; hay que sumar el desfase del mercado
    (meta.gmtoffset) para obtener el día local real. Sin esto, los fondos que
    Yahoo sella a medianoche local caían en el día anterior (p. ej. domingo).
    """
    res = (payload.get("chart") or {}).get("result")
    if not res:
        raise RuntimeError("respuesta sin datos")
    r = res[0]
    off = int((r.get("meta") or {}).get("gmtoffset") or 0)
    ts = r.get("timestamp") or []
    closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")
        out[d] = round(float(c), 4)
    return out


def fetch_yahoo(symbol):
    s = urllib.parse.quote(symbol)
    payload = get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=max&interval=1d"
    )
    return parse_chart(payload)


def fetch_yahoo_best(isin, hint, cache):
    """Prueba el ticker que ya funcionó, el de funds.json y los que Yahoo asocia
    al ISIN. Devuelve (símbolo, datos) con el primero que sirva."""
    tried, errors = [], []
    candidates = [cache.get(isin), hint]
    try:
        candidates += search_symbols(isin)
    except Exception as e:
        errors.append(f"búsqueda por ISIN: {e}")
    for sym in candidates:
        if not sym or sym in tried:
            continue
        tried.append(sym)
        try:
            data = fetch_yahoo(sym)
            if data:
                cache[isin] = sym
                return sym, data
            errors.append(f"{sym}: sin datos")
        except Exception as e:
            errors.append(f"{sym}: {str(e)[-40:]}")
    raise RuntimeError("Yahoo, probados " + ", ".join(tried or ["ninguno"]) + " -> " + "; ".join(errors[-2:]))


# ------------------------------------------------------------- Morningstar
def _import_mstarpy():
    try:
        from mstarpy import Funds
        return Funds
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "mstarpy"],
            check=False, timeout=300,
        )
        from mstarpy import Funds  # si sigue sin estar, ImportError -> se captura fuera
        return Funds


def fetch_mstar(isin):
    """Histórico diario completo del ISIN desde Morningstar (vía mstarpy)."""
    Funds = _import_mstarpy()
    fund = Funds(term=isin)
    rows, last_err = None, None
    for start, end in ((date(2000, 1, 1), date.today()), ("2000-01-01", date.today().isoformat())):
        try:
            rows = fund.nav(start_date=start, end_date=end, frequency="daily")
            break
        except Exception as e:
            last_err = e
    if rows is None:
        raise RuntimeError(f"nav(): {last_err}")
    out = {}
    for r in rows or []:
        d = str(r.get("date") or r.get("Date") or "")[:10]
        v = r.get("nav", r.get("value", r.get("close")))
        if d and v is not None:
            try:
                out[d] = round(float(v), 4)
            except (TypeError, ValueError):
                pass
    if not out:
        raise RuntimeError("Morningstar no devolvió datos para ese ISIN")
    return out


# ------------------------------------------------------------- CSV manual
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y", "%d.%m.%y")
VALUE_HEADERS = ("nav", "valor liquidativo", "vl", "cierre", "close", "último", "ultimo",
                 "precio", "price", "valor", "value")


def parse_date(s):
    s = s.strip().strip('"').split(" ")[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse_num(s):
    s = s.strip().strip('"').replace("\xa0", "").replace(" ", "").replace("€", "").replace("$", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def read_manual(isin):
    """Lee data/manual/<ISIN>.csv en formatos habituales (Yahoo, Investing,
    Morningstar, hojas de cálculo): separador , ; o tabulador, fechas
    AAAA-MM-DD o DD/MM/AAAA, decimales con punto o coma."""
    f = MANUAL / f"{isin}.csv"
    out = {}
    if not f.exists():
        return out
    text = f.read_text(encoding="utf-8-sig", errors="replace")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return out
    first = lines[0]
    delim = ";" if first.count(";") >= first.count(",") and ";" in first else ("\t" if "\t" in first else ",")
    rows = list(csv.reader(lines, delimiter=delim))
    header = [h.strip().lower() for h in rows[0]]
    col = 1
    if parse_date(rows[0][0]) is None:  # hay cabecera
        for name in VALUE_HEADERS:
            if name in header:
                col = header.index(name)
                break
        rows = rows[1:]
    for row in rows:
        if len(row) <= col:
            continue
        d, v = parse_date(row[0]), parse_num(row[col])
        if d and v is not None:
            out[d] = v
    return out


# ------------------------------------------------------------------ utilidades
def describe(series):
    ds = sorted(series)
    if len(ds) < 2:
        return f"{len(ds)} datos"
    gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(ds, ds[1:])]
    return (f"{ds[0]} → {ds[-1]}, {len(ds)} datos, hueco típico "
            f"{statistics.median(gaps):g} d, máximo {max(gaps)} d")


def main():
    funds = json.loads(FUNDS.read_text(encoding="utf-8"))["funds"]
    navs = json.loads(NAVS.read_text(encoding="utf-8")) if NAVS.exists() else {}
    tickers = json.loads(TICKERS.read_text(encoding="utf-8")) if TICKERS.exists() else {}

    if navs.get("schema") != SCHEMA:
        print("Formato antiguo de datos: se reconstruye el histórico desde cero.")
        navs = {}
    series = {isin: dict(map(tuple, pts)) for isin, pts in navs.get("series", {}).items()}
    sources = dict(navs.get("sources", {}))

    ok, fail, skipped = [], [], []
    for f in funds:
        isin = (f.get("isin") or "").strip()
        name = f["name"]
        if not isin:
            skipped.append(name)
            continue
        base = series.get(isin, {})
        new, src, notes = None, None, []

        try:
            new, src = fetch_mstar(isin), "morningstar"
        except Exception as e:
            notes.append(f"Morningstar: {str(e)[-90:]}")
        if not new or len(new) < 30:
            try:
                sym, new = fetch_yahoo_best(isin, (f.get("ticker") or "").strip(), tickers)
                src = f"yahoo:{sym}"
            except Exception as e:
                notes.append(str(e)[-160:])
                new, src = None, None

        merged = dict(base)
        if new:
            prev = sources.get(isin)
            if prev and prev.split(":")[0] != src.split(":")[0] and base:
                # Cambió de fuente: solo se añaden fechas posteriores para no mezclar cifras.
                last = max(base)
                merged.update({d: v for d, v in new.items() if d > last})
                notes.append(f"fuente distinta a la anterior ({prev}); solo se añaden fechas nuevas")
            else:
                merged.update(new)
                sources[isin] = src
        manual = read_manual(isin)
        if manual:
            merged.update(manual)
            notes.append(f"CSV manual: {len(manual)} datos")

        if new or manual:
            series[isin] = merged
            ok.append(f"{name} [{src or 'manual'}] {describe(merged)}" + ("  | " + "; ".join(notes) if notes else ""))
        else:
            fail.append(f"{name}: " + " | ".join(notes))
        time.sleep(1)

    NAVS.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "schema": SCHEMA,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": sources,
        "series": {isin: sorted(d.items()) for isin, d in series.items()},
    }
    NAVS.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    TICKERS.write_text(json.dumps(tickers, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Actualizados: {len(ok)}")
    for x in ok:
        print("  ok  ", x)
    for x in skipped:
        print("  SIN ISIN:", x)
    for x in fail:
        print("  FALLO:", x)
    if fail and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

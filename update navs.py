#!/usr/bin/env python3
"""Actualiza data/navs.json con el histórico diario de NAV de cada fondo.

Fuente principal: Yahoo Finance (resuelve ISIN -> ticker y baja el histórico).
Respaldo manual: si existe data/manual/<ISIN>.csv (columnas: fecha,nav) se
mezcla con lo descargado; sirve para fondos que Yahoo no cubre.

Solo usa la librería estándar de Python.
"""
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
FUNDS = ROOT / "funds.json"
NAVS = ROOT / "data" / "navs.json"
TICKERS = ROOT / "data" / "tickers.json"
MANUAL = ROOT / "data" / "manual"
UA = {"User-Agent": "Mozilla/5.0 (compatible; cartera-nav/1.0)"}


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


def fetch_best(isin, hint, cache):
    """Prueba el ticker que ya funcionó, el que pones a mano y los que Yahoo
    asocia al ISIN. Devuelve (símbolo, {fecha: nav}) con el primero que sirva."""
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
    raise RuntimeError("probados " + ", ".join(tried or ["ninguno"]) + " -> " + "; ".join(errors[-2:]))


def parse_chart(payload):
    """Devuelve {fecha ISO: nav} a partir de la respuesta v8/chart de Yahoo."""
    res = (payload.get("chart") or {}).get("result")
    if not res:
        raise RuntimeError("respuesta sin datos")
    r = res[0]
    ts = r.get("timestamp") or []
    closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        out[d] = round(float(c), 4)
    return out


def fetch_yahoo(symbol):
    s = urllib.parse.quote(symbol)
    payload = get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=max&interval=1d"
    )
    return parse_chart(payload)


def read_manual(isin):
    f = MANUAL / f"{isin}.csv"
    out = {}
    if not f.exists():
        return out
    with f.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            try:
                d = datetime.strptime(row[0].strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
                out[d] = round(float(row[1].strip().replace(",", ".")), 4)
            except ValueError:
                continue  # cabecera u otra fila no válida
    return out


def main():
    funds = json.loads(FUNDS.read_text(encoding="utf-8"))["funds"]
    navs = json.loads(NAVS.read_text(encoding="utf-8")) if NAVS.exists() else {}
    tickers = json.loads(TICKERS.read_text(encoding="utf-8")) if TICKERS.exists() else {}
    series = {isin: dict(map(tuple, pts)) for isin, pts in navs.get("series", {}).items()}

    ok, fail, skipped = [], [], []
    for f in funds:
        isin = (f.get("isin") or "").strip()
        if not isin:
            skipped.append(f["name"])
            continue
        merged = series.get(isin, {})
        try:
            symbol, data = fetch_best(isin, (f.get("ticker") or "").strip(), tickers)
            merged.update(data)
            ok.append(f"{f['name']} ({symbol}, {len(data)} datos)")
        except Exception as e:
            fail.append(f"{f['name']}: {e}")
        merged.update(read_manual(isin))  # lo manual manda sobre lo descargado
        if merged:
            series[isin] = merged
        time.sleep(1)

    NAVS.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    # Solo falla el job si había fondos con ISIN y ninguno se pudo actualizar.
    if fail and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

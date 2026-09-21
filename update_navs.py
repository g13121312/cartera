#!/usr/bin/env python3
"""Actualiza data/navs.json con el histórico diario de NAV de cada fondo.

Fuentes, por orden:
  1. Morningstar (librería mstarpy): histórico completo y DIARIO por ISIN, en la
     divisa exacta de tu clase. Abre UNA sola sesión de Chrome para todos los fondos.
  2. Yahoo Finance: respaldo. Se prueban todos los tickers candidatos y se elige el
     más diario y reciente (los 0P…F suelen dar solo un dato por semana).
  3. data/manual/<ISIN>.csv: histórico que subas tú. Manda sobre lo descargado.

Reglas de mezcla:
  - Si Morningstar funciona y antes el fondo venía de Yahoo, el histórico se
    reconstruye entero desde Morningstar (más historia y diario).
  - Si Morningstar falla y el fondo ya venía de Morningstar, Yahoo solo añade fechas
    nuevas y solo si sus cifras cuadran con las que ya hay (misma clase/divisa).
  - Solo se reescribe data/navs.json si hay cambios (así el workflow no hace commits
    vacíos y «Datos actualizados» = última vez que llegó un dato nuevo).

Códigos de salida: 0 ok · 1 no se pudo actualizar ningún fondo · 2 algún fondo lleva
más de STALE_DAYS días sin dato nuevo (el workflow lo deja en rojo para que te enteres).
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
FUNDS = ROOT / "funds.json"
NAVS = ROOT / "data" / "navs.json"
TICKERS = ROOT / "data" / "tickers.json"
MANUAL = ROOT / "data" / "manual"
UA = {"User-Agent": "Mozilla/5.0 (compatible; cartera-nav/3.0)"}
SCHEMA = 2  # mismo formato que v2: no se pierde lo ya descargado
MIN_POINTS = 30  # menos datos que esto = respuesta sospechosa
STALE_DAYS = 6  # último dato más viejo que esto = aviso y run en rojo
WANT_CCY = "EUR"  # divisa de tus clases


def short(e, n=220):
    """Primera línea útil del error (las excepciones de Selenium traen la causa
    arriba y una traza enorme abajo; quedarse con el final la esconde)."""
    for line in str(e).splitlines():
        line = line.strip()
        if line and not line.lower().startswith("stacktrace"):
            return line[:n]
    return type(e).__name__


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
        f"https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=10&newsCount=0"
    )
    quotes = data.get("quotes") or []
    quotes.sort(key=lambda x: 0 if x.get("quoteType") == "MUTUALFUND" else 1)
    return [x["symbol"] for x in quotes if x.get("symbol")]


def parse_chart(payload):
    """({fecha ISO: nav}, divisa) desde la respuesta v8/chart de Yahoo.

    Yahoo da los instantes en UTC; hay que sumar el desfase del mercado
    (meta.gmtoffset) para obtener el día local real.
    """
    res = (payload.get("chart") or {}).get("result")
    if not res:
        raise RuntimeError("respuesta sin datos")
    r = res[0]
    meta = r.get("meta") or {}
    off = int(meta.get("gmtoffset") or 0)
    ts = r.get("timestamp") or []
    closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")
        out[d] = round(float(c), 4)
    return out, (meta.get("currency") or "").upper() or None


def fetch_yahoo(symbol):
    s = urllib.parse.quote(symbol)
    payload = get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=max&interval=1d"
    )
    return parse_chart(payload)


def recent_points(series, days=365):
    limit = (date.today() - timedelta(days=days)).isoformat()
    return sum(1 for d in series if d >= limit)


def is_good(series, ccy):
    """Diario (≥150 datos en el último año), reciente y en la divisa buscada."""
    if not series:
        return False
    fresh = (date.today() - date.fromisoformat(max(series))).days <= STALE_DAYS
    return fresh and recent_points(series) >= 150 and ccy in (None, WANT_CCY)


def fetch_yahoo_best(isin, hint, cache):
    """Prueba el ticker guardado, el de funds.json y los que Yahoo asocia al ISIN.
    Se para en el primero que sea diario y reciente; si no, elige el mejor.
    Devuelve (símbolo, datos, notas)."""
    tried, errors, results = [], [], []

    def probe(syms):
        """True en cuanto un candidato es diario, reciente y en la divisa buscada."""
        for sym in syms:
            if not sym or sym in tried:
                continue
            tried.append(sym)
            try:
                data, ccy = fetch_yahoo(sym)
            except Exception as e:
                errors.append(f"{sym}: {short(e, 60)}")
                continue
            if not data:
                errors.append(f"{sym}: sin datos")
                continue
            results.append((sym, data, ccy))
            if is_good(data, ccy):
                return True
        return False

    if not probe([cache.get(isin), hint]):
        try:
            found = search_symbols(isin)
        except Exception as e:
            errors.append(f"búsqueda por ISIN: {short(e, 80)}")
            found = []
        probe(found)
    if not results:
        raise RuntimeError("Yahoo, probados " + (", ".join(tried) or "ninguno") + " -> " + "; ".join(errors[-2:]))

    def key(r):
        sym, data, ccy = r
        return (ccy in (None, WANT_CCY), recent_points(data) >= 150, max(data), recent_points(data), len(data))

    sym, data, ccy = max(results, key=key)
    notes = []
    if ccy not in (None, WANT_CCY):
        notes.append(f"OJO: {sym} cotiza en {ccy}, no en {WANT_CCY}")
    if len(results) > 1:
        notes.append("Yahoo: probados " + ", ".join(r[0] for r in results))
    cache[isin] = sym
    return sym, data, notes


# ------------------------------------------------------------- Morningstar
def _import_mstarpy():
    try:
        import mstarpy
        return mstarpy
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "mstarpy>=11,<12"],
            check=False, timeout=300,
        )
        import mstarpy  # si sigue sin estar, ImportError -> se captura fuera
        return mstarpy


def open_mstar_session():
    """Una sesión (un Chrome) para todos los fondos. Devuelve (sesión, error)."""
    err = None
    for attempt in range(2):
        try:
            mp = _import_mstarpy()
            return mp.MorningstarSession(), None
        except Exception as e:
            err = short(e)
            time.sleep(5)
    return None, err


def fetch_mstar(isin, session):
    """Histórico diario completo del ISIN desde Morningstar (vía mstarpy)."""
    mp = _import_mstarpy()
    fund = mp.Funds(term=isin, session=session)
    rows, last_err = None, None
    for start in (date(2000, 1, 1), date(2010, 1, 1), date(2018, 1, 1)):
        try:
            rows = fund.nav(start_date=start, end_date=date.today(), frequency="daily")
            if rows:
                break
        except Exception as e:
            last_err = e
    if not rows:
        raise RuntimeError(f"nav(): {short(last_err) if last_err else 'respuesta vacía'}")
    out = {}
    for r in rows:
        d = str(r.get("date") or r.get("Date") or r.get("asOfDate") or "")[:10]
        v = r.get("nav", r.get("value", r.get("close")))
        if d and v is not None:
            try:
                out[d] = round(float(v), 4)
            except (TypeError, ValueError):
                pass
    if not out:
        raise RuntimeError(f"no se entiende la respuesta (claves: {sorted(rows[0])[:6]})")
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


# ------------------------------------------------------------------ mezcla
def agrees(base, new):
    """¿Las cifras de `new` cuadran con las de `base` en las fechas comunes?
    Detecta clases o divisas distintas antes de mezclar dos fuentes."""
    common = sorted(set(base) & set(new))[-60:]
    if len(common) < 3:
        return False, f"solo {len(common)} fechas en común"
    diffs = [abs(new[d] / base[d] - 1) for d in common if base[d]]
    med = statistics.median(diffs)
    return med < 0.015, f"diferencia mediana {med:.1%}"


def combine(base, prev_src, new, src, notes):
    """Devuelve (serie, fuente) según las reglas de mezcla."""
    if not new:
        return base, prev_src
    if not base:
        return dict(new), src
    fam, pfam = src.split(":")[0], (prev_src or "").split(":")[0]
    if pfam == fam:  # misma fuente: la descarga nueva manda, se conserva lo antiguo que ya no venga
        merged = dict(base)
        merged.update(new)
        return merged, src
    if fam == "morningstar":  # mejora de fuente: se reconstruye entero
        notes.append(f"histórico reconstruido desde Morningstar (antes {prev_src or 'manual'})")
        return dict(new), src
    ok, why = agrees(base, new)  # bajada de fuente: solo si cuadra
    if ok:
        last = max(base)
        merged = dict(base)
        merged.update({d: v for d, v in new.items() if d > last})
        notes.append(f"Morningstar no disponible: añadidas fechas nuevas de Yahoo ({why})")
        return merged, prev_src
    notes.append(f"Yahoo no cuadra con {prev_src} ({why}): no se mezcla")
    return base, prev_src


# ------------------------------------------------------------------ utilidades
def describe(series):
    ds = sorted(series)
    if len(ds) < 2:
        return f"{len(ds)} datos"
    gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(ds, ds[1:])]
    return (f"{ds[0]} → {ds[-1]}, {len(ds)} datos, hueco típico "
            f"{statistics.median(gaps):g} d, máximo {max(gaps)} d")


def recent_gap(series, days=120):
    """Hueco mediano (días) entre datos en los últimos `days` días."""
    limit = (date.today() - timedelta(days=days)).isoformat()
    ds = sorted(d for d in series if d >= limit)
    if len(ds) < 2:
        return None
    return statistics.median((date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(ds, ds[1:]))


def main():
    funds = json.loads(FUNDS.read_text(encoding="utf-8"))["funds"]
    navs = json.loads(NAVS.read_text(encoding="utf-8")) if NAVS.exists() else {}
    tickers = json.loads(TICKERS.read_text(encoding="utf-8")) if TICKERS.exists() else {}
    tickers_before = dict(tickers)

    if navs.get("schema") != SCHEMA:
        print("Formato antiguo de datos: se reconstruye el histórico desde cero.")
        navs = {}
    old_series = {isin: [list(p) for p in pts] for isin, pts in navs.get("series", {}).items()}
    old_sources = dict(navs.get("sources", {}))
    series = {isin: dict(map(tuple, pts)) for isin, pts in old_series.items()}
    sources = dict(old_sources)

    session, ms_err = open_mstar_session()
    if session is None:
        print(f"MORNINGSTAR NO DISPONIBLE: {ms_err}")
        print("  -> se usa Yahoo como respaldo (mira SELENIUM_CHROME_FLAGS / xvfb en el workflow)\n")

    ok, fail, skipped, summary = [], [], [], []
    for f in funds:
        isin = (f.get("isin") or "").strip()
        name = f["name"]
        if not isin:
            skipped.append(name)
            continue
        base = series.get(isin, {})
        new, src, notes = None, None, []

        if session is not None:
            try:
                new, src = fetch_mstar(isin, session), "morningstar"
            except Exception as e:
                notes.append(f"Morningstar: {short(e)}")
        if not new or len(new) < MIN_POINTS:
            try:
                sym, new, ynotes = fetch_yahoo_best(isin, (f.get("ticker") or "").strip(), tickers)
                src = f"yahoo:{sym}"
                notes += ynotes
            except Exception as e:
                notes.append(short(e, 200))
                new, src = None, None

        merged, msrc = combine(base, sources.get(isin), new, src or "", notes)
        if new and msrc:
            sources[isin] = msrc
        manual = read_manual(isin)
        if manual:
            merged.update(manual)
            notes.append(f"CSV manual: {len(manual)} datos")

        if merged and (new or manual or base):
            series[isin] = merged
        if new or manual:
            ok.append(f"{name} [{msrc or 'manual'}] {describe(merged)}" + ("  | " + "; ".join(notes) if notes else ""))
        else:
            fail.append(f"{name}: " + " | ".join(notes)
                        + ("  (se conservan los datos anteriores)" if base else ""))
        if merged:
            last = max(merged)
            summary.append((name, last, (date.today() - date.fromisoformat(last)).days,
                            (sources.get(isin) or "manual").split(":")[0], recent_gap(merged)))
        time.sleep(1)

    out = {
        "schema": SCHEMA,
        "updated": navs.get("updated") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": sources,
        "series": {isin: [list(p) for p in sorted(d.items())] for isin, d in series.items()},
    }
    changed = out["series"] != old_series or out["sources"] != old_sources
    NAVS.parent.mkdir(parents=True, exist_ok=True)
    if changed or not NAVS.exists():
        out["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        NAVS.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if tickers != tickers_before or not TICKERS.exists():
        TICKERS.write_text(json.dumps(tickers, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Actualizados: {len(ok)}   ({'datos nuevos guardados' if changed else 'sin cambios'})")
    for x in ok:
        print("  ok  ", x)
    for x in skipped:
        print("  SIN ISIN:", x)
    for x in fail:
        print("  FALLO:", x)

    print("\nResumen de frescura (hoy:", date.today().isoformat() + ")")
    stale = []
    for name, last, age, fam, gap in summary:
        flags = []
        if age > STALE_DAYS:
            flags.append("DESACTUALIZADO")
            stale.append(name)
        if gap is not None and gap > 3:
            flags.append(f"fuente casi semanal (hueco {gap:g} d)")
        print(f"  {name:<34} último dato {last} ({age} d) · {fam}" + ("  ⚠ " + ", ".join(flags) if flags else ""))

    if fail and not ok:
        sys.exit(1)
    if stale:
        print(f"\n⚠ {len(stale)} fondo(s) con más de {STALE_DAYS} días sin dato nuevo.")
        sys.exit(2)


if __name__ == "__main__":
    main()

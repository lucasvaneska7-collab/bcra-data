"""
Dashboard Monetario Argentina - Backend
Fetches monetary data from BCRA API and serves an interactive dashboard.
"""

import json
import logging
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BCRA_BASE = "https://api.bcra.gob.ar"
BCRA_MONETARIAS = f"{BCRA_BASE}/estadisticas/v4.0/monetarias"
REQUEST_TIMEOUT = 30

# Cache for variable catalog so we don't re-fetch it on every refresh
_variable_catalog = None


def fetch_variable_catalog():
    """Fetch the full list of monetary variables from BCRA."""
    global _variable_catalog
    if _variable_catalog is not None:
        return _variable_catalog

    all_vars = []
    offset = 0
    limit = 1000
    while True:
        url = f"{BCRA_MONETARIAS}?limit={limit}&offset={offset}"
        logger.info(f"Fetching variable catalog: {url}")
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        all_vars.extend(results)
        if len(results) < limit:
            break
        offset += limit

    _variable_catalog = all_vars
    logger.info(f"Fetched {len(all_vars)} variables from BCRA catalog")
    return all_vars


def find_variable(catalog, keywords, exclude_keywords=None):
    """Find a variable in the catalog by matching keywords in its description."""
    matches = []
    for var in catalog:
        desc = var.get("descripcion", "").lower()
        if all(kw.lower() in desc for kw in keywords):
            if exclude_keywords and any(ek.lower() in desc for ek in exclude_keywords):
                continue
            matches.append(var)
    return matches


def fetch_series(id_variable, desde, hasta):
    """Fetch time series data for a specific variable."""
    all_data = []
    offset = 0
    limit = 3000
    while True:
        url = (
            f"{BCRA_MONETARIAS}/{id_variable}"
            f"?desde={desde}&hasta={hasta}&limit={limit}&offset={offset}"
        )
        logger.info(f"Fetching series: idVariable={id_variable}, offset={offset}")
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        all_data.extend(results)
        if len(results) < limit:
            break
        offset += limit
    return all_data


def parse_series(raw_data):
    """Parse raw BCRA series data into sorted list of (date_str, value) tuples."""
    parsed = []
    for item in raw_data:
        fecha = item.get("fecha", "")
        valor = item.get("valor")
        if valor is None:
            continue
        # BCRA returns dates as "DD/MM/YYYY" or "YYYY-MM-DD"
        try:
            if "/" in fecha:
                dt = datetime.strptime(fecha, "%d/%m/%Y")
            else:
                dt = datetime.strptime(fecha, "%Y-%m-%d")
            # Clean up value: remove thousands separator, handle decimal
            if isinstance(valor, str):
                valor = valor.replace(".", "").replace(",", ".")
                valor = float(valor)
            else:
                valor = float(valor)
            parsed.append((dt.strftime("%Y-%m-%d"), valor))
        except (ValueError, TypeError) as e:
            logger.warning(f"Skipping bad data point: fecha={fecha}, valor={valor}, error={e}")
            continue
    parsed.sort(key=lambda x: x[0])
    return parsed


def compute_yoy(dates, values):
    """Compute year-over-year % change for a time series."""
    # Build a lookup dict for quick access
    date_val = dict(zip(dates, values))
    yoy_dates = []
    yoy_values = []
    for d, v in zip(dates, values):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            dt_prev = dt - timedelta(days=365)
            # Look for closest date within 10 days
            prev_val = None
            for delta in range(0, 15):
                candidate = (dt_prev + timedelta(days=delta)).strftime("%Y-%m-%d")
                if candidate in date_val:
                    prev_val = date_val[candidate]
                    break
                candidate = (dt_prev - timedelta(days=delta)).strftime("%Y-%m-%d")
                if candidate in date_val:
                    prev_val = date_val[candidate]
                    break
            if prev_val and prev_val != 0:
                yoy_dates.append(d)
                yoy_values.append(round((v / prev_val - 1) * 100, 2))
        except (ValueError, ZeroDivisionError):
            continue
    return yoy_dates, yoy_values


def to_billions(values):
    """Convert values to billions."""
    return [round(v / 1_000_000, 2) for v in values]


def align_and_sum(series_list):
    """Align multiple series by date and compute their sum at each date."""
    # Collect all dates
    all_dates = set()
    series_dicts = []
    for dates, values in series_list:
        d = dict(zip(dates, values))
        series_dicts.append(d)
        all_dates.update(dates)

    all_dates = sorted(all_dates)
    totals = []
    valid_dates = []
    for date in all_dates:
        vals = [sd.get(date) for sd in series_dicts]
        if all(v is not None for v in vals):
            valid_dates.append(date)
            totals.append(sum(vals))
    return valid_dates, totals


# ---------------------------------------------------------------------------
# Variable ID mapping — hardcoded from the BCRA catalog
# The BCRA API descriptions don't distinguish MN/ME, so we use known IDs.
# ---------------------------------------------------------------------------
VARIABLE_ID_MAP = {
    # Chart 1: Total credit (private + public sectors)
    "prestamos_spriv": 26,    # Préstamos de las entidades financieras al sector privado
    "prestamos_spub": 199,    # Préstamos a los gobiernos (national + provincial + municipal)

    # Chart 2: Consumer credit breakdown
    "hipotecarios": 112,      # Préstamos hipotecaríos otorgados al sector privado
    "prendarios": 113,        # Préstamos prendaríos otorgados al sector privado
    "personales": 114,        # Préstamos personales otorgados al sector privado
    "tarjetas": 115,          # Préstamos mediante tarjeta de créditos otorgados al sector privado

    # Chart 3: Total private sector loans (alternate series for comparison)
    "prestamos_spriv_alt": 117,  # Préstamos otorgados al sector privado (alternate)

    # Chart 4: Balance sheet — these variables are NOT available in the
    # monetarias endpoint. We keep them as None so the chart shows a message.
    "activo_efectivo": None,
    "activo_titulos": None,
    "activo_total": None,
}


def discover_variables(catalog):
    """Map required variables using hardcoded IDs from the BCRA catalog."""
    discovered = {}
    catalog_by_id = {v.get("idVariable"): v for v in catalog}

    for key, var_id in VARIABLE_ID_MAP.items():
        if var_id is None:
            discovered[key] = None
            logger.info(f"Variable '{key}' not available in this API endpoint")
            continue

        var = catalog_by_id.get(var_id)
        if var:
            discovered[key] = {
                "idVariable": var_id,
                "descripcion": var.get("descripcion", ""),
                "frecuencia": var.get("frecuencia", ""),
                "unidad": var.get("unidad", ""),
            }
            logger.info(f"Matched '{key}' -> id={var_id}: {var.get('descripcion', '')}")
        else:
            logger.warning(f"Variable ID {var_id} not found in catalog for '{key}'")
            discovered[key] = None

    return discovered


def build_chart_data(discovered, desde, hasta):
    """Fetch all required series and build chart data."""
    charts = {}

    # Helper to safely fetch a series
    def get_series(key):
        info = discovered.get(key)
        if not info:
            return [], []
        raw = fetch_series(info["idVariable"], desde, hasta)
        parsed = parse_series(raw)
        if not parsed:
            return [], []
        dates = [p[0] for p in parsed]
        values = [p[1] for p in parsed]
        return dates, values

    # -----------------------------------------------------------------------
    # Chart 1: Crecimiento del Credito (SPriv+SPub)
    # -----------------------------------------------------------------------
    spriv_dates, spriv_total = get_series("prestamos_spriv")
    spub_dates, spub_total = get_series("prestamos_spub")

    # Total for YoY
    total_dates, total_values = align_and_sum([
        (spriv_dates, spriv_total), (spub_dates, spub_total)
    ]) if spriv_dates and spub_dates else (spriv_dates or spub_dates, spriv_total or spub_total)

    yoy_dates_1, yoy_values_1 = compute_yoy(total_dates, total_values) if total_dates else ([], [])

    charts["chart1"] = {
        "title": "Crecimiento del Crédito (SPriv+SPub)",
        "spriv": {"dates": spriv_dates, "values": to_billions(spriv_total)},
        "spub": {"dates": spub_dates, "values": to_billions(spub_total)},
        "yoy": {"dates": yoy_dates_1, "values": yoy_values_1},
    }

    # -----------------------------------------------------------------------
    # Chart 2: Credito al Consumo
    # -----------------------------------------------------------------------
    hip_d, hip_v = get_series("hipotecarios")
    pren_d, pren_v = get_series("prendarios")
    pers_d, pers_v = get_series("personales")
    tarj_d, tarj_v = get_series("tarjetas")

    series_2 = [(d, v) for d, v in [(hip_d, hip_v), (pren_d, pren_v), (pers_d, pers_v), (tarj_d, tarj_v)] if d]
    total_dates_2, total_values_2 = align_and_sum(series_2) if series_2 else ([], [])
    yoy_dates_2, yoy_values_2 = compute_yoy(total_dates_2, total_values_2) if total_dates_2 else ([], [])

    charts["chart2"] = {
        "title": "Crédito al Consumo",
        "hipotecarios": {"dates": hip_d, "values": to_billions(hip_v)},
        "prendarios": {"dates": pren_d, "values": to_billions(pren_v)},
        "personales": {"dates": pers_d, "values": to_billions(pers_v)},
        "tarjetas": {"dates": tarj_d, "values": to_billions(tarj_v)},
        "yoy": {"dates": yoy_dates_2, "values": yoy_values_2},
    }

    # -----------------------------------------------------------------------
    # Chart 3: Prestamos al Sector Privado (serie alternativa)
    # -----------------------------------------------------------------------
    alt_d, alt_v = get_series("prestamos_spriv_alt")
    yoy_dates_3, yoy_values_3 = compute_yoy(alt_d, alt_v) if alt_d else ([], [])

    charts["chart3"] = {
        "title": "Préstamos al Sector Privado (Serie B)",
        "total_me": {"dates": alt_d, "values": to_billions(alt_v)},
        "yoy": {"dates": yoy_dates_3, "values": yoy_values_3},
    }

    # -----------------------------------------------------------------------
    # Chart 4: Composicion de Balances - Activo
    # -----------------------------------------------------------------------
    efec_d, efec_v = get_series("activo_efectivo")
    tit_d, tit_v = get_series("activo_titulos")
    at_d, at_v = get_series("activo_total")

    # Reuse sector loans from chart 1
    # Align all series to common dates
    all_asset_series = [
        (efec_d, efec_v),
        (tit_d, tit_v),
        (spub_dates, spub_total),
        (spriv_dates, spriv_total),
        (at_d, at_v),
    ]
    available_series = [(d, v) for d, v in all_asset_series if d]

    if available_series and at_d:
        # Find common dates across all series
        at_dict = dict(zip(at_d, at_v))
        efec_dict = dict(zip(efec_d, efec_v)) if efec_d else {}
        tit_dict = dict(zip(tit_d, tit_v)) if tit_d else {}
        spub_dict = dict(zip(spub_dates, spub_total)) if spub_dates else {}
        spriv_dict = dict(zip(spriv_dates, spriv_total)) if spriv_dates else {}

        common_dates = sorted(set(at_d))
        comp_dates = []
        comp_efec = []
        comp_tit = []
        comp_spub = []
        comp_spriv = []
        comp_otros = []

        for date in common_dates:
            total = at_dict.get(date)
            if not total or total == 0:
                continue
            e = efec_dict.get(date, 0)
            t = tit_dict.get(date, 0)
            sp = spub_dict.get(date, 0)
            spr = spriv_dict.get(date, 0)
            otros = total - e - t - sp - spr

            comp_dates.append(date)
            comp_efec.append(round(e / total * 100, 2))
            comp_tit.append(round(t / total * 100, 2))
            comp_spub.append(round(sp / total * 100, 2))
            comp_spriv.append(round(spr / total * 100, 2))
            comp_otros.append(round(otros / total * 100, 2))

        charts["chart4"] = {
            "title": "Composición de Balances - Activo",
            "dates": comp_dates,
            "efectivo": comp_efec,
            "titulos": comp_tit,
            "spub": comp_spub,
            "spriv": comp_spriv,
            "otros": comp_otros,
        }
    else:
        charts["chart4"] = {
            "title": "Composición de Balances - Activo",
            "dates": [], "efectivo": [], "titulos": [],
            "spub": [], "spriv": [], "otros": [],
        }

    return charts


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/refresh")
def api_refresh():
    """Fetch all data from BCRA and return processed chart data."""
    try:
        catalog = fetch_variable_catalog()
        discovered = discover_variables(catalog)

        # Check which variables were found (exclude intentionally-None entries)
        intentionally_missing = {k for k, v in VARIABLE_ID_MAP.items() if v is None}
        missing = [k for k, v in discovered.items() if v is None and k not in intentionally_missing]
        if missing:
            logger.warning(f"Missing variables: {missing}")

        # Default date range: 5 years back
        hasta = datetime.now().strftime("%Y-%m-%d")
        desde = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")

        charts = build_chart_data(discovered, desde, hasta)

        return jsonify({
            "status": "ok",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "missing_variables": missing,
            "charts": charts,
        })
    except requests.exceptions.RequestException as e:
        logger.error(f"BCRA API error: {e}")
        return jsonify({"status": "error", "message": f"Error connecting to BCRA API: {str(e)}"}), 502
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/variables")
def api_variables():
    """Return the full variable catalog for debugging."""
    try:
        catalog = fetch_variable_catalog()
        return jsonify({
            "count": len(catalog),
            "variables": [
                {
                    "idVariable": v.get("idVariable"),
                    "descripcion": v.get("descripcion"),
                    "frecuencia": v.get("frecuencia"),
                    "unidad": v.get("unidad"),
                }
                for v in catalog
            ],
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/clear-cache")
def api_clear_cache():
    """Clear the cached variable catalog so it's re-fetched on next refresh."""
    global _variable_catalog
    _variable_catalog = None
    return jsonify({"status": "ok", "message": "Variable catalog cache cleared"})


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  Dashboard Monetario Argentina")
    print("  Open your browser at: http://localhost:5000")
    print("  Press Ctrl+C to stop the server")
    print("=" * 60 + "\n")
    app.run(debug=False, host="127.0.0.1", port=5000)

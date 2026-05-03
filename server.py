"""
Travel Atlas MCP Server — guided LLM curation + REST Countries enrichment.

Three tools, one file.

  1. find_destinations(month, travel_style) -> dict
     INTERNET (LLM). Asks Gemini for exactly 6 country recommendations from
     India, matched to the given month and travel style. Returns names plus
     a one-line "why this country" reason for each.

  2. recommendations_crud(action, month, travel_style, countries) -> dict
     CRUD on a local file (`recommendations.json`), keyed by `<month>__<style>`.
     On `create` / `update` the country names are enriched live from the
     restcountries.com API and written to disk.

  3. travel_dashboard(month, travel_style) -> PrefabApp
     PREFAB UI tool (`@mcp.tool(app=True)`). Renders an interactive dashboard
     from `recommendations.json`. If `month` and `travel_style` are passed,
     that recommendation set's tab is the default open tab.

Run:
    pip install -r requirements.txt
    fastmcp dev apps server.py        # Prefab-aware previewer
    # or, for a real MCP host:
    python server.py
"""

from __future__ import annotations

import calendar
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Badge,
    Card,
    CardContent,
    CardHeader,
    CardTitle,
    Column,
    H1,
    H2,
    H3,
    Muted,
    Row,
    Tab,
    Tabs,
    Text,
)
from prefab_ui.components.charts import (
    BarChart,
    ChartSeries,
    PieChart,
)


load_dotenv()

HERE = Path(__file__).parent
RECS_FILE = HERE / "recommendations.json"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

mcp = FastMCP("TravelAtlasServer")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STYLES: tuple[str, ...] = (
    "adventure",
    "beach",
    "cultural",
    "food",
    "romantic",
    "family",
    "nature",
)

# Quick humanised hint for each style — feeds the LLM prompt and the dashboard.
STYLE_NOTES: dict[str, str] = {
    "adventure": "trekking, hiking, extreme sports, off-beat trails",
    "beach":     "coast/island getaways, water sports, snorkeling, diving",
    "cultural":  "museums, heritage sites, history, traditional experiences",
    "food":      "cuisine-led travel, food markets, regional specialties",
    "romantic":  "scenic, intimate, couples-friendly, atmospheric",
    "family":    "kid-friendly, varied attractions, easy logistics",
    "nature":    "national parks, wildlife, landscapes, photography",
}

# Map common country names that REST Countries doesn't match exactly.
COUNTRY_NAME_OVERRIDES: dict[str, str] = {
    "United Arab Emirates": "uae",
    "UAE": "uae",
    "United Kingdom": "united kingdom",
    "United States": "united states",
    "USA": "united states",
    "Czech Republic": "czechia",
    "South Korea": "korea (republic of)",
    "Bali": "indonesia",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _normalise_month(month: str) -> str:
    """Accept 'jan', 'JAN', 'January', '1', etc. and return 'January'."""
    s = str(month).strip()
    if not s:
        raise ValueError("month is required")
    if s.isdigit():
        idx = int(s)
        if 1 <= idx <= 12:
            return calendar.month_name[idx]
        raise ValueError(f"month index out of range: {s}")
    s_low = s.lower()
    for full in calendar.month_name[1:]:
        if full.lower().startswith(s_low):
            return full
    raise ValueError(f"could not parse month: {month!r}")


def _normalise_style(style: str) -> str:
    s = str(style).strip().lower()
    if s not in VALID_STYLES:
        raise ValueError(f"travel_style must be one of {list(VALID_STYLES)}, got {style!r}")
    return s


def _flag_emoji(cca2: str) -> str:
    if not cca2 or len(cca2) != 2:
        return ""
    return "".join(chr(0x1F1E6 + ord(c.upper()) - ord("A")) for c in cca2)


def _slug(s: str, default: str = "x") -> str:
    out = re.sub(r"[^a-zA-Z0-9_]+", "_", str(s)).strip("_").lower()
    return out or default


def _set_key(month: str, travel_style: str) -> str:
    return f"{_normalise_month(month)}__{_normalise_style(travel_style)}"


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "travel-atlas-mcp/2.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _restcountries_lookup(name: str) -> dict | None:
    """Query restcountries.com for `name`. Returns a slim dict, or None."""
    slug = COUNTRY_NAME_OVERRIDES.get(name, name)
    url = (
        "https://restcountries.com/v3.1/name/"
        f"{urllib.parse.quote(slug)}"
        "?fields=name,cca2,capital,population,region,subregion,currencies,languages,flag"
    )
    try:
        rows = _http_get_json(url)
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None

    chosen = rows[0]
    for row in rows:
        common = ((row.get("name") or {}).get("common") or "").lower()
        if common == name.lower():
            chosen = row
            break

    name_obj = chosen.get("name") or {}
    capitals = chosen.get("capital") or []
    currencies = chosen.get("currencies") or {}
    languages = chosen.get("languages") or {}

    return {
        "name": name_obj.get("common", name),
        "official_name": name_obj.get("official", ""),
        "cca2": chosen.get("cca2", ""),
        "flag": chosen.get("flag") or _flag_emoji(chosen.get("cca2", "")),
        "capital": capitals[0] if capitals else "",
        "population": chosen.get("population", 0),
        "region": chosen.get("region", ""),
        "subregion": chosen.get("subregion", ""),
        "currencies": [
            f"{code} ({info.get('symbol', '?')}) — {info.get('name', '')}"
            for code, info in currencies.items()
        ],
        "languages": list(languages.values()),
    }


# ---------------------------------------------------------------------------
# LLM helper (lazy — only initialised when find_destinations is called)
# ---------------------------------------------------------------------------


_GEMINI_CLIENT: Any = None


def _gemini_client() -> Any:
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is not None:
        return _GEMINI_CLIENT
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Add it to .env so find_destinations can call Gemini."
        )
    # Imported lazily so server.py still imports clean when google-genai is missing.
    from google import genai
    _GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    return _GEMINI_CLIENT


_LLM_PROMPT_TEMPLATE = """\
You are a travel curator helping someone plan a trip FROM INDIA.

Recommend EXACTLY 6 countries that are excellent to visit from India in {month}
for {style} travelers ({style_note}). Consider:
  - Weather and seasonality during {month} at the destination.
  - Visa accessibility from India (prefer visa-on-arrival or e-visa).
  - Reasonable flight time from major Indian cities.
  - Strong fit with the {style} travel style.

Output EXACTLY this JSON object — no prose, no markdown fences:
{{
  "countries": [
    {{"name": "<Country>", "reason": "<one short sentence>"}},
    ...exactly 6 entries...
  ]
}}

Use widely-recognised country names (e.g. "Switzerland", "United Arab Emirates",
"Japan"). Avoid sub-regions or cities (no "Bali", no "Tokyo"). Do not repeat
countries.
"""


def _llm_recommend(month: str, style: str) -> list[dict]:
    """Call Gemini and return a list of {name, reason} dicts (length 6)."""
    prompt = _LLM_PROMPT_TEMPLATE.format(
        month=month, style=style, style_note=STYLE_NOTES.get(style, style)
    )
    client = _gemini_client()
    resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    raw = (resp.text or "").strip()

    # Strip markdown fences if Gemini ignores instructions.
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Best-effort regex fallback: pull the first {...} block.
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"Gemini returned unparseable JSON: {raw[:300]!r}")
        parsed = json.loads(m.group())

    countries = parsed.get("countries") or []
    out: list[dict] = []
    for c in countries:
        if isinstance(c, dict) and "name" in c:
            out.append({"name": str(c["name"]), "reason": str(c.get("reason", ""))})
        elif isinstance(c, str):
            out.append({"name": c, "reason": ""})
    if not out:
        raise ValueError(f"Gemini returned no countries: {raw[:300]!r}")
    return out


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


def _load_recs() -> dict:
    if not RECS_FILE.exists():
        return {}
    try:
        return json.loads(RECS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_recs(data: dict) -> None:
    RECS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# TOOL 1 — internet (LLM curation)
# ---------------------------------------------------------------------------


@mcp.tool()
def find_destinations(month: str, travel_style: str) -> dict:
    """Ask Gemini for 6 country recommendations from India for a given month + travel style.

    Args:
        month: Month name (e.g. "December", "dec", "12").
        travel_style: One of adventure, beach, cultural, food, romantic, family, nature.

    Returns:
        {
          "month": "<canonical month>",
          "travel_style": "<canonical style>",
          "countries": [{"name": "Switzerland", "reason": "..."}, ... 6 entries]
        }

    Notes:
        - This tool only calls the LLM. It returns names + reasons. Run
          recommendations_crud(action="create", ...) to persist + enrich.
    """
    canonical_month = _normalise_month(month)
    canonical_style = _normalise_style(travel_style)
    countries = _llm_recommend(canonical_month, canonical_style)
    return {
        "month": canonical_month,
        "travel_style": canonical_style,
        "countries": countries,
    }


# ---------------------------------------------------------------------------
# TOOL 2 — CRUD on recommendations.json (with REST Countries enrichment on save)
# ---------------------------------------------------------------------------


@mcp.tool()
def recommendations_crud(
    action: str,
    month: str = "",
    travel_style: str = "",
    countries: list | None = None,
    reasons: dict | None = None,
) -> dict:
    """CRUD on `recommendations.json`. Keyed by `<Month>__<style>`.

    Args:
        action: One of "create", "read", "update", "delete", "list".
        month: Required for everything except "list".
        travel_style: Required for everything except "list".
        countries: List of country names (or {name, reason} dicts). Required
                   for "create" and "update".
        reasons: Optional dict mapping country name -> reason string. Used
                 alongside `countries` when `countries` is a list of strings.

    On create/update, every country name is enriched live from REST Countries
    before being written to disk.
    """
    action = action.strip().lower()
    data = _load_recs()

    if action == "list":
        return {
            "ok": True,
            "action": "list",
            "data": list(data.keys()),
            "count": len(data),
        }

    if action not in {"create", "read", "update", "delete"}:
        return {"ok": False, "action": action, "error": f"unknown action {action!r}"}

    if not month or not travel_style:
        return {
            "ok": False,
            "action": action,
            "error": "month and travel_style are required",
        }

    try:
        key = _set_key(month, travel_style)
    except ValueError as e:
        return {"ok": False, "action": action, "error": str(e)}
    canonical_month, canonical_style = key.split("__", 1)

    if action == "read":
        if key not in data:
            return {"ok": False, "action": "read", "error": f"{key!r} not found"}
        return {"ok": True, "action": "read", "data": {key: data[key]}}

    if action == "delete":
        if key not in data:
            return {"ok": False, "action": "delete", "error": f"{key!r} not found"}
        removed = data.pop(key)
        _save_recs(data)
        return {"ok": True, "action": "delete", "data": {key: removed}}

    if action == "create" and key in data:
        return {
            "ok": False,
            "action": "create",
            "error": f"{key!r} already exists; call with action='update' instead",
        }
    if action == "update" and key not in data:
        return {
            "ok": False,
            "action": "update",
            "error": f"{key!r} not found; call with action='create' first",
        }

    if not countries:
        return {
            "ok": False,
            "action": action,
            "error": "countries (list of names) is required for create/update",
        }

    # Normalise the input — accept either ["Switzerland", ...] or
    # [{"name": "Switzerland", "reason": "..."}, ...].
    name_to_reason: dict[str, str] = {}
    for c in countries:
        if isinstance(c, dict):
            n = str(c.get("name", "")).strip()
            r = str(c.get("reason", "")).strip()
            if n:
                name_to_reason[n] = r
        elif isinstance(c, str):
            n = c.strip()
            if n:
                name_to_reason.setdefault(n, "")
    if reasons:
        for n, r in reasons.items():
            if n in name_to_reason and not name_to_reason[n]:
                name_to_reason[n] = str(r)

    if not name_to_reason:
        return {"ok": False, "action": action, "error": "no usable country names"}

    # Enrich each name via REST Countries.
    enriched: list[dict] = []
    enrichment_misses: list[str] = []
    for name, reason in name_to_reason.items():
        info = _restcountries_lookup(name)
        if info is None:
            enrichment_misses.append(name)
            enriched.append({
                "name": name,
                "flag": "",
                "capital": "",
                "population": 0,
                "region": "",
                "subregion": "",
                "currencies": [],
                "languages": [],
                "reason": reason,
                "live": False,
            })
        else:
            info["reason"] = reason
            info["live"] = True
            enriched.append(info)

    record = {
        "month": canonical_month,
        "travel_style": canonical_style,
        "destinations": enriched,
        "saved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    data[key] = record
    _save_recs(data)
    return {
        "ok": True,
        "action": action,
        "data": {key: record},
        "enrichment_misses": enrichment_misses,
    }


# ---------------------------------------------------------------------------
# TOOL 3 — Prefab UI
# ---------------------------------------------------------------------------


_STYLE_VARIANT: dict[str, str] = {
    "adventure": "warning",
    "beach":     "default",
    "cultural":  "success",
    "food":      "destructive",
    "romantic":  "default",
    "family":    "success",
    "nature":    "warning",
}


def _build_overview_aggregates(recs: dict) -> dict:
    sets = list(recs.values())
    total_sets = len(sets)
    all_destinations = [d for s in sets for d in s.get("destinations") or []]
    by_region = Counter([d.get("region") or "Unknown" for d in all_destinations])
    by_month = Counter([s.get("month") or "Unknown" for s in sets])
    by_style = Counter([s.get("travel_style") or "Unknown" for s in sets])

    region_data = [{"name": k, "value": v} for k, v in by_region.most_common()]
    month_order = list(calendar.month_name)[1:]
    month_data = [
        {"month": m, "sets": by_month.get(m, 0)}
        for m in month_order
        if by_month.get(m, 0) > 0
    ]
    return {
        "total_sets": total_sets,
        "total_destinations": len(all_destinations),
        "by_region": by_region,
        "by_style": by_style,
        "region_data": region_data,
        "month_data": month_data,
    }


@mcp.tool(app=True)
def travel_dashboard(month: str = "", travel_style: str = "") -> PrefabApp:
    """Render the Prefab dashboard from `recommendations.json`.

    If `month` and `travel_style` are passed and a saved set matches, that
    set's tab is the default open tab (i.e. highlighted).
    """
    recs = _load_recs()
    agg = _build_overview_aggregates(recs)

    default_tab = "overview"
    if month and travel_style:
        try:
            key = _set_key(month, travel_style)
        except ValueError:
            key = ""
        if key and key in recs:
            default_tab = _slug(key)

    with PrefabApp(css_class="max-w-5xl mx-auto p-6") as app:
        with Card():
            with CardHeader():
                CardTitle("Travel Atlas — countries to visit from India")
            with CardContent():
                with Column(gap=5):
                    # Top stats row.
                    with Row(gap=5):
                        with Column(gap=1):
                            Muted("Recommendation sets")
                            H1(str(agg["total_sets"]))
                            Muted("recommendations.json")
                        with Column(gap=1):
                            Muted("Countries surfaced")
                            H1(str(agg["total_destinations"]))
                            Muted(f"across {len(agg['by_region'])} regions")
                        with Column(gap=1):
                            Muted("Style mix")
                            with Row(gap=2):
                                if agg["by_style"]:
                                    for style_name, n in agg["by_style"].most_common():
                                        Badge(
                                            f"{style_name}: {n}",
                                            variant=_STYLE_VARIANT.get(style_name, "default"),
                                        )
                                else:
                                    Muted("(none yet)")

                    if not recs:
                        Muted(
                            "No recommendations saved yet. Call find_destinations(month, "
                            "travel_style) then recommendations_crud(action='create', ...) "
                            "first."
                        )
                    else:
                        with Tabs(value=default_tab):
                            # Overview tab.
                            with Tab("Overview", value="overview"):
                                with Column(gap=5):
                                    if agg["region_data"]:
                                        H3("Destinations by region")
                                        PieChart(
                                            data=agg["region_data"],
                                            data_key="value",
                                            name_key="name",
                                            show_legend=True,
                                        )
                                    if agg["month_data"]:
                                        H3("Sets saved by month")
                                        BarChart(
                                            data=agg["month_data"],
                                            series=[ChartSeries(data_key="sets", label="Sets")],
                                            x_axis="month",
                                            show_legend=False,
                                        )

                                    H3("All saved sets")
                                    with Row(gap=3):
                                        for h in ("Month", "Style", "# countries", "Saved at"):
                                            Text(h)
                                    for k, s in recs.items():
                                        with Row(gap=3):
                                            Text(s.get("month", ""))
                                            Text(s.get("travel_style", ""))
                                            Text(str(len(s.get("destinations") or [])))
                                            Text((s.get("saved_at", "") or "").replace("T", " "))

                            # One tab per saved set.
                            for key, s in recs.items():
                                month_name = s.get("month", "")
                                style_name = s.get("travel_style", "")
                                tab_label = f"{month_name[:3]} · {style_name}"
                                with Tab(tab_label, value=_slug(key)):
                                    with Column(gap=4):
                                        H2(f"{month_name} · {style_name}")
                                        with Row(gap=3):
                                            Badge(
                                                style_name,
                                                variant=_STYLE_VARIANT.get(style_name, "default"),
                                            )
                                            Muted(STYLE_NOTES.get(style_name, ""))
                                            Muted(
                                                f"{len(s.get('destinations') or [])} destinations · "
                                                f"saved {s.get('saved_at', '').replace('T', ' ')}"
                                            )

                                        # Per-country card.
                                        for d in s.get("destinations") or []:
                                            with Card():
                                                with CardContent():
                                                    with Column(gap=2):
                                                        with Row(gap=3):
                                                            H2(d.get("flag", "") or "·")
                                                            with Column(gap=1):
                                                                H3(d.get("name", ""))
                                                                Muted(
                                                                    f"{d.get('capital', '')} · "
                                                                    f"{d.get('region', '')} "
                                                                    f"({d.get('subregion', '')})"
                                                                )
                                                        with Row(gap=2):
                                                            pop = d.get("population") or 0
                                                            Badge(f"pop {pop:,}", variant="default")
                                                            for cur in (d.get("currencies") or [])[:1]:
                                                                Badge(cur.split(" — ")[0], variant="success")
                                                            for lang in (d.get("languages") or [])[:3]:
                                                                Badge(lang, variant="warning")
                                                        if d.get("reason"):
                                                            Text(f"Why: {d['reason']}")
    return app


# ---------------------------------------------------------------------------
# Source-string renderer (for the agent that runs `prefab serve` itself)
# ---------------------------------------------------------------------------


def build_dashboard_source(month: str = "", travel_style: str = "") -> str:
    """Return Python source equivalent to `travel_dashboard(month, travel_style)`.

    Writing this to a file and running `prefab serve <file>` produces the same
    UI that `travel_dashboard()` returns over MCP.
    """
    recs = _load_recs()
    agg = _build_overview_aggregates(recs)

    default_tab = "overview"
    if month and travel_style:
        try:
            key = _set_key(month, travel_style)
        except ValueError:
            key = ""
        if key and key in recs:
            default_tab = _slug(key)

    L: list[str] = []
    p = L.append

    p("# Auto-generated by build_dashboard_source(). Do not hand-edit.")
    p("from prefab_ui.app import PrefabApp")
    p("from prefab_ui.components import (")
    p("    Badge, Card, CardContent, CardHeader, CardTitle,")
    p("    Column, H1, H2, H3, Muted, Row, Tab, Tabs, Text,")
    p(")")
    p("from prefab_ui.components.charts import (")
    p("    BarChart, ChartSeries, PieChart,")
    p(")")
    p("")
    p('with PrefabApp(css_class="max-w-5xl mx-auto p-6") as app:')
    p("    with Card():")
    p("        with CardHeader():")
    p('            CardTitle("Travel Atlas — countries to visit from India")')
    p("        with CardContent():")
    p("            with Column(gap=5):")

    # Stats row.
    p("                with Row(gap=5):")
    p("                    with Column(gap=1):")
    p('                        Muted("Recommendation sets")')
    p(f"                        H1({str(agg['total_sets'])!r})")
    p('                        Muted("recommendations.json")')
    p("                    with Column(gap=1):")
    p('                        Muted("Countries surfaced")')
    p(f"                        H1({str(agg['total_destinations'])!r})")
    regions_label = f"across {len(agg['by_region'])} regions"
    p(f"                        Muted({regions_label!r})")
    p("                    with Column(gap=1):")
    p('                        Muted("Style mix")')
    p("                        with Row(gap=2):")
    if agg["by_style"]:
        for style_name, n in agg["by_style"].most_common():
            variant = _STYLE_VARIANT.get(style_name, "default")
            p(f"                            Badge({f'{style_name}: {n}'!r}, variant={variant!r})")
    else:
        p('                            Muted("(none yet)")')

    if not recs:
        p(
            '                Muted("No recommendations saved yet. Call find_destinations'
            "(month, travel_style) then recommendations_crud(action='create', ...) first.\")"
        )
    else:
        p(f'                with Tabs(value={default_tab!r}):')

        # Overview tab.
        p('                    with Tab("Overview", value="overview"):')
        p("                        with Column(gap=5):")
        if agg["region_data"]:
            p('                            H3("Destinations by region")')
            p(
                f"                            PieChart(data={agg['region_data']!r}, "
                f'data_key="value", name_key="name", show_legend=True)'
            )
        if agg["month_data"]:
            p('                            H3("Sets saved by month")')
            p(
                f"                            BarChart(data={agg['month_data']!r}, "
                f'series=[ChartSeries(data_key="sets", label="Sets")], '
                f'x_axis="month", show_legend=False)'
            )

        p('                            H3("All saved sets")')
        p("                            with Row(gap=3):")
        for h in ("Month", "Style", "# countries", "Saved at"):
            p(f"                                Text({h!r})")
        for s in recs.values():
            p("                            with Row(gap=3):")
            p(f"                                Text({str(s.get('month', ''))!r})")
            p(f"                                Text({str(s.get('travel_style', ''))!r})")
            p(f"                                Text({str(len(s.get('destinations') or []))!r})")
            saved = (s.get("saved_at", "") or "").replace("T", " ")
            p(f"                                Text({saved!r})")

        # Per-set tabs.
        for key, s in recs.items():
            month_name = s.get("month", "")
            style_name = s.get("travel_style", "")
            tab_label = f"{month_name[:3]} · {style_name}"
            variant = _STYLE_VARIANT.get(style_name, "default")
            p(f'                    with Tab({tab_label!r}, value={_slug(key)!r}):')
            p("                        with Column(gap=4):")
            header_label = f"{month_name} · {style_name}"
            p(f"                            H2({header_label!r})")
            p("                            with Row(gap=3):")
            p(f"                                Badge({style_name!r}, variant={variant!r})")
            p(f"                                Muted({STYLE_NOTES.get(style_name, '')!r})")
            saved_str = (s.get("saved_at", "") or "").replace("T", " ")
            count_n = len(s.get("destinations") or [])
            line = f"{count_n} destinations · saved {saved_str}"
            p(f"                                Muted({line!r})")

            for d in s.get("destinations") or []:
                p("                            with Card():")
                p("                                with CardContent():")
                p("                                    with Column(gap=2):")
                p("                                        with Row(gap=3):")
                flag_text = d.get("flag", "") or "·"
                p(f"                                            H2({flag_text!r})")
                p("                                            with Column(gap=1):")
                p(f"                                                H3({d.get('name', '')!r})")
                muted_line = (
                    f"{d.get('capital', '')} · "
                    f"{d.get('region', '')} ({d.get('subregion', '')})"
                )
                p(f"                                                Muted({muted_line!r})")
                p("                                        with Row(gap=2):")
                pop = d.get("population") or 0
                pop_label = f"pop {pop:,}"
                p(f"                                            Badge({pop_label!r}, variant=\"default\")")
                first_cur = (d.get("currencies") or [None])[0]
                if first_cur:
                    cur_label = first_cur.split(" — ")[0]
                    p(f"                                            Badge({cur_label!r}, variant=\"success\")")
                for lang in (d.get("languages") or [])[:3]:
                    p(f"                                            Badge({lang!r}, variant=\"warning\")")
                if d.get("reason"):
                    why = f"Why: {d['reason']}"
                    p(f"                                        Text({why!r})")

    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()

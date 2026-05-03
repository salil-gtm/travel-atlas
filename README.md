# Travel Atlas

> Pick a month. Pick a travel style. Get six LLM-curated countries to visit
> from India, enriched live from public country data, and rendered as an
> interactive Prefab dashboard — all driven by an MCP agent loop.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

A self-contained MCP server with three tools (an LLM curation tool, a JSON
CRUD tool, and a Prefab UI tool) plus a Gemini-driven agent loop that uses
all three.

---

## Demo at a glance

```
$ python agent.py
Travel Atlas Agent — guided LLM curation + REST Countries + Prefab UI
Tools: find_destinations, recommendations_crud, travel_dashboard
Model: gemini-2.5-flash-lite

Starting `prefab serve generated_dashboard.py` (logs → prefab_server.log) ...
Open http://127.0.0.1:5175 in your browser.

> [Enter]
Which month would you like to travel? > December
Pick a travel style (adventure | beach | cultural | food | romantic | family | nature) > adventure

  [agent] → find_destinations: month='December' travel_style='adventure'
  [agent] ← find_destinations: 6 countries: Switzerland, New Zealand, Nepal, ...
  [agent] → recommendations_crud: action='create' month='December' style='adventure' ...
  [agent] ← recommendations_crud: {"ok": true, "keys": ["December__adventure"]}
  [agent] → travel_dashboard: month='December' travel_style='adventure'
  [agent] ← travel_dashboard: {"ok": true, "url": "http://127.0.0.1:5175", ...}

  AGENT: Saved 6 December · adventure picks. Open http://127.0.0.1:5175 —
         the "Dec · adventure" tab is highlighted.
```

The browser shows country cards with flag, capital, region, population,
currency, languages, and the LLM's "why this country" line.

---

## Why this project exists

Travel Atlas demonstrates a clean three-tool agent pattern:

1. Something **internet**-y (here: an LLM API call).
2. **CRUD** on a local file (here: a recommendations JSON store).
3. Something that **communicates back via UI** (here: a Prefab dashboard
   served at `http://127.0.0.1:5175`).

Plus a single agent loop that uses all three in order. The user picks a
**month** and a **travel style**, and the agent curates a real
recommendation set, persists it, and renders a per-set tab on the
dashboard. Every run touches all three tools.

---

## The three tools

| # | Tool | Category | What it does |
|---|------|----------|--------------|
| 1 | `find_destinations(month, travel_style)` | **Internet (Gemini API)** | Asks Gemini for 6 country recommendations matched to the month + travel style, from India. Returns names + a one-line "why this country" reason. |
| 2 | `recommendations_crud(action, month, travel_style, countries, reasons)` | **CRUD on local file** | Manages `recommendations.json`, keyed by `<Month>__<style>` so each combo is its own saved set. On `create` / `update`, every country name is enriched live from the [REST Countries API](https://restcountries.com) (capital, population, currencies, languages, region, flag) before being written to disk. |
| 3 | `travel_dashboard(month, travel_style)` | **Prefab UI** (`@mcp.tool(app=True)`) | Renders an interactive dashboard from `recommendations.json`: stats row, Overview tab (regions pie chart + months bar chart + saved-sets table), and one tab per saved set with country cards. Tab matching the passed `(month, travel_style)` is the default open tab. |

---

## Free-text mode

If you don't use the guided REPL, type one sentence and the agent does the
rest:

> *I want to plan a trip from India in December with an adventure travel style. Find me 6 country recommendations, save them to recommendations.json, then show me the travel dashboard with that tab highlighted.*

The agent always hits all three tools, in order:
`find_destinations` → `recommendations_crud(create)` → `travel_dashboard`.

---

## Setup

```bash
git clone <your-repo-url> travel_atlas
cd travel_atlas

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env, set GEMINI_API_KEY=...
```

Prefab needs **Python 3.10+**.

`.env` keys:

```dotenv
GEMINI_API_KEY=...                 # required
GEMINI_MODEL=gemini-2.5-flash-lite # default
THROTTLE_SECONDS=4                 # delay before each Gemini call (rate-limit guard)
```

---

## Running it — three ways

### A. Full agent loop

```bash
python agent.py
```

When `prefab serve` starts and the URL is printed, open it. Then either:

- Hit **Enter** at the prompt → **guided mode**: agent asks Month, then Style, then runs the full chain.
- Type a **free-text** request → agent's LLM extracts month + style and chooses tools.
- Type `quit` to exit.

The terminal prints `[agent]` and `[llm]` lines for every turn. The browser updates the moment `travel_dashboard` runs.

### B. Manual MCP preview

```bash
fastmcp dev apps server.py
```

FastMCP's Prefab-aware previewer. Click any of the three tools by hand to see the dashboard render inline.

### C. Real MCP host (Claude Desktop / Cowork)

```bash
python server.py
```

Register `server.py` in your host's MCP config, then ask the host:
*"Find me 6 adventure travel countries from India for December, save them, and show the dashboard."*

---

## Architecture

```
   user prompt
       │
       ▼
    agent.py (guided REPL or free-text)
       │
       ▼  Gemini → JSON tool call
       ┌──────────────────────────────────────────────────────────────┐
       │ find_destinations(month, travel_style)                       │
       │   → Gemini API → ["Switzerland","NZ","Nepal",...]            │
       ├──────────────────────────────────────────────────────────────┤
       │ recommendations_crud(action="create", month, style,          │
       │                      countries=[...names...])                 │
       │   → REST Countries API per name (capital, pop, currency, ...)│
       │   → write recommendations.json                               │
       ├──────────────────────────────────────────────────────────────┤
       │ travel_dashboard(month, travel_style)                        │
       │   → write generated_dashboard.py from build_dashboard_source │
       │   → restart `prefab serve` subprocess                        │
       │   → browser at http://127.0.0.1:5175 reconnects               │
       └──────────────────────────────────────────────────────────────┘
                                                      ▼
                                           Prefab dashboard:
                                             stats row · Overview tab
                                             (pie + bar + table) ·
                                             one tab per saved set
                                             with country cards
                                             (flag, capital, pop,
                                             currency, languages,
                                             "why this country")
                                             — matching tab is default.
```

---

## Repository layout

```
travel_atlas/
├── server.py            # MCP server: 3 tools, all decorated.
├── agent.py             # Gemini agent + guided REPL + prefab subprocess.
├── requirements.txt     # prefab-ui, fastmcp, google-genai, python-dotenv.
├── Makefile             # convenience targets: install / run / preview / clean.
├── .env.example         # GEMINI_API_KEY etc. (real `.env` is gitignored).
├── .gitignore
├── LICENSE              # MIT.
├── README.md            # you are here.
└── (created at runtime, all gitignored)
    ├── recommendations.json    # CRUD target.
    ├── generated_dashboard.py  # written by tool 3 for `prefab serve`.
    └── prefab_server.log
```

---

## Convenience commands (Makefile)

```bash
make install   # pip install -r requirements.txt
make run       # python agent.py
make preview   # fastmcp dev apps server.py (Prefab-aware MCP previewer)
make verify    # syntax-parse server.py and agent.py
make clean     # remove runtime artifacts (recommendations.json, logs, caches)
```

---

## End-to-end walkthrough

1. Terminal: `python agent.py` boots, `prefab serve` starts.
2. Open the browser at <http://127.0.0.1:5175> — placeholder card.
3. Hit Enter → guided mode → answer **December** → answer **adventure**.
4. Watch the three tool calls scroll by with the `[agent]` / `[llm]` lines.
5. Browser reconnects with the populated dashboard. The **"Dec · adventure"** tab is the default; click into it to see country cards.
6. Hit Enter again, pick **June** + **beach** — the dashboard grows a second tab while keeping December · adventure intact.

---

## Notes / known caveats

- Gemini's free tier is rate-limited. `agent.py` throttles each LLM call by `THROTTLE_SECONDS` seconds (default `4`, set in `.env`). Bump it if you hit 429s.
- `prefab serve --reload` is sometimes flaky, so the agent does a hard restart on every `travel_dashboard` call.
- REST Countries occasionally returns multiple matches (e.g. "United States"). The lookup picks the exact-name match when present, else the first row.
- If Gemini suggests something REST Countries can't find by name (rare), `recommendations_crud` keeps the entry but marks it `live: false` and reports it in `enrichment_misses`.

---

## Credits

- LLM curation by [Google Gemini](https://ai.google.dev/).
- Country data by the free [REST Countries API](https://restcountries.com).
- UI by [Prefab](https://pypi.org/project/prefab-ui/) (`prefab-ui`).
- Tool surface by [FastMCP](https://github.com/jlowin/fastmcp) (`fastmcp`).

---

## License

[MIT](LICENSE) © 2026 Salil Gautam.

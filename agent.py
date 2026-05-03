"""
Travel Atlas Agent — guided LLM loop driving 3 MCP tools.

Tools wired to the agent:
  1. find_destinations(month, travel_style)  — Gemini curates 6 country names.
  2. recommendations_crud(action, ...)        — CRUD on recommendations.json,
                                                with REST Countries enrichment
                                                on save.
  3. travel_dashboard(month, travel_style)    — Prefab UI served at
                                                http://127.0.0.1:5175. The
                                                tab matching (month, style) is
                                                opened by default.

Run:
    pip install -r requirements.txt
    cp .env.example .env       # set GEMINI_API_KEY=...
    python agent.py

You can use either mode at the input prompt:
  - "guided" (or just hit Enter)  → asks you Month, then Travel style.
  - free-text                       → describe the trip in one sentence; the
                                      agent's LLM picks tools to satisfy it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai

# Direct import — the @mcp.tool decorators are no-ops at import time, so the
# tool functions are just normal Python callables.
import server as mcp_server

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
THROTTLE_SECONDS = float(os.getenv("THROTTLE_SECONDS", "4"))

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY not set. Copy .env.example to .env and fill it in.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Prefab subprocess — the agent owns its lifecycle.
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
GENERATED = HERE / "generated_dashboard.py"
LOG_PATH = HERE / "prefab_server.log"

_PLACEHOLDER_DASHBOARD = """\
# Placeholder shown until the agent calls travel_dashboard().
from prefab_ui.app import PrefabApp
from prefab_ui.components import Card, CardContent, CardHeader, CardTitle, Muted

with PrefabApp(css_class="max-w-md mx-auto p-6") as app:
    with Card():
        with CardHeader():
            CardTitle("Travel Atlas")
        with CardContent():
            Muted("Waiting for the agent to call travel_dashboard()...")
"""


class PrefabServer:
    def __init__(self, target: Path, log_path: Path):
        self.target = target
        self.log_path = log_path
        self._proc: subprocess.Popen | None = None
        self._log = None

    def start(self) -> None:
        self._log = open(self.log_path, "a", encoding="utf-8")
        self._log.write("\n===== restart =====\n")
        self._log.flush()
        self._proc = subprocess.Popen(
            ["prefab", "serve", str(self.target)],
            cwd=self.target.parent,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self._proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def restart(self) -> None:
        self.stop()
        self.start()


prefab_server = PrefabServer(GENERATED, LOG_PATH)


# ---------------------------------------------------------------------------
# Tool wrappers — verbose logging + Prefab subprocess restart on tool 3.
# ---------------------------------------------------------------------------


def _log(prefix: str, payload: str) -> None:
    print(f"  [agent] {prefix}: {payload}", flush=True)


def tool_find_destinations(month: str, travel_style: str) -> str:
    _log("→ find_destinations", f"month={month!r} travel_style={travel_style!r}")
    try:
        result = mcp_server.find_destinations(month=month, travel_style=travel_style)
    except Exception as e:
        msg = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        _log("← find_destinations", json.dumps(msg))
        return json.dumps(msg)

    names = [c.get("name", "") for c in result.get("countries", [])]
    _log("← find_destinations", f"{len(names)} countries: {', '.join(names)}")
    return json.dumps(result)


def tool_recommendations_crud(
    action: str,
    month: str = "",
    travel_style: str = "",
    countries: list | None = None,
    reasons: dict | None = None,
) -> str:
    _log(
        "→ recommendations_crud",
        f"action={action!r} month={month!r} style={travel_style!r} "
        f"countries={countries!r}",
    )
    result = mcp_server.recommendations_crud(
        action=action,
        month=month,
        travel_style=travel_style,
        countries=countries,
        reasons=reasons,
    )
    # Trim verbose payload before logging.
    summary = {"ok": result.get("ok"), "action": result.get("action")}
    if "error" in result:
        summary["error"] = result["error"]
    if "data" in result:
        summary["keys"] = list(result["data"].keys()) if isinstance(result["data"], dict) else result["data"]
    _log("← recommendations_crud", json.dumps(summary))
    return json.dumps(result)


def tool_travel_dashboard(month: str = "", travel_style: str = "") -> str:
    _log(
        "→ travel_dashboard",
        f"month={month!r} travel_style={travel_style!r} "
        "(rebuilding generated_dashboard.py and bouncing prefab serve)",
    )
    source = mcp_server.build_dashboard_source(month=month, travel_style=travel_style)
    compile(source, "<generated_dashboard>", "exec")
    GENERATED.write_text(source, encoding="utf-8")
    prefab_server.restart()
    time.sleep(1.5)
    msg = {
        "ok": True,
        "url": "http://127.0.0.1:5175",
        "highlighted": (
            f"{month}__{travel_style.lower()}" if (month and travel_style) else None
        ),
        "file": str(GENERATED),
        "note": "Open the URL in your browser. The matching tab is the default.",
    }
    _log("← travel_dashboard", json.dumps(msg))
    return json.dumps(msg)


TOOLS = {
    "find_destinations": tool_find_destinations,
    "recommendations_crud": tool_recommendations_crud,
    "travel_dashboard": tool_travel_dashboard,
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


VALID_STYLES = ("adventure", "beach", "cultural", "food", "romantic", "family", "nature")


SYSTEM_PROMPT = f"""You are Travel Atlas, an agent that helps a user plan a
trip from India by recommending countries, persisting them, and rendering an
interactive Prefab dashboard.

You have THREE tools, in this exact contract:

1) find_destinations(month: str, travel_style: str) -> dict
   Asks Gemini for 6 curated country recommendations matched to the month and
   travel style. travel_style MUST be one of {list(VALID_STYLES)}.
   Example: {{"tool_name": "find_destinations", "tool_arguments": {{"month": "December", "travel_style": "adventure"}}}}

2) recommendations_crud(action: str, month: str = "", travel_style: str = "",
                        countries: list | None = None, reasons: dict | None = None) -> dict
   CRUD on recommendations.json. action ∈ {{"create","read","update","delete","list"}}.
   On create/update, pass `countries` as the LIST OF COUNTRY NAMES returned by
   find_destinations (e.g. ["Switzerland", "Norway", ...]). Optionally pass
   `reasons` as a {{name: reason}} mapping if you want the dashboard to show
   "why this country" lines. The tool enriches each country live from REST
   Countries before saving.
   Example: {{"tool_name": "recommendations_crud", "tool_arguments": {{"action": "create", "month": "December", "travel_style": "adventure", "countries": ["Switzerland", "Norway"]}}}}

3) travel_dashboard(month: str = "", travel_style: str = "") -> dict
   Re-renders the Prefab UI at http://127.0.0.1:5175 from recommendations.json.
   When you pass month + travel_style that match a saved set, that set's tab
   is highlighted (default open).
   Example: {{"tool_name": "travel_dashboard", "tool_arguments": {{"month": "December", "travel_style": "adventure"}}}}

You must respond in ONE of these two JSON formats:

If you need a tool:
  {{"tool_name": "<name>", "tool_arguments": {{...}}}}

If you have the final answer:
  {{"answer": "<short user-facing summary>"}}

RULES:
- Respond with ONLY the JSON object. No markdown fences, no commentary.
- Use ONE tool per turn. The harness will execute it and feed you the result.
- Required tool ordering: find_destinations → recommendations_crud(create) →
  travel_dashboard. Always follow that order on a fresh ask.
- Pass `countries` as a list of plain country-name strings (you can pass the
  reasons separately via the `reasons` dict if you want them on the dashboard).
- After travel_dashboard returns ok, your final answer should mention the URL
  and which (month, style) tab is highlighted.
"""


# ---------------------------------------------------------------------------
# LLM call + parser
# ---------------------------------------------------------------------------


def call_llm(prompt: str) -> str:
    if THROTTLE_SECONDS > 0:
        print(f"  [agent] (throttle {THROTTLE_SECONDS:g}s before LLM call)", flush=True)
        time.sleep(THROTTLE_SECONDS)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text or ""


def parse_llm_response(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    raise ValueError(f"could not parse LLM response: {text[:200]}")


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def run_agent(user_query: str, max_iterations: int = 10) -> str | None:
    print(f"\n{'=' * 70}")
    print(f"  USER: {user_query}")
    print(f"{'=' * 70}")

    messages: list[tuple[str, str]] = [
        ("system", SYSTEM_PROMPT),
        ("user", user_query),
    ]

    for iteration in range(1, max_iterations + 1):
        print(f"\n--- Iteration {iteration} ---")

        prompt_parts: list[str] = []
        for role, content in messages:
            if role == "system":
                prompt_parts.append(content)
            elif role == "user":
                prompt_parts.append(f"User: {content}")
            elif role == "assistant":
                prompt_parts.append(f"Assistant: {content}")
            elif role == "tool":
                prompt_parts.append(f"Tool Result: {content}")
        prompt = "\n\n".join(prompt_parts)

        raw = call_llm(prompt)
        print(f"  [llm] {raw.strip()[:400]}")

        try:
            parsed = parse_llm_response(raw)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"  [agent] parse error: {e}; asking LLM to retry")
            messages.append(("assistant", raw))
            messages.append(("user", "Respond with valid JSON only. No markdown."))
            continue

        if "answer" in parsed:
            print(f"\n{'=' * 70}")
            print(f"  AGENT: {parsed['answer']}")
            print(f"{'=' * 70}")
            return parsed["answer"]

        tool_name = parsed.get("tool_name")
        tool_args = parsed.get("tool_arguments", {}) or {}
        if tool_name not in TOOLS:
            err = json.dumps({"error": f"unknown tool {tool_name!r}; available: {list(TOOLS)}"})
            messages.append(("assistant", raw))
            messages.append(("tool", err))
            continue

        try:
            tool_result = TOOLS[tool_name](**tool_args)
        except TypeError as e:
            tool_result = json.dumps({"error": f"bad arguments for {tool_name}: {e}"})
            print(f"  [agent] {tool_result}")
        except Exception as e:
            tool_result = json.dumps({"error": f"{type(e).__name__}: {e}"})
            print(f"  [agent] tool raised: {tool_result}")

        messages.append(("assistant", raw))
        messages.append(("tool", tool_result))

    print("\n  [agent] hit max iterations without a final answer.")
    return None


# ---------------------------------------------------------------------------
# Guided mode — explicit prompts for month + travel style.
# ---------------------------------------------------------------------------


def _ask_month() -> str:
    while True:
        s = input("Which month would you like to travel? "
                  "(e.g. December, dec, 12)\n> ").strip()
        try:
            return mcp_server._normalise_month(s)
        except ValueError as e:
            print(f"  ! {e}; try again.")


def _ask_style() -> str:
    pretty = " | ".join(VALID_STYLES)
    while True:
        s = input(f"Pick a travel style ({pretty})\n> ").strip().lower()
        if s in VALID_STYLES:
            return s
        print(f"  ! must be one of {VALID_STYLES}; try again.")


def run_guided() -> None:
    month = _ask_month()
    style = _ask_style()
    user_query = (
        f"I want to plan a trip from India in {month} with a {style} travel style. "
        f"Find me 6 country recommendations, save them to recommendations.json, "
        f"then show me the travel dashboard with that tab highlighted."
    )
    run_agent(user_query)


# ---------------------------------------------------------------------------
# Main REPL
# ---------------------------------------------------------------------------


def main() -> None:
    print("Travel Atlas Agent — guided LLM curation + REST Countries + Prefab UI")
    print("Tools: find_destinations, recommendations_crud, travel_dashboard")
    print(f"Model: {GEMINI_MODEL}")

    if not GENERATED.exists():
        GENERATED.write_text(_PLACEHOLDER_DASHBOARD, encoding="utf-8")
    LOG_PATH.write_text("")

    print(f"\nStarting `prefab serve {GENERATED.name}` (logs → {LOG_PATH.name}) ...")
    prefab_server.start()
    time.sleep(1.5)
    print("Open http://127.0.0.1:5175 in your browser.\n")

    try:
        while True:
            try:
                prompt = input(
                    "\nPress Enter for guided mode (asks Month + Style), "
                    "or type a free-text request, or 'quit':\n> "
                ).strip()
            except EOFError:
                break
            if prompt.lower() in {"quit", "exit"}:
                break
            if not prompt or prompt.lower() == "guided":
                run_guided()
            else:
                run_agent(prompt)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down prefab serve ...")
        prefab_server.stop()


if __name__ == "__main__":
    main()

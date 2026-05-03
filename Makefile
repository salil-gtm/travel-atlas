.PHONY: install run preview verify clean help

PY ?= python3

help:
	@echo "Travel Atlas — common dev commands"
	@echo ""
	@echo "  make install   pip install -r requirements.txt"
	@echo "  make run       python agent.py"
	@echo "  make preview   fastmcp dev apps server.py (Prefab-aware MCP previewer)"
	@echo "  make verify    syntax-parse server.py and agent.py"
	@echo "  make clean     remove runtime artifacts (recommendations.json, logs, caches)"

install:
	$(PY) -m pip install -r requirements.txt

run:
	$(PY) agent.py

preview:
	fastmcp dev apps server.py

verify:
	@$(PY) -c "import ast; ast.parse(open('server.py').read()); print('server.py: parses OK')"
	@$(PY) -c "import ast; ast.parse(open('agent.py').read());  print('agent.py:  parses OK')"

clean:
	rm -rf __pycache__ .pytest_cache .mypy_cache .ruff_cache
	rm -f recommendations.json generated_dashboard.py .last_good_dashboard.py prefab_server.log agent_run.log
	rm -f trips.json
	@echo "Cleaned runtime artifacts."

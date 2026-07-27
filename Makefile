.PHONY: setup fmt lint typecheck test check demo demo-claude demo-ophyd demo-ophyd-claude \
	demo-pylabrobot demo-pylabrobot-claude

setup:  ## Install Python + all workspace packages + dev tools
	uv sync --all-packages

fmt:  ## Auto-format code
	uv run ruff format .

lint:  ## Check formatting and lint rules
	uv run ruff format --check .
	uv run ruff check .

typecheck:  ## Run pyright strict
	uv run pyright

test:  ## Run tests with coverage (fails under 85% on labwire.core)
	uv run pytest --cov --cov-report=term-missing

check: lint typecheck test  ## Everything CI runs

demo:  ## Closed-loop optimizer over pump + PSU + balance, signed evidence
	uv run python examples/demo/closed_loop.py

demo-claude:  ## Same loop planned live by Claude (needs ANTHROPIC_API_KEY)
	uv run python examples/demo/claude_agent.py

demo-ophyd:  ## Peak-finding scan over ophyd.sim devices bridged into Labwire
	uv run python examples/ophyd_scan/scan.py

demo-ophyd-claude:  ## The same scan planned by Claude (needs ANTHROPIC_API_KEY)
	uv run python examples/ophyd_scan/claude_scan.py

demo-pylabrobot:  ## Serial dilution on a simulated PyLabRobot liquid handler
	uv run python examples/liquid_handling/dilution.py

demo-pylabrobot-claude:  ## The same dilution planned by Claude (needs ANTHROPIC_API_KEY)
	uv run python examples/liquid_handling/claude_dilution.py

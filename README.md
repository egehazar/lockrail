# Lockrail

A transactional runtime that wraps every LLM-agent MCP tool call in a gate pipeline — idempotency, evidence validation, policy evaluation, human approval — before any side effect lands.

[Portfolio writeup](./docs/portfolio-post.md) · [Architecture](./ARCHITECTURE.md) · [Eval methodology](./evals/README.md)

## What it is

Lockrail sits between an LLM agent and its MCP tools. Every tool call passes through an ordered gate pipeline; decisions are event-sourced into a Postgres audit log; idempotency uses Redis with TTL. The Runtime accepts an injected executor — the same code serves an MCP middleware, a FastAPI route, or a direct Python integration.

## Headline results

From the eval suite (`evals/`), reproducible in under five seconds:

- **Unsafe writes**: 23.6% → 0% (33 → 0 of 140 scenarios)
- **Duplicate prevention**: 95.0% of 200 webhook replays prevented
- **Task completion (dedicated set)**: 61.0% → 81.0% (+20pp, regression-pinned)
- **Test suite**: 98 passing — 69 unit + 29 integration

Full methodology and the case for why two completion measurements exist on two scenario sets: [`evals/README.md`](./evals/README.md).

## Quickstart

```bash
# install
uv sync

# bring up Postgres + Redis, apply migrations
docker compose -f docker/docker-compose.yml up -d
uv run alembic upgrade head

# demo the HITL approval cycle
uv run python scripts/demo_transaction.py

# reproduce the headline numbers
uv run python evals/run.py --metric all --output evals/results/report.md

# run as an MCP server (stdio)
uv run python scripts/run_mcp_server.py
```

## Stack

Python 3.12 · FastAPI · Pydantic v2 · MCP · SQLAlchemy 2.0 (async) · Postgres + Redis · Alembic · LangGraph / PydanticAI compatible

## Repo layout

```
src/lockrail/             Runtime, gates, storage, MCP server, FastAPI surface
evals/                    150 + 100 scenario sets, harness, methodology
tests/                    69 unit + 29 integration
docs/portfolio-post.md    Narrative writeup
ARCHITECTURE.md           Design decisions and tradeoffs
migrations/               Alembic schema migrations
docker/                   Postgres + Redis compose
```

## License

[MIT](./LICENSE)

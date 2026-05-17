#!/usr/bin/env bash
# Capture portfolio-post evidence into ./docs/post-assets/.
# Prereqs: docker-compose stack up (postgres + redis), migrations applied,
# Python deps installed via `uv sync`.
#
# Usage from repo root:
#   bash docs/post-assets/capture.sh
#
# Optional: also run `uv run uvicorn lockrail.api.app:create_app --factory --reload`
# in another shell and screenshot http://localhost:8000/docs into swagger.png.

set -euo pipefail

OUT="docs/post-assets"
mkdir -p "$OUT"

echo "==> 1/4 demo cycle"
uv run python scripts/demo_transaction.py 2>&1 | tee "$OUT/demo-output.txt"

echo
echo "==> 2/4 cross-transaction audit trail"
docker exec lockrail-postgres psql -U lockrail -d lockrail -A -F $'\t' -c "
  SELECT
    t.status,
    ae.sequence_num,
    ae.event_type,
    ae.payload->>'gate_name'  AS gate,
    ae.payload->>'decision'   AS decision,
    ae.payload->>'resumed_from' AS resumed_from
  FROM transactions t
  JOIN audit_events ae ON ae.transaction_id = t.transaction_id
  ORDER BY t.started_at, ae.sequence_num;
" 2>&1 | tee "$OUT/audit-trail.txt"

echo
echo "==> 3/4 approvals queue"
docker exec lockrail-postgres psql -U lockrail -d lockrail -c "
  SELECT id, tool_name, status, resolver_id, requested_reason
  FROM approvals
  ORDER BY requested_at DESC;
" 2>&1 | tee "$OUT/approvals.txt"

echo
echo "==> 4/4 test suite"
uv run pytest tests/ -v 2>&1 | tail -30 | tee "$OUT/tests.txt"

echo
echo "Done. Captures saved to $OUT/."
echo "Manual: start the API and screenshot http://localhost:8000/docs into $OUT/swagger.png."
echo "  uv run uvicorn lockrail.api.app:create_app --factory --reload"

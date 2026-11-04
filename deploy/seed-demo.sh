#!/usr/bin/env bash
# Fill a freshly deployed system with the demo dataset and run one full analysis, so the web
# interface has something to show. Safe to re-run: it reloads the same seeded dataset.
#   ./deploy/seed-demo.sh [events]        (default 1,000,000 events over 2023-2025)
# Set ENV_FILE=.env.prod if your settings are not in .env.
set -euo pipefail
cd "$(dirname "$0")/.."
EVENTS="${1:-1000000}"
C="docker compose ${ENV_FILE:+--env-file $ENV_FILE} -f docker-compose.prod.yml"
# Right after Docker is installed the new group membership is not active yet; fall back to sudo.
if ! docker info >/dev/null 2>&1; then C="sudo $C"; fi

echo "==> generating $EVENTS events (seed 42)"
$C --profile tools run --rm tools python -m scripts.generate --events "$EVENTS" --start 2023-01-01 --end 2025-12-31 --seed 42 --out data/demo.csv --anomalies data/demo_truth.json
echo "==> loading into the database"
$C --profile tools run --rm tools python -m scripts.load_csv data/demo.csv --truncate
echo "==> running the full analysis"
$C --profile tools run --rm tools python -m scripts.run_analysis --start 2023-01-01 --end 2025-12-31
echo "==> done"

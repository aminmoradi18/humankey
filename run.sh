#!/bin/bash
# HumanKey in one command.
#   ./run.sh sample    no Zetaris, no model, no keys: replays a recorded run (for judges)
#   ./run.sh live      Zetaris + model provider from .env: investigates every invoice for real
#   ./run.sh setup     first-time setup for live mode (ERP database + Zetaris sources)
#   ./run.sh test      unit tests + Meterless conformance suites
# Then open http://127.0.0.1:8700 (the web view starts at the end of sample/live).
set -euo pipefail
cd "$(dirname "$0")"
MODE=${1:-sample}

need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)"; exit 1; }; }
need python3 "Python 3.10+"
need node "Node.js 20+ for the Meterless engines"
python3 -c "import cryptography" 2>/dev/null || { echo "missing Python package: pip install -r requirements.txt"; exit 1; }
[ -d meterless/node_modules ] || (cd meterless && npm install --no-audit --no-fund --silent)
[ -f .env ] || cp .env.example .env

case "$MODE" in
  setup)
    ./scripts/erp_db.sh
    python3 scripts/setup_zetaris.py
    ;;
  test)
    python3 -m unittest discover -s tests -t .
    (cd meterless && npm run -s conformance:world && npm run -s conformance:hmem)
    ;;
  sample|live)
    if [ "$MODE" = sample ]; then
      export SAMPLE_MODE=true
      export HUMANKEY_LOGS="$PWD/logs/sample"
      export WORLD_PORT=8712
      export WORLD_DIR="$PWD/logs/sample/world" HMEM_DIR="$PWD/logs/sample/hmem"
      rm -rf logs/sample
    fi
    python3 -m gate.main enroll-emulator >/dev/null
    python3 -m agent.run
    python3 -m eval.run | sed -n '1,8p'
    echo
    echo "Web view: http://127.0.0.1:8700   (Ctrl+C to stop)"
    python3 -m web.server
    ;;
  *) echo "usage: ./run.sh [sample|live|setup|test]"; exit 2 ;;
esac

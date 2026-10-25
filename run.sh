#!/bin/bash
# HumanKey in one command.
#   ./run.sh           same as ./run.sh live (the default runs the real agents)
#   ./run.sh live      Zetaris + model provider from .env: investigates every invoice for real
#   ./run.sh offline   no Zetaris: recorded query results, but every model call is made for real
#                      (Nightshift, or any provider set in .env, e.g. a local Ollama Nemotron)
#   ./run.sh sample    no Zetaris, no model, no keys: recorded query results and model answers, with
#                      all agent, critic, risk and gate logic running for real (for judges)
#   ./run.sh setup     first-time setup for live mode (ERP database + Zetaris sources)
#   ./run.sh test      unit tests + Meterless conformance suites
# Then open http://127.0.0.1:8700 (the web view starts at the end of sample/live).
set -euo pipefail
cd "$(dirname "$0")"
MODE=${1:-live}

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
  sample|offline|live)
    if [ "$MODE" = offline ]; then
      # Recorded Zetaris answers, live model calls: the agents reason anew on every run.
      export ZETARIS_MODE=snapshot LLM_USE_CACHE=false DEVICE=${DEVICE:-emulator}
      export HUMANKEY_LLM_CACHE="$PWD/logs/offline/llm_cache"   # fresh answers never overwrite the recording
    fi
    if [ "$MODE" != live ]; then
      # A world server left from an earlier run would point at a deleted folder.
      command -v fuser >/dev/null && fuser -k 8712/tcp >/dev/null 2>&1 || true
    fi
    if [ "$MODE" = sample ] || [ "$MODE" = offline ]; then
      # Everything for the replay lives in logs/sample, so a judge's run starts clean every time
      # and never touches live state: own logs, gate state, emulator key and memory, World Model.
      [ "$MODE" = sample ] && export SAMPLE_MODE=true DEVICE=emulator
      S="$PWD/logs/$MODE"
      export HUMANKEY_LOGS="$S" HUMANKEY_STATE="$S/gate" HUMANKEY_EMU_DIR="$S/emulator"
      export WORLD_PORT=8712 WORLD_DIR="$S/world" HMEM_DIR="$S/hmem"
      rm -rf "$S"
    fi
    python3 -m gate.main enroll-emulator >/dev/null
    if [ "$MODE" != live ]; then
      # The emulator's own account memory, from payment history (auto-confirmed in sample mode).
      # With the hardware key this is a one-time step: python3 -m gate.main import-accounts
      DEVICE_SCRIPT=approve python3 -m gate.main import-accounts >/dev/null
    fi
    python3 -m agent.run
    python3 -m gate.main night   # Scout contracts for what waits for a person; re-planned at approval
    python3 -m eval.run | sed -n '1,8p'
    echo
    echo "Web view: http://127.0.0.1:${WEB_PORT:-8700}   (Ctrl+C to stop)"
    python3 -m web.server
    ;;
  *) echo "usage: ./run.sh [live|offline|sample|setup|test]"; exit 2 ;;
esac

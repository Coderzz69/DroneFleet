#!/usr/bin/env bash
# Start Ollama (if installed here) and the simulation with the local model.
set -e
export PATH="$HOME/.local/ollama/bin:$PATH"
MODEL="${FLEET_LLM_MODEL:-gemma2:2b}"

if command -v ollama >/dev/null; then
  curl -sf http://127.0.0.1:11434/api/version >/dev/null || {
    echo "starting ollama…"; (setsid nohup ollama serve >/tmp/ollama.log 2>&1 &); sleep 3; }
  ollama list | grep -q "${MODEL%%:*}" || ollama pull "$MODEL"
  exec python3 run.py --llm --llm-model "$MODEL" "$@"
fi
echo "ollama not found — running with the deterministic parser"
exec python3 run.py "$@"

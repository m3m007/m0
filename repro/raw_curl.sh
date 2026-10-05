#!/usr/bin/env bash
# Probe-first recipe: raw curl BOTH Token Plan endpoints before wiring anything.
#
#   ./repro/raw_curl.sh                # both endpoints, one tiny tool call each
#   ./repro/raw_curl.sh anthropic      # just one
#   VERBOSE=1 ./repro/raw_curl.sh      # also dump the (redacted) request/response
#
# WHY THIS EXISTS. The brief's central lesson: both endpoints were healthy all
# along and our SDK usage was not. Two cheap curls would have shown that in 30
# seconds and saved a whole investigation. Run this BEFORE blaming a model, a
# provider, or the network.
#
# SECRETS. The key is resolved by python via lib/keys.py and written to a
# 0600 curl config file that is deleted on exit. It is NEVER placed on a curl
# command line: argv is visible to `ps`, to process accounting, and to shell
# history. Requests and responses land under runs/ (gitignored).
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${MULTIAGENT_PY:-/home/hermes/.hermes/cache/scratch/oh-venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

HOST="token-plan.ap-southeast-1.maas.aliyuncs.com"
OUT="runs/repro/raw"
mkdir -p "$OUT"
CFG="$OUT/curl.cfg"
trap 'rm -f "$CFG"' EXIT

# Write the auth header into a 0600 config file (never argv).
umask 077
"$PY" - "$CFG" <<'PYEOF'
import sys
sys.path.insert(0, ".")
from lib.keys import token_plan_key
path = sys.argv[1]
key = token_plan_key()
with open(path, "w", encoding="utf-8") as fh:
    fh.write('header = "Authorization: Bearer %s"\n' % key)
    fh.write('header = "x-api-key: %s"\n' % key)
    fh.write('header = "anthropic-version: 2023-06-01"\n')
    fh.write('header = "content-type: application/json"\n')
PYEOF
chmod 600 "$CFG"

MODEL="${MODEL:-glm-5.3}"
PROMPT="What is the weather in Osaka right now? Use the get_weather tool."

write_body_openai() {  # $1 = out file -- OpenAI chat-completions shape
  cat > "$1" <<JSON
{
  "model": "${MODEL}",
  "messages": [{"role": "user", "content": "${PROMPT}"}],
  "tools": [{"type": "function", "function": {
      "name": "get_weather",
      "description": "Look up current weather for a city.",
      "parameters": {"type": "object",
                     "properties": {"city": {"type": "string"}},
                     "required": ["city"]}}}],
  "tool_choice": "auto",
  "max_tokens": 512
}
JSON
}

write_body_anthropic() {  # $1 = out file -- Anthropic messages shape
  # NOTE: this endpoint is ANTHROPIC-NATIVE. Tools use input_schema (not
  # OpenAI's "parameters"), and there is no {"type":"function"} wrapper.
  # Sending OpenAI shape here yields HTTP 400 "Request body format invalid"
  # -- which is OUR request bug, not an endpoint limitation. LiteLLM does
  # this translation for us; raw curl has to get it right by hand.
  cat > "$1" <<JSON
{
  "model": "${MODEL}",
  "max_tokens": 512,
  "messages": [{"role": "user", "content": "${PROMPT}"}],
  "tools": [{"name": "get_weather",
             "description": "Look up current weather for a city.",
             "input_schema": {"type": "object",
                              "properties": {"city": {"type": "string"}},
                              "required": ["city"]}}],
  "tool_choice": {"type": "auto"}
}
JSON
}

report() {  # $1 = transport  $2 = response file
  "$PY" - "$1" "$2" <<'PYEOF'
import json, sys
transport, path = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path))
except Exception as exc:
    print(f"  UNPARSEABLE RESPONSE ({type(exc).__name__}); see {path}")
    sys.exit(1)
if transport == "openai":
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    tcs = msg.get("tool_calls") or []
    print(f"  finish={ch.get('finish_reason')} tool_calls={len(tcs)} "
          f"content_len={len(msg.get('content') or '')} "
          f"reasoning={'yes' if msg.get('reasoning_content') else 'no'}")
    if tcs:
        fn = tcs[0].get("function", {})
        print(f"  TOOL: {fn.get('name')}({fn.get('arguments')})")
    print("  VERDICT: " + ("OK tool call" if tcs else "NO TOOL CALL"))
else:
    blocks = d.get("content") or []
    kinds = [b.get("type") for b in blocks if isinstance(b, dict)]
    tcs = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
    print(f"  stop_reason={d.get('stop_reason')} blocks={kinds} tool_use={len(tcs)}")
    if tcs:
        print(f"  TOOL: {tcs[0].get('name')}({json.dumps(tcs[0].get('input'))})")
    print("  VERDICT: " + ("OK tool_use" if tcs else "NO TOOL USE"))
PYEOF
}

probe_openai() {
  local body="$OUT/openai_${MODEL}.json"
  write_body_openai "$OUT/openai_request.json"
  echo "-- OpenAI compatible-mode / ${MODEL}"
  curl -sS --fail-with-body -m 90 --config "$CFG" \
    -d "@$OUT/openai_request.json" \
    "https://${HOST}/compatible-mode/v1/chat/completions" \
    -o "$body" -w "   http=%{http_code} time=%{time_total}s\n" || {
      echo "   OPENAI PROBE FAILED -> $body"; return 1; }
  report openai "$body"
}

probe_anthropic() {
  local body="$OUT/anthropic_${MODEL}.json"
  write_body_anthropic "$OUT/anthropic_request.json"
  echo "-- Anthropic messages / ${MODEL}"
  curl -sS --fail-with-body -m 90 --config "$CFG" \
    -d "@$OUT/anthropic_request.json" \
    "https://${HOST}/apps/anthropic/v1/messages" \
    -o "$body" -w "   http=%{http_code} time=%{time_total}s\n" || {
      echo "   ANTHROPIC PROBE FAILED -> $body"; return 1; }
  report anthropic "$body"
}

echo "== probe-first: Token Plan, ONE key, TWO endpoints, model=${MODEL} =="
rc=0
case "${1:-both}" in
  openai)    probe_openai || rc=1 ;;
  anthropic) probe_anthropic || rc=1 ;;
  both)      probe_openai || rc=1; probe_anthropic || rc=1 ;;
  *) echo "usage: $0 [openai|anthropic|both]"; exit 2 ;;
esac

if [ "${VERBOSE:-0}" = "1" ]; then
  for f in "$OUT"/openai_request.json "$OUT"/anthropic_request.json \
           "$OUT"/openai_"$MODEL".json "$OUT"/anthropic_"$MODEL".json; do
    [ -f "$f" ] || continue
    echo "---- $f"
    "$PY" -c "
import json, sys; sys.path.insert(0, '.')
from lib.keys import redact
print(json.dumps(redact(json.load(open('$f'))), indent=1)[:1500])
" 2>/dev/null || echo "   (not JSON)"
  done
fi
echo "cost: 2 API calls for 'both'. Responses in $OUT/ (gitignored, scrub before sharing)"
exit $rc

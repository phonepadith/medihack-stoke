#!/usr/bin/env bash
# Wire the FreeLLMAPI unified API key into the vitals service.
#
#   ./set-llm-key.sh sk-xxxxxxxx        (or run with no argument to be prompted)
#
# The key is written to ~/vitalsigns/.llm-env with 0600 permissions and read by
# autostart.sh on every start, so it survives restarts and reboots. It is never
# baked into the image.
set -eu

KEY="${1:-}"
if [ -z "$KEY" ]; then
    printf 'FreeLLMAPI unified API key (Keys page header): '
    read -r KEY
fi
[ -n "$KEY" ] || { echo "no key given" >&2; exit 1; }

ENVFILE="$HOME/vitalsigns/.llm-env"
umask 077
cat > "$ENVFILE" <<EOT
LLM_API_KEY=$KEY
LLM_MODEL=${LLM_MODEL:-auto}
EOT
echo "wrote $ENVFILE (0600)"

echo "verifying the key against the gateway…"
code=$(curl -s -o /tmp/llmcheck.json -w '%{http_code}' -m 20 \
       -H "Authorization: Bearer $KEY" http://127.0.0.1:3001/v1/models)
if [ "$code" != "200" ]; then
    echo "gateway rejected the key (HTTP $code):" >&2
    head -c 300 /tmp/llmcheck.json >&2; echo >&2
    exit 1
fi
echo "key accepted. models visible: $(grep -o '"id"' /tmp/llmcheck.json | wc -l)"

echo "restarting the vitals service with the key…"
docker rm -f vitals >/dev/null 2>&1 || true
"$HOME/vitalsigns/autostart.sh"
sleep 6
curl -s http://localhost:8080/api/llm-status; echo
echo "Done. Try the stroke-risk panel with “ໃຫ້ AI ອະທິບາຍ” ticked."

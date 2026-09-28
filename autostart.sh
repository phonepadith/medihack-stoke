#!/usr/bin/env bash
# Ensure the vitals container is running.
#
# Invoked at Windows logon by the "Vitals Monitor autostart (WSL)" scheduled
# task, which boots this WSL distro. systemd then starts docker.service and the
# container's `unless-stopped` policy usually brings it back on its own -- this
# script is the safety net for the cases that policy does not cover: the
# container having been removed, or the image having been rebuilt under a new
# tag. Safe to run by hand at any time.
set -u

IMAGE="${VITALS_IMAGE:-vitals-monitor:2.3}"

# Credentials live outside the image and outside this script. Written by
# set-llm-key.sh; absent on a fresh install, in which case the service still
# runs and simply reports why the AI explanation is unavailable.
LLM_ENV="$HOME/vitalsigns/.llm-env"
if [ -f "$LLM_ENV" ]; then
    # shellcheck disable=SC1090
    . "$LLM_ENV"
fi
NAME="vitals"
PORT="${VITALS_PORT:-8080}"

# The task fires as soon as the distro is up, which can be before dockerd has
# finished starting. Wait rather than fail.
for _ in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 1
done
if ! docker info >/dev/null 2>&1; then
    echo "vitals-autostart: docker daemon did not become ready" >&2
    exit 1
fi

if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "vitals-autostart: already running"
    exit 0
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker start "$NAME" >/dev/null && echo "vitals-autostart: started existing container" && exit 0
    # A stale container that will not start is worse than no container.
    docker rm -f "$NAME" >/dev/null 2>&1
fi

# `clinic` joins this container to the FreeLLMAPI gateway so the stroke-risk
# explanation can reach it by name. The gateway itself stays bound to loopback.
docker network create clinic >/dev/null 2>&1 || true
# vitals-data holds the clinician accounts and the session signing key, so
# recreating the container below does not wipe everyone's login.
docker volume create vitals-data >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "${PORT}:8080" --restart unless-stopped \
    --network clinic \
    -v vitals-data:/data \
    -e LLM_GATEWAY="${LLM_GATEWAY:-http://freellmapi:3001/v1/chat/completions}" \
    -e LLM_API_KEY="${LLM_API_KEY:-}" \
    -e LLM_MODEL="${LLM_MODEL:-auto}" \
    -e SEALION_API_KEY="${SEALION_API_KEY:-}" \
    -e SEALION_MODEL="${SEALION_MODEL:-aisingapore/Gemma-SEA-LION-v4-27B-IT}" \
    -e REGISTER_CODE="${REGISTER_CODE:-}" \
    "$IMAGE" >/dev/null \
    && echo "vitals-autostart: created and started container" \
    || { echo "vitals-autostart: failed to start $IMAGE" >&2; exit 1; }

#!/bin/bash
# The console's image builds from console/Dockerfile with runner/ as setup.sh hands it over, and the console in it
# answers: as a plain user, with a read-only root, no capabilities, and a secret file replaced by an empty one.
# Needs Docker and the network (the base image, PyPI). Exits 77 (skipped) without Docker.
#   bash setup/tests/test-console-image.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v docker >/dev/null && docker info >/dev/null 2>&1 || { echo "skip: no docker here"; exit 77; }
T="$(mktemp -d)"; NAME="arceus-console-test-$$"; TAG="arceus-console:test-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; docker image rm -f "$TAG" >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT
trap 'exit 130' INT TERM HUP  # killed (a timeout): the EXIT trap still cleans up
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

# The build folder exactly as apply_console makes it: runner/ with pyproject.toml and src/, and extra/ with the
# wheels of setup.conf's RUNNER_EXTRA_PACKAGES, nothing else. First with none, as on most installs.
mkdir -p "$T/build/runner" "$T/build/extra" "$T/data"
rsync -a --exclude __pycache__ --exclude '*.egg-info' --exclude build \
  "$ROOT/runner/pyproject.toml" "$ROOT/runner/src" "$T/build/runner/"
docker build --quiet --label arceus.keep=true -t "$TAG" -f "$ROOT/console/Dockerfile" "$T/build" > "$T/build.log" 2>&1
check "the image builds from console/Dockerfile and runner/, with no extra package" "[ $? = 0 ]" || cat "$T/build.log"

# Then with one: a provider kept outside this repository, as a wheel built the way apply_extra_packages builds it.
mkdir -p "$T/acme/src/acme_register"
printf '[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n[project]\nname = "acme-register"\nversion = "0.1.0"\n' \
  > "$T/acme/pyproject.toml"
echo 'REGISTER = "acme"' > "$T/acme/src/acme_register/__init__.py"
"$ROOT/runner/.venv/bin/pip" wheel --quiet --disable-pip-version-check --no-deps --wheel-dir "$T/build/extra" "$T/acme" \
  > "$T/wheel.log" 2>&1 || cat "$T/wheel.log"
docker build --quiet --label arceus.keep=true -t "$TAG-extra" -f "$ROOT/console/Dockerfile" "$T/build" > "$T/build.log" 2>&1
check "with a wheel in extra/, the image builds" "[ $? = 0 ]" || cat "$T/build.log"
check "and the console's Python imports it" \
  "[ \"\$(docker run --rm --entrypoint /opt/console/bin/python $TAG-extra -c 'import acme_register; print(acme_register.REGISTER)')\" = acme ]"
docker image rm -f "$TAG-extra" >/dev/null 2>&1

echo "GITLAB_TOKEN=glpat-secret" > "$T/data/secrets.env"
chmod -R a+rwX "$T/data"
docker run -d --name "$NAME" --read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /tmp \
  --user 1000:1000 -e HOME=/home/ai -e ARCEUS_RUNNER_CONFIG="$T/data/config.yaml" -v "$T/data:$T/data" \
  --mount "type=bind,source=/dev/null,target=$T/data/secrets.env,readonly" "$TAG" \
  console serve --host 127.0.0.1 --port 8180 >/dev/null
up=1
for _ in $(seq 1 20); do
  docker exec "$NAME" /opt/console/bin/python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8180/healthz', timeout=2)" 2>/dev/null && { up=0; break; }
  sleep 1
done
check "the console answers inside the container" "[ $up = 0 ]" || docker logs "$NAME" 2>&1 | tail -20
login="$(docker exec "$NAME" /opt/console/bin/python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8180/login', timeout=2).read().decode())" 2>&1)"
check "its sign-in page renders from the packaged templates" "grep -q 'Sign in' <<< \"\$login\""
check "it runs as the given user, not root" "[ \"\$(docker exec $NAME id -u)\" = 1000 ]"
check "it has no sudo" "! docker exec $NAME sh -c 'command -v sudo' >/dev/null"
check "it made console.db in the data folder" "[ -f '$T/data/console/console.db' ]"
check "the secret file reads as empty" "[ -z \"\$(docker exec $NAME cat '$T/data/secrets.env')\" ]"
check "the root is read-only" "! docker exec $NAME sh -c 'touch /opt/console/x' 2>/dev/null"

[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
echo "$fails failed"; exit 1

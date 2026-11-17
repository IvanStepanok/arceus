#!/bin/bash
# setup.conf's RUNNER_EXTRA_PACKAGES with a real pip and setuptools: a package in a folder on the machine is built
# from a clean copy, so a module removed from it is gone from the wheel and from what is installed, even when an
# earlier build left the module in the folder's build/lib. apply_extra_packages is taken from setup.sh as it is.
# Needs the network (setuptools from PyPI, as pip builds a package); exits 77 (skipped) without it. No root.
#   bash setup/tests/test-extra-build.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d /tmp/arceus-extra-build.XXXXXX)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

eval "$(sed -n '/^extra_packages() {/,/^}/p; /^extra_names() {/,/^}/p; /^apply_extra_packages() {/,/^}/p' \
  "$HERE/../setup.sh")"
type extra_packages extra_names apply_extra_packages >/dev/null || { echo "FAIL the functions are not in setup.sh"; exit 1; }

RUNNER_OPT="$T/opt/arceus-runner"; EXTRA_DIR="$RUNNER_OPT/extra"; EXTRA_INSTALLED="$RUNNER_OPT/.extra-installed"
say() { :; }
die() { printf 'die: %s\n' "$*"; exit 1; }
install() { mkdir -p "${@: -1}"; }   # no chown here

python3 -m venv "$RUNNER_OPT/venv" || { echo "FAIL python3 -m venv"; exit 1; }
PIP="$RUNNER_OPT/venv/bin/pip"
"$PIP" download --quiet --disable-pip-version-check --no-deps --dest "$T/probe" setuptools >/dev/null 2>&1 \
  || { echo "skip: PyPI cannot be reached"; exit 77; }

# A provider in a folder, with two modules; then built once in place, as a person trying it by hand would, which
# leaves both modules in its build/lib.
PKG="$T/acme-tracker"
mkdir -p "$PKG/src/acme_tracker"
printf '[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n[project]\nname = "acme-tracker"\nversion = "0.1.0"\n[tool.setuptools.packages.find]\nwhere = ["src"]\n' \
  > "$PKG/pyproject.toml"
echo 'TRACKER = "acme"' > "$PKG/src/acme_tracker/__init__.py"
echo 'OLD = True' > "$PKG/src/acme_tracker/legacy.py"
"$PIP" wheel --quiet --disable-pip-version-check --no-deps --wheel-dir "$T/by-hand" "$PKG" >/dev/null 2>&1
check "built by hand in the folder, the old module is in its build/lib" "[ -f '$PKG/build/lib/acme_tracker/legacy.py' ]"

RUNNER_EXTRA_PACKAGES="$PKG"
( apply_extra_packages ) > "$T/apply1.log" 2>&1
check "setup builds and installs the folder's package" \
  "[ \"\$(extra_names)\" = acme_tracker ] && '$RUNNER_OPT/venv/bin/python' -c 'import acme_tracker.legacy'" || cat "$T/apply1.log"

rm "$PKG/src/acme_tracker/legacy.py"
( apply_extra_packages ) > "$T/apply2.log" 2>&1
check "the module removed from the folder is not in the new wheel" \
  "! unzip -l '$EXTRA_DIR'/acme_tracker-*.whl | grep -q legacy.py" || cat "$T/apply2.log"
check "nor in what is installed, while the rest is" \
  "'$RUNNER_OPT/venv/bin/python' -c 'import acme_tracker; assert acme_tracker.TRACKER == \"acme\"' \
   && ! '$RUNNER_OPT/venv/bin/python' -c 'import acme_tracker.legacy' 2>/dev/null"
check "setup wrote nothing into the folder: its build/ is the one built by hand" \
  "[ \"\$(ls '$PKG')\" = \"\$(printf 'build\npyproject.toml\nsrc')\" ] && [ -f '$PKG/build/lib/acme_tracker/legacy.py' ]"

[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
echo "$fails failed"; exit 1

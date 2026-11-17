#!/bin/bash
# setup.conf's RUNNER_EXTRA_PACKAGES: an install's own packages (a provider kept outside this repository) are built
# into wheels once, installed into the runner's virtualenv and dropped again when they leave the list; without
# the setting nothing changes, the hashes that decide a reinstall included. The functions are taken from setup.sh
# as they are; pip is a stand-in that records what it was asked. No root, no network.
#   bash setup/tests/test-extra-packages.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d /tmp/arceus-extra-test.XXXXXX)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); fi; }

eval "$(sed -n '/^extra_packages() {/,/^}/p; /^extra_fingerprint() {/,/^}/p; /^with_extra() {/,/^}/p; /^extra_names() {/,/^}/p;
                /^extra_installed_ok() {/,/^}/p; /^apply_extra_packages() {/,/^}/p;
                /^runner_source_hash() {/,/^}/p; /^console_source_hash() {/,/^}/p' "$HERE/../setup.sh")"
type extra_packages extra_fingerprint with_extra apply_extra_packages extra_installed_ok runner_source_hash console_source_hash \
  >/dev/null || { echo "FAIL the functions are not in setup.sh"; exit 1; }

REPO_DIR="$(cd "$HERE/../.." && pwd)"
RUNNER_OPT="$T/opt/arceus-runner"; EXTRA_DIR="$RUNNER_OPT/extra"; EXTRA_INSTALLED="$RUNNER_OPT/.extra-installed"
say() { printf '%s\n' "$*" >> "$T/said"; }
die() { printf 'die: %s\n' "$*" >> "$T/said"; exit 1; }
install() { mkdir -p "${@: -1}"; }   # no chown here

# pip: `wheel` makes <name>-0.1.0-py3-none-any.whl for each requirement (its last path part, as pip names a
# distribution), `install` and `uninstall` keep the installed set in a file, `show` answers from it.
mkdir -p "$RUNNER_OPT/venv/bin"
cat > "$RUNNER_OPT/venv/bin/pip" <<'PIP'
#!/bin/bash
set -u
db="$(dirname "$0")/../installed"; touch "$db"
echo "$*" >> "$(dirname "$0")/../calls"
cmd="$1"; shift
case "$cmd" in
  wheel)
    dir=""; reqs=()
    while [ $# -gt 0 ]; do
      case "$1" in --wheel-dir) dir="$2"; shift ;; -*) ;; *) reqs+=("$1") ;; esac; shift
    done
    for r in "${reqs[@]}"; do
      [ "$r" = broken ] && exit 1
      n="${r%%@*}"; n="${n%/}"; n="${n##*/}"; n="${n//-/_}"
      : > "$dir/$n-0.1.0-py3-none-any.whl"
    done ;;
  install)
    for w in "$@"; do case "$w" in *.whl) n="$(basename "$w" | cut -d- -f1)"; grep -qxF "$n" "$db" || echo "$n" >> "$db" ;; esac; done ;;
  uninstall)
    for n in "$@"; do case "$n" in -*) ;; *) grep -vxF "$n" "$db" > "$db.new"; mv "$db.new" "$db" ;; esac; done ;;
  show)
    for n in "$@"; do case "$n" in -*) ;; *) grep -qxF "$n" "$db" || exit 1 ;; esac; done ;;
esac
PIP
chmod +x "$RUNNER_OPT/venv/bin/pip"
installed() { tr '\n' ' ' < "$RUNNER_OPT/venv/installed" 2>/dev/null | sed 's/ $//'; }

RUNNER_EXTRA_PACKAGES=""
plain_runner="$(cd "$REPO_DIR/runner" && find pyproject.toml src -type f \( -name '*.py' -o -name '*.toml' \) -print0 \
  | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
plain_console="$(cd "$REPO_DIR" && find console runner/pyproject.toml runner/src -type f ! -path '*/__pycache__/*' \
  ! -name '*.pyc' ! -path '*.egg-info*' ! -path 'runner/src/build/*' -print0 | sort -z | xargs -0 sha256sum \
  | sha256sum | cut -c1-16)"
check "without RUNNER_EXTRA_PACKAGES the runner's hash is the sources' alone, so nothing installs again" \
  "[ \"\$(runner_source_hash)\" = '$plain_runner' ]"
check "and so is the console image's tag" "[ \"\$(console_source_hash)\" = '$plain_console' ]"
check "an install that never had extra packages counts as done" "extra_installed_ok"
apply_extra_packages
check "applied without any, it asks pip for nothing and installs nothing" \
  "[ ! -s '$RUNNER_OPT/venv/calls' ] && [ -z \"\$(ls '$EXTRA_DIR')\" ] && extra_installed_ok"

RUNNER_EXTRA_PACKAGES="  git+https://git.example.com/acme/acme-register@0123abc
  /srv/acme-tracker  "
check "the list is one requirement per word, spaces and new lines alike" \
  "[ \"\$(extra_packages | tr '\n' '|')\" = 'git+https://git.example.com/acme/acme-register@0123abc|/srv/acme-tracker|' ]"
check "with a list, the runner's hash moves, so the runner phase installs again" "[ \"\$(runner_source_hash)\" != '$plain_runner' ]"
check "and so does the console image's tag, which stays 16 characters" \
  "[ \"\$(console_source_hash)\" != '$plain_console' ] && [ \"\$(console_source_hash | tr -d '\n' | wc -c | tr -d ' ')\" = 16 ]"
apply_extra_packages
check "applied, each requirement is a wheel in EXTRA_DIR" \
  "[ \"\$(ls '$EXTRA_DIR' | tr '\n' ' ')\" = 'acme_register-0.1.0-py3-none-any.whl acme_tracker-0.1.0-py3-none-any.whl ' ]"
check "built without dependencies, and installed from those wheels" \
  "grep -q '^wheel .*--no-deps' '$RUNNER_OPT/venv/calls' && grep -q '^install .*--no-deps.*acme_register-0.1.0' '$RUNNER_OPT/venv/calls'"
check "both are installed and the phase is done" "[ \"\$(installed)\" = 'acme_register acme_tracker' ] && extra_installed_ok"
"$RUNNER_OPT/venv/bin/pip" uninstall --yes acme_tracker
check "one removed by hand makes the phase not done" "! extra_installed_ok"

RUNNER_EXTRA_PACKAGES="git+https://git.example.com/acme/acme-register@4567def"
apply_extra_packages
check "a package dropped from the list is uninstalled, and setup says so" \
  "[ \"\$(installed)\" = 'acme_register' ] && grep -q 'removed acme_tracker' '$T/said' && extra_installed_ok"

mkdir -p "$T/acme-tracker/src/acme_tracker"
echo 'TRACKER = 1' > "$T/acme-tracker/src/acme_tracker/__init__.py"
RUNNER_EXTRA_PACKAGES="$T/acme-tracker"
before_runner="$(runner_source_hash)"; before_console="$(console_source_hash)"
mkdir -p "$T/acme-tracker/build" "$T/acme-tracker/src/acme_tracker/__pycache__"
echo junk > "$T/acme-tracker/build/x"; echo junk > "$T/acme-tracker/src/acme_tracker/__pycache__/x.pyc"
check "a folder's build leftovers do not count as a change" \
  "[ \"\$(runner_source_hash)\" = '$before_runner' ] && [ \"\$(console_source_hash)\" = '$before_console' ]"
echo 'TRACKER = 2' > "$T/acme-tracker/src/acme_tracker/__init__.py"
check "an edited file in a listed folder, at the same path, moves the runner's hash: the runner phase installs again" \
  "[ \"\$(runner_source_hash)\" != '$before_runner' ]"
check "and the console image's tag: the image is built again" "[ \"\$(console_source_hash)\" != '$before_console' ]"

RUNNER_EXTRA_PACKAGES=""
apply_extra_packages
check "an emptied list takes out what setup installed" "[ -z \"\$(installed)\" ] && extra_installed_ok"

RUNNER_EXTRA_PACKAGES="broken"
check "a requirement pip cannot build stops setup with its name" \
  "! ( apply_extra_packages ) && grep -q 'die: RUNNER_EXTRA_PACKAGES: pip could not fetch or build broken' '$T/said'"

[ "$fails" = 0 ] && { echo "all passed"; exit 0; }
echo "$fails failed"; exit 1

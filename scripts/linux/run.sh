#!/bin/sh
# In-container runner (see Dockerfile). Unpacks the tree piped on stdin into /tmp/work, commits
# it as a throwaway git repo, then runs one suite's steps. Each step's outcome is recorded in
# /out/steps.jsonl and printed as "step <name> exit <code>"; run.sh itself exits 0 once a suite's
# steps have run (it stops early after a failed "setup", which includes a tree that cannot be
# unpacked or committed) and non-zero only when it cannot run at all (an unknown suite: 64).
#
# In the main image the steps run as uid 1000. From the first of them on, root never writes
# through, runs, chmods or chowns a path uid 1000 can change: /out stays root's, user steps write
# their reports to /tmp/reports, and root copies each one into /out by reading it as the user.
SUITE="${1:-}"
OUT=/out
WORK=/tmp/work
REPORTS=/tmp/reports

case "$SUITE" in
  check | e2e | desktop | package | compile | oldgit) ;;
  *)
    echo "run.sh: unknown suite: $SUITE" >&2
    exit 64
    ;;
esac

mkdir -p "$OUT" /cache/uv /cache/npm /cache/electron
: > "$OUT/steps.jsonl"

# Appends {"step", "exit", "seconds"} to /out/steps.jsonl for step $1, which exited $2 and started
# at $3 (epoch seconds), and prints "step <name> exit <code>".
record() {
  printf '{"step": "%s", "exit": %s, "seconds": %s}\n' "$1" "$2" "$(($(date +%s) - $3))" >> "$OUT/steps.jsonl"
  echo "step $1 exit $2"
}

# Nothing has run as uid 1000 yet. A tree that cannot be unpacked, committed or (in main) handed
# to uid 1000 is recorded as a failed "setup", and the suite stops there like after a failed setup
# step.
start=$(date +%s)
code=0
mkdir -p "$WORK" "$REPORTS" && tar -xf - -C "$WORK" && cd "$WORK" && git init -q && git add -A \
  && git -c user.email=check@example.com -c user.name=check commit -qm tree || code=$?
if [ "$code" -eq 0 ] && [ "$SUITE" != compile ]; then
  chown -R 1000:1000 "$WORK" "$REPORTS" /cache || code=$?
fi
if [ "$code" -ne 0 ]; then
  record setup "$code" "$start"
  exit 0
fi

if [ "$SUITE" = compile ]; then
  # The compile image has no uid 1000 account: everything below already runs as root.
  as_user() {
    env HOME=/home/ubuntu PYTHONUTF8=1 UV_CACHE_DIR=/cache/uv UV_LINK_MODE=copy \
      npm_config_cache=/cache/npm ELECTRON_CACHE=/cache/electron/electron \
      ELECTRON_BUILDER_CACHE=/cache/electron/builder PYTEST_ADDOPTS=-rfEsp "$@"
  }
else
  as_user() {
    setpriv --reuid=1000 --regid=1000 --clear-groups env HOME=/home/ubuntu PYTHONUTF8=1 \
      UV_CACHE_DIR=/cache/uv UV_LINK_MODE=copy npm_config_cache=/cache/npm \
      ELECTRON_CACHE=/cache/electron/electron ELECTRON_BUILDER_CACHE=/cache/electron/builder \
      electron_config_cache=/cache/electron/xdg/electron PYTEST_ADDOPTS=-rfEsp "$@"
  }
fi

# Runs "$@" (a step function), logs its combined output to /out/<name>.log and records it. set -e
# stays off for the whole script so a step's own commands can chain with && (stop at the first
# failure inside that step) while run.sh itself keeps going to the next step.
run_step() {
  name="$1"
  shift
  start=$(date +%s)
  code=0
  ( "$@" ) >"$OUT/$name.log" 2>&1 || code=$?
  record "$name" "$code" "$start"
  return "$code"
}

# Copies the report $REPORTS/$1 into /out/$1. The user reads it, so root opens no path uid 1000
# can change; a report that cannot be read leaves none in /out.
keep_report() {
  as_user cat "$REPORTS/$1" > "$OUT/$1" || rm -f "$OUT/$1"
}

# pytest's lastfailed cache as /out/$1, read the same way; {} when pytest left none.
keep_lastfailed() {
  as_user cat "$WORK/.pytest_cache/v/cache/lastfailed" > "$OUT/$1" 2>/dev/null || printf '{}' > "$OUT/$1"
}

# testResults[].name (file path) for every failed file in the vitest json report $1; empty when
# there is nothing to rerun. node runs as the user, with the path as an argument, not as source.
failing_files() {
  as_user node -e '
    const r = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
    const f = r.testResults.filter(function (t) { return t.status === "failed"; }).map(function (t) { return t.name; });
    process.stdout.write(f.join("\n"));
  ' "$1" 2>/dev/null
}

check_setup() {
  as_user uv sync --locked --quiet \
    && (cd "$WORK/frontend" && as_user npm ci --no-audit --no-fund --loglevel=error)
}

check_python() {
  as_user uv run --locked python scripts/check.py --py
}

check_python_rerun() {
  as_user uv run --locked pytest -q --lf --lfnf=none
}

check_lint() {
  cd "$WORK/frontend" && as_user npm run lint
}

check_typecheck() {
  cd "$WORK/frontend" && as_user npm run typecheck
}

check_unit() {
  cd "$WORK/frontend" && as_user npm run test -- --reporter=dot --reporter=json --outputFile="$REPORTS/unit.json"
}

check_unit_rerun() {
  files="$1"
  cd "$WORK/frontend" && as_user npm run test -- $files --reporter=dot --reporter=json --outputFile="$REPORTS/unit-rerun.json"
}

check_build() {
  cd "$WORK/frontend" && as_user npm run build
}

e2e_setup() {
  as_user uv sync --locked --quiet \
    && (cd "$WORK/frontend" && as_user npm ci --no-audit --no-fund --loglevel=error && as_user npm run build --silent)
}

e2e_e2e() {
  cd "$WORK/frontend" && as_user npm run e2e -- --reporter=dot --reporter=json --outputFile="$REPORTS/e2e.json"
}

e2e_e2e_rerun() {
  files="$1"
  cd "$WORK/frontend" && as_user npm run e2e -- $files --reporter=dot --reporter=json --outputFile="$REPORTS/e2e-rerun.json"
}

# Chromium sandboxes itself with user namespaces (the desktop container runs with
# seccomp=unconfined, which lets uid 1000 create them), so Electron's chrome-sandbox helper is
# deleted rather than made setuid root.
desktop_setup() {
  as_user uv sync --locked --quiet \
    && (cd "$WORK/frontend" && as_user npm ci --no-audit --no-fund --loglevel=error && as_user npm run build --silent) \
    && (cd "$WORK/desktop" && as_user npm ci --no-audit --no-fund --loglevel=error) \
    && (cd "$WORK/desktop" && as_user node node_modules/electron/install.js \
      && as_user rm -f node_modules/electron/dist/chrome-sandbox)
}

desktop_lint() {
  cd "$WORK/desktop" && as_user npm run lint
}

desktop_test() {
  cd "$WORK/desktop" && as_user npm run test
}

desktop_smoke() {
  cd "$WORK/desktop" && as_user xvfb-run -a npm run smoke
}

desktop_e2e() {
  cd "$WORK/desktop" && as_user env CI=true xvfb-run -a npm run e2e
}

magpie_prepare() {
  # A comparison snapshot from before Magpie has no sidecar to prepare.
  if [ ! -f "$WORK/scripts/prepare_magpie.py" ]; then
    return 0
  fi
  as_user uv run --no-sync python scripts/prepare_magpie.py || return $?
  if [ "$SUITE" = package ]; then
    as_user uv run --no-sync python -c \
      'from pathlib import Path; from scripts.prepare_magpie import stage; stage(Path("."), Path("dist/compiled/viki-agent.dist"))'
  fi
}

package_setup() {
  # This is a disposable stub packaging/notices fixture, not a compiled-server verification.
  # Record real locked build-environment licences; no invented versions/texts or gate bypass.
  # The stub and its folders are made by uid 1000, the uid of the later steps: "dist" must end up
  # theirs (the dist step makes folders under it), and root writing into their tree could be sent
  # elsewhere through a link.
  as_user uv sync --locked --no-dev --no-editable --group build --quiet \
    && (cd "$WORK/frontend" && as_user npm ci --no-audit --no-fund --loglevel=error) \
    && (cd "$WORK/desktop" && as_user npm ci --no-audit --no-fund --loglevel=error) \
    && as_user mkdir -p "$WORK/dist/compiled/viki-agent.dist" \
    && as_user sh -c 'printf "#!/bin/sh\nexit 0\n" > "$1" && chmod 755 "$1"' sh \
      "$WORK/dist/compiled/viki-agent.dist/viki-agent" \
    && magpie_prepare \
    && as_user uv run --no-sync python -c \
      'from pathlib import Path; from scripts.licence_inventory import write; write(Path("dist/compiled/viki-agent.dist"), Path("dist/compiled/viki-agent-licences.json"))'
}

package_dist() {
  cd "$WORK/desktop" && as_user env XDG_CACHE_HOME=/cache/electron/xdg npm run dist -- --linux --dir
}

package_packaged_test() {
  cd "$WORK/desktop" && as_user env VIKI_PACKAGED_APP="$WORK/dist/desktop/linux-unpacked/viki-agent" npm run test
}

compile_setup() {
  as_user uv sync --locked --no-dev --no-editable --group build --quiet \
    && (cd "$WORK/frontend" && as_user npm ci --no-audit --no-fund --loglevel=error && as_user npm run build --silent) \
    && magpie_prepare
}

compile_compile() {
  as_user uv run --no-sync python scripts/compile.py
}

compile_worker_smoke() {
  as_user uv run --no-sync python scripts/compiled_smoke.py
}

compile_app_smoke() {
  as_user uv run --no-sync python scripts/compiled_smoke_app.py
}

oldgit_setup() {
  as_user uv sync --locked --quiet
}

# Runs tests/workspace_code on a real old Git (Debian 12's 2.39.5). That Git has no
# `check-attr --source` (added in 2.40), so `code_range.filters` takes its fallback: it reads
# the commit's tree into a private temporary index and runs `check-attr --cached` against it.
# On failure this ends its own log with the line _python_failures reads, same as check.py does
# for the check suite's python step, so the ids still surface instead of a bare
# "no result line".
oldgit_python() {
  as_user uv run --locked pytest -q -n auto --maxprocesses 12 --dist load tests/workspace_code
  rc=$?
  [ "$rc" -eq 0 ] || echo 'FAILED: pytest tests/workspace_code'
  return "$rc"
}

case "$SUITE" in
  check)
    run_step setup check_setup || exit 0
    run_step python check_python
    keep_lastfailed pytest-first.json
    first="$(cat "$OUT/pytest-first.json" 2>/dev/null || echo '{}')"
    if [ "$first" != '{}' ]; then
      run_step python-rerun check_python_rerun
      keep_lastfailed pytest-still.json
    fi
    run_step lint check_lint
    run_step typecheck check_typecheck
    run_step unit check_unit
    keep_report unit.json
    files="$(failing_files "$OUT/unit.json")"
    if [ -n "$files" ]; then
      run_step unit-rerun check_unit_rerun "$files"
      keep_report unit-rerun.json
    fi
    run_step build check_build
    ;;
  e2e)
    run_step setup e2e_setup || exit 0
    run_step e2e e2e_e2e
    keep_report e2e.json
    files="$(failing_files "$OUT/e2e.json")"
    if [ -n "$files" ]; then
      run_step e2e-rerun e2e_e2e_rerun "$files"
      keep_report e2e-rerun.json
    fi
    ;;
  desktop)
    run_step setup desktop_setup || exit 0
    run_step lint desktop_lint
    run_step test desktop_test
    run_step smoke desktop_smoke
    run_step e2e desktop_e2e
    ;;
  package)
    run_step setup package_setup || exit 0
    run_step dist package_dist
    run_step packaged-test package_packaged_test
    ;;
  compile)
    run_step setup compile_setup || exit 0
    run_step compile compile_compile
    run_step worker-smoke compile_worker_smoke
    run_step app-smoke compile_app_smoke
    ;;
  oldgit)
    run_step setup oldgit_setup || exit 0
    run_step python oldgit_python
    keep_lastfailed pytest-first.json
    first="$(cat "$OUT/pytest-first.json" 2>/dev/null || echo '{}')"
    if [ "$first" != '{}' ]; then
      run_step python-rerun check_python_rerun
      keep_lastfailed pytest-still.json
    fi
    ;;
esac

exit 0

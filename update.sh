#!/usr/bin/env bash
#
# update.sh — one-shot updater for lulism on the server.
#
# Pulls the latest code, reinstalls the CLI with pipx, restarts the long-running
# watchdog so it runs the new code, and health-checks the box before and after.
# Idempotent and safe to run anytime.
#
#   ./update.sh                 # the works: pull → reinstall → restart → doctor → status
#   ./update.sh --no-restart    # leave the watchdog service alone
#   ./update.sh --no-doctor     # skip the deep end-to-end check (e.g. server is down)
#   ./update.sh --no-status     # skip the final live dashboard
#   ./update.sh -h | --help     # this help
#
# Why it exists: `pipx install --force .` — mind the trailing dot! — is the only
# correct way to refresh a pipx install from a local checkout. `pipx upgrade`
# silently no-ops on an unchanged version, and dropping the "." makes pipx do
# nothing at all (the exact trap that left the box on 1.0.0). This wraps the
# whole dance so there's nothing to fumble, and verifies it actually took.
set -euo pipefail

# Run from the repo root no matter where you invoke it from.
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# ----------------------------------------------------------------- looks
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'
  YLW=$'\e[33m'; CYN=$'\e[36m'; MAG=$'\e[35m'; RST=$'\e[0m'
else
  BOLD= DIM= RED= GRN= YLW= CYN= MAG= RST=
fi
step()  { printf '\n%s▶ %s%s\n' "${BOLD}${CYN}" "$*" "$RST"; }
ok()    { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn()  { printf '  %s!%s %s\n' "$YLW" "$RST" "$*"; }
err()   { printf '  %s✗%s %s\n' "$RED" "$RST" "$*" >&2; }
info()  { printf '  %s·%s %s\n' "$DIM" "$RST" "$*"; }
field() { printf '  %s%-13s%s %s\n' "$DIM" "$1" "$RST" "$2"; }
die()   { err "$*"; exit 1; }

# ----------------------------------------------------------------- args
DO_RESTART=1 DO_DOCTOR=1 DO_STATUS=1
for a in "$@"; do
  case "$a" in
    --no-restart) DO_RESTART=0 ;;
    --no-doctor)  DO_DOCTOR=0 ;;
    --no-status)  DO_STATUS=0 ;;
    -h|--help)    sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $a (try --help)" ;;
  esac
done

# ----------------------------------------------------------------- helpers
have()        { command -v "$1" &>/dev/null; }
src_version() { sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' src/lulism/__init__.py; }
inst_version(){ have lulism && lulism --version 2>/dev/null | awk '{print $NF}' || echo "(none)"; }
have_user_systemd() { systemctl --user show-environment &>/dev/null; }

svc_state() {  # pretty "active (enabled)" / "inactive (disabled)" / "failed"
  local act ena
  act=$(systemctl --user is-active   "$1" 2>/dev/null || true)
  ena=$(systemctl --user is-enabled  "$1" 2>/dev/null || true)
  case "${act:-unknown}" in
    active)   printf '%sactive%s (%s)'   "$GRN" "$RST" "${ena:-?}" ;;
    inactive) printf '%sinactive%s (%s)' "$DIM" "$RST" "${ena:-?}" ;;
    failed)   printf '%sfailed%s'        "$RED" "$RST" ;;
    *)        printf '%s' "${act:-unknown}" ;;
  esac
}

server_reach() {  # map `lulism status` exit code (0 ok / 1 err / 3 unreachable)
  have lulism || { echo "(lulism not installed)"; return; }
  local rc=0; lulism status >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) printf '%sreachable%s'   "$GRN" "$RST" ;;
    3) printf '%sunreachable%s (SSH/host down)' "$YLW" "$RST" ;;
    *) printf '%sissue%s (lulism status exit %s)' "$YLW" "$RST" "$rc" ;;
  esac
}

health_panel() {
  field "lulism" "$(inst_version)"
  if have_user_systemd; then
    field "watchdog" "$(svc_state lulism-watchdog.service)"
    field "autosave" "$(svc_state lulism-autosave.timer)"
    field "backup"   "$(svc_state lulism-backup.timer)"
    field "metrics"  "$(svc_state lulism-metrics.timer)"
  else
    field "services" "systemctl --user unavailable — skipped"
  fi
  field "server" "$(server_reach)"
}

# ----------------------------------------------------------------- go
trap 'rc=$?; (( rc )) && err "aborted (exit $rc) — nothing was rolled back; fix the cause and re-run"' ERR

printf '%s╔════════════════════════════════════╗%s\n' "$MAG$BOLD" "$RST"
printf '%s║   lulism · update                  ║%s\n' "$MAG$BOLD" "$RST"
printf '%s╚════════════════════════════════════╝%s\n' "$MAG$BOLD" "$RST"

step "Preflight"
have git  || die "git not found"
have pipx || die "pipx not found — install it (pacman -S python-pipx / apt install pipx)"
[[ -f src/lulism/__init__.py ]] || die "this isn't the lulism repo (no src/lulism/__init__.py)"
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  warn "running as root — pipx and 'systemctl --user' are per-user; prefer the lulism user"
fi
git diff --quiet && git diff --cached --quiet || warn "working tree has local changes — a fast-forward pull may refuse"
ok "tooling present, in the lulism repo"

OLD_VER=$(inst_version)
OLD_COMMIT=$(git rev-parse --short HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

step "Before"
health_panel

step "Pulling latest code ($BRANCH)"
if git pull --ff-only; then :; else
  die "git pull failed — sort the working tree (git status) and retry"
fi
NEW_COMMIT=$(git rev-parse --short HEAD)
if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
  info "already at $NEW_COMMIT — reinstalling anyway so the install can't drift"
else
  ok "$OLD_COMMIT → $NEW_COMMIT"
fi

# 2.0.0: the pipx package itself was renamed. Removing the old one first stops
# the two packages fighting over the `mcctl` binary the shim needs to install.
# `pipx list` can exit non-zero for reasons unrelated to `mcctl` (e.g. some other
# pipx venv is broken), so its exit status must not feed this `if` under
# `pipefail` — capture the grep match into a variable instead, or an unrelated
# stale venv silently skips the uninstall we need here.
mcctl_pipx=$(pipx list --short 2>/dev/null | grep '^mcctl ' || true)
if [[ -n "$mcctl_pipx" ]]; then
  step "Removing the pre-2.0.0 pipx package"
  pipx uninstall mcctl && ok "old mcctl pipx package removed"
fi

step "Reinstalling lulism  (pipx install --force .)"
pipx install --force .   # the trailing "." is the whole point — install from this checkout

# The one check that catches the dropped-dot / stale-install bug for good.
NEW_VER=$(inst_version)
SRC_VER=$(src_version)
if [[ "$NEW_VER" == "$SRC_VER" ]]; then
  ok "lulism --version → $NEW_VER (matches the source)"
else
  die "version mismatch: installed=$NEW_VER but source=$SRC_VER — the reinstall didn't take"
fi

FAILED=0

# Restart the long-running watchdog onto the new code. Oneshot timers (autosave/
# backup/metrics) pick it up automatically on their next fire, so they're left be.
if (( DO_RESTART )) && have_user_systemd; then
  if [[ "$(systemctl --user is-active lulism-watchdog.service 2>/dev/null || true)" == "active" ]]; then
    step "Restarting the watchdog onto the new code"
    systemctl --user daemon-reload || true
    systemctl --user restart lulism-watchdog.service || { err "restart failed"; FAILED=1; }
    sleep 1
    if [[ "$(systemctl --user is-active lulism-watchdog.service 2>/dev/null || true)" == "active" ]]; then
      ok "lulism-watchdog.service is back up"
    else
      err "watchdog did not come back — check: systemctl --user status lulism-watchdog.service"; FAILED=1
    fi
  else
    info "watchdog not running — nothing to restart"
  fi
  info "the phone's 'mcctl agent' is spawned per SSH session, so it gets the new code on next connect"
fi

if (( DO_DOCTOR )) && have lulism; then
  step "Health check  (lulism doctor)"
  lulism doctor || { warn "doctor reported problems (above) — the update still applied"; FAILED=1; }
fi

step "After"
health_panel

step "Summary"
field "version" "$OLD_VER → $NEW_VER"
field "commit"  "$OLD_COMMIT → $NEW_COMMIT"

if (( DO_STATUS )) && have lulism; then
  step "Live status  (lulism status)"
  lulism status || true   # exit 3 just means the server's down; not an update failure
fi

if (( FAILED )); then
  printf '\n%s! updated, but with warnings — see above.%s\n' "$YLW$BOLD" "$RST"
  exit 1
fi
printf '\n%slulism is now %s. done! ✨ 🌟 ✨%s\n' "$GRN$BOLD" "$NEW_VER" "$RST"

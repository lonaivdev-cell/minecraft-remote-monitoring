#!/usr/bin/env bash
#
# update.sh — one-shot updater for lulism on the server.
#
# Pulls the latest code, reinstalls the CLI with pipx, migrates the pre-2.0.0
# systemd units, restarts the long-running watchdog so it runs the new code, and
# health-checks the box before and after. Idempotent and safe to run anytime.
#
#   ./update.sh                 # the works: pull → reinstall → units → restart → doctor
#   ./update.sh --no-restart    # leave an already-migrated watchdog service alone
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

# Everything below lives in one brace group ON PURPOSE. This script `git pull`s
# itself, and bash reads a script incrementally: if the file grows or shrinks
# mid-run, the commands after the pull are read from the *new* file at the *old*
# byte offset. A single compound command is parsed in full before any of it
# executes, so a self-update can never leave us running spliced text.
{

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
    -h|--help)    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $a (try --help)" ;;
  esac
done

# ----------------------------------------------------------------- helpers
have()        { command -v "$1" &>/dev/null; }

# The package directory moved in 2.0.0 (src/mcctl -> src/lulism), and this
# script pulls the very commit that moves it: after the pull the legacy path is
# gone, before it the new one does not exist yet. Reading only one of the two
# makes `sed` exit non-zero on a missing file, which under `set -e` aborts the
# run *after* pipx has already swapped the package. Try both, in new-first order.
# (tests/test_update_sh.py extracts this function by name and runs it.)
src_version() {
  local f
  for f in src/lulism/__init__.py src/mcctl/__init__.py; do
    if [[ -f $f ]]; then
      sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$f"
      return 0
    fi
  done
  return 0
}

inst_version(){ have lulism && lulism --version 2>/dev/null | awk '{print $NF}' || echo "(none)"; }
have_user_systemd() { systemctl --user show-environment &>/dev/null; }

# The seven pre-2.0.0 unit names. Must stay in step with util.legacy_unit_names()
# — tests/test_update_sh.py asserts the two lists are identical, because a name
# missing here is a unit that stays enabled and becomes a second restart
# authority (the 2026-06-11 outage).
legacy_units() {
  printf '%s\n' mcctl-watchdog.service \
                mcctl-autosave.service mcctl-autosave.timer \
                mcctl-backup.service   mcctl-backup.timer \
                mcctl-metrics.service  mcctl-metrics.timer
}
new_unit_for() { printf '%s\n' "lulism-${1#mcctl-}"; }

unit_enabled() {
  local st; st=$(systemctl --user is-enabled "$1" 2>/dev/null || true)
  case "$st" in enabled|enabled-runtime|linked|linked-runtime) return 0 ;; *) return 1 ;; esac
}
unit_active()  { [[ "$(systemctl --user is-active "$1" 2>/dev/null || true)" == "active" ]]; }
unit_known()   { systemctl --user cat "$1" &>/dev/null; }

# Every legacy unit systemd still knows as enabled or running. Used twice: once
# in the Before panel to record what has to be re-enabled under the new names,
# and once at the very end, where anything left is a hard failure.
legacy_leftovers() {
  local u
  while read -r u; do
    if unit_enabled "$u" || unit_active "$u"; then printf '%s\n' "$u"; fi
  done < <(legacy_units)
}

# How many watchdog daemons are alive under either name. A count, not a yes/no:
# one is the target state and two is the 2026-06-11 outage, and a daemon started
# by hand belongs to no unit, so `systemctl` cannot see it at all.
watchdog_procs() {
  local n; n=$(pgrep -c -f '(mcctl|lulism) watchdog run' 2>/dev/null || true)
  printf '%s\n' "${n:-0}"
}

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
# Accept the pre-2.0.0 layout too: on a 1.1.2 box this check runs *before* the
# pull that creates src/lulism/, so demanding the new path would refuse to start
# the very upgrade that produces it.
[[ -f src/lulism/__init__.py || -f src/mcctl/__init__.py ]] \
  || die "this isn't the lulism repo (no src/lulism/__init__.py)"
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

# Record which pre-2.0.0 units are live BEFORE anything is removed — this is the
# only moment the information exists. `lulism watchdog install` disables them a
# few steps down, and without this list we could not tell "the operator ran the
# watchdog + backups" from "the operator ran nothing", so we could not re-enable
# the right replacements.
LEGACY_SEEN=()
if have_user_systemd; then
  while read -r u; do LEGACY_SEEN+=("$u"); done < <(legacy_leftovers)
  if (( ${#LEGACY_SEEN[@]} )); then
    field "pre-2.0.0" "${YLW}${LEGACY_SEEN[*]}${RST}"
  fi
fi

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

# ------------------------------------------------- systemd units (design §6.2)
# Renaming the units without migrating them re-creates the 2026-06-11 outage:
# the mcctl-* units stay enabled and running, the lulism-* ones get enabled
# alongside, and two watchdogs each believe they own restart authority. The pipx
# swap above does not touch systemd, so this is the step that has to.
#
# It runs regardless of --no-restart when there is something to migrate: leaving
# a box half-migrated is exactly the failure this exists to prevent. --no-restart
# still spares an already-migrated, already-running watchdog further down.
UNITS_MIGRATED=0 WD_STARTED=0
if have_user_systemd; then
  NEED_UNITS=0
  (( ${#LEGACY_SEEN[@]} )) && NEED_UNITS=1
  unit_known lulism-watchdog.service || NEED_UNITS=1

  if (( NEED_UNITS )); then
    step "Migrating the systemd user units"
    # stop → disable → remove the seven mcctl-* units, write the lulism-* ones,
    # daemon-reload. Never leaves both generations installed.
    if lulism watchdog install; then
      UNITS_MIGRATED=1
    else
      err "lulism watchdog install failed"; FAILED=1
    fi

    # Re-enable exactly what was enabled before, under the new names. Nothing
    # more: enabling a timer the operator never wanted would be a behaviour
    # change, and enabling nothing when the watchdog *was* running would leave
    # the server without self-healing.
    if (( UNITS_MIGRATED && ${#LEGACY_SEEN[@]} )); then
      targets=()
      for u in "${LEGACY_SEEN[@]}"; do targets+=("$(new_unit_for "$u")"); done
      # `systemctl enable --now` does NOT restart a unit that is already
      # active, so whether the watchdog was actually just started has to be
      # observed before the call, not assumed from "it's in targets". On a
      # mixed half-migrated box (a legacy unit still enabled, but
      # lulism-watchdog.service already running) assuming it would leave the
      # watchdog running the pre-`pipx install` code while this script claims
      # otherwise and skips the restart below.
      wd_active_before=0
      if unit_active lulism-watchdog.service; then wd_active_before=1; fi
      if systemctl --user enable --now "${targets[@]}"; then
        ok "enabled ${targets[*]}"
        if [[ " ${targets[*]} " == *" lulism-watchdog.service "* ]] && (( ! wd_active_before )); then
          WD_STARTED=1
        fi
      else
        err "could not enable ${targets[*]}"; FAILED=1
      fi
    elif (( UNITS_MIGRATED )); then
      info "no pre-2.0.0 units were enabled — nothing to re-enable"
      info "enable self-healing with: systemctl --user enable --now lulism-watchdog.service"
    fi

    # Anything still enabled or running under the old names is a second restart
    # authority. That is a failure, not a footnote: reporting success here is how
    # a box ends up with two watchdogs and nobody looking.
    leftover=()
    while read -r u; do leftover+=("$u"); done < <(legacy_leftovers)
    if (( ${#leftover[@]} )); then
      err "pre-2.0.0 units survived the migration: ${leftover[*]}"
      info "two restart authorities — fix before walking away:"
      info "  systemctl --user disable --now ${leftover[*]}"
      info "  systemctl --user daemon-reload"
      FAILED=1
    else
      ok "no pre-2.0.0 units left enabled or running"
    fi
  else
    info "systemd units already on the lulism names — nothing to migrate"
  fi
fi

# A watchdog that was never a unit at all. `systemctl stop` cannot reach a
# daemon someone started by hand, and pacman's replaces=('mcctl') deletes unit
# files while their processes keep running, so the unit checks above can come
# back clean with two brains alive.
#
# This has to run on EVERY invocation, not just the one that migrates units:
# a hand-started `lulism watchdog run` belongs to no unit at all, so the unit
# checks above come back clean on that run too, and the extra daemon
# accumulates on an already-migrated box — precisely the run where
# NEED_UNITS is 0 and the block above never executes. Nesting this inside
# `if (( NEED_UNITS ))` (or `have_user_systemd`, which it doesn't need — it's
# pgrep, not the bus) would make the gate blind on exactly the box it exists
# to catch, and neither `doctor` (WARN, exit 0) nor `--no-restart` would pick
# up the slack.
wd_procs=$(watchdog_procs)
if (( wd_procs > 1 )); then
  err "$wd_procs watchdog daemons are running — there must be exactly one"
  info "  pgrep -af '(mcctl|lulism) watchdog run'   # find the extra one, then kill it"
  FAILED=1
fi

# Restart the long-running watchdog onto the new code. Oneshot timers (autosave/
# backup/metrics) pick it up automatically on their next fire, so they're left be.
if (( WD_STARTED )); then
  info "lulism-watchdog.service was just started on the new code by the unit migration"
elif (( DO_RESTART )) && have_user_systemd; then
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

}  # end of the self-update-safe brace group (see the note under `set -euo pipefail`)

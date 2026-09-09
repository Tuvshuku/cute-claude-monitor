#!/usr/bin/env bash
# Health check for both halves. Run this if the card looks wrong or missing.
#   ./status.sh
set -uo pipefail

APP_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$APP_DIR"

ok()   { printf '  \033[32m OK \033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }
FAILED=0

DATA="$(python3 - <<'PY' 2>/dev/null
import sys; sys.path.insert(0, ".")
from collector import load_config, resolve_output_path
print(resolve_output_path(load_config()))
PY
)"

echo
echo "collector (WSL)"
if systemctl --user is-active --quiet claude-usage-collector 2>/dev/null; then
    ok "service running"
else
    bad "service not running   ->  systemctl --user start claude-usage-collector"
fi
if [[ "$(systemctl --user is-enabled claude-usage-collector 2>/dev/null)" == "enabled" ]]; then
    ok "starts with WSL"
else
    bad "not enabled          ->  systemctl --user enable --now claude-usage-collector"
fi
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == "yes" ]]; then
    ok "linger on (starts without a shell)"
else
    bad "linger off           ->  loginctl enable-linger $USER"
fi

echo
echo "data"
if [[ -f "$DATA" ]]; then
    python3 - "$DATA" <<'PY'
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"  \033[31mFAIL\033[0m snapshot unreadable ({exc})"); raise SystemExit
age = time.time() - d["generated_at"]
mark = "\033[32m OK \033[0m" if age < 45 else "\033[31mFAIL\033[0m"
print(f"  {mark} snapshot {age:.0f}s old   ({d['status']})")

live = d.get("live") or {}
if live.get("ok"):
    print("  \033[32m OK \033[0m live sync with your account (exact, matches /usage)")
elif live.get("cached"):
    age = live.get("age_seconds") or 0
    print(f"  \033[33mWARN\033[0m live sync: {live.get('error') or 'unavailable'} - showing last exact reading from {age/60:.0f}m ago")
else:
    print(f"  \033[33mWARN\033[0m live sync off: {live.get('error') or 'disabled'} - falling back to local estimates")

for g in d["gauges"]:
    r = f"{int(g['resets_in'])//3600}h {int(g['resets_in'])%3600//60:02d}m" if g["resets_in"] else "ready"
    tag = {"live": "live", "last_live": "LAST SYNC", "configured": "calibrated"}.get(g["source"], "ESTIMATED")
    print(f"       {g['label']:<8} {g['pct']*100:5.1f}%  resets {r:<9} [{tag}]   local tokens {g['used_label']}")
PY
else
    bad "no snapshot at $DATA"
fi

echo
echo "widget (Windows)"
COUNT="$(powershell.exe -NoProfile -Command \
  "@(Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { \$_.CommandLine -like '*widget.ps1*' -and \$_.ProcessId -ne \$PID }).Count" \
  2>/dev/null | tr -d '\r')"
case "${COUNT:-0}" in
    0) bad "not running          ->  open $(wslpath -w "$(dirname "$DATA")" 2>/dev/null)\\Start-Widget.vbs" ;;
    1) ok  "running (1 instance)" ;;
    *) warn "$COUNT instances running - close the extras from the right-click menu" ;;
esac

STARTUP="$(dirname "$(dirname "$DATA")")/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/CuteClaudeWidget.vbs"
if [[ -f "$STARTUP" ]]; then
    ok "launches at Windows sign-in"
else
    bad "no autostart entry   ->  ./install.sh --autostart"
fi

echo
if [[ $FAILED -eq 0 ]]; then
    echo "All good."
else
    echo "Run the suggested command next to each FAIL."
fi
exit $FAILED

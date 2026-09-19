#!/bin/sh
# Check the Slack token: is it valid, whose is it, and does it hold only the scopes
# this tool allows? No dependencies.
#
# Read-only. It makes a single call, auth.test, and never attempts a write to see
# whether one would be refused - if a token ever did carry a write scope, a test
# like that would itself post the message. The scope list Slack reports answers
# the question without acting on it.
#
# Exits nonzero if the token is invalid, if Slack does not report its scopes, or
# if any scope is not on the allowlist.
set -u

TOKEN=$(security find-generic-password -a "$USER" -s "SLACK_USER_TOKEN" -w 2>/dev/null) || true
[ -z "${TOKEN:-}" ] && { echo "FATAL: no SLACK_USER_TOKEN in Keychain"; exit 1; }

W=$(mktemp -d); trap 'rm -rf "$W"' EXIT

# Token goes in a curl config on stdin, never in argv -> never visible in `ps`.
auth_cfg() { printf 'header = "Authorization: Bearer %s"\nsilent\n' "$TOKEN"; }

echo "==============================================================="
echo " TEST 1  auth.test  — is the token valid, and whose is it?"
echo "==============================================================="
{ auth_cfg; printf 'url = "https://slack.com/api/auth.test"\n'; } \
  | curl -K - -D "$W/h.txt" -o "$W/auth.json" \
  || { echo "FATAL: could not reach Slack"; exit 1; }

python3 - "$W/auth.json" <<'PY' || exit 1
import json, sys
d = json.load(open(sys.argv[1]))
if not d.get("ok"):
    print(f"  ok         : False\n  error      : {d.get('error')}"); sys.exit(1)
print(f"  ok         : True")
print(f"  workspace  : {d.get('team')}   {d.get('url')}")
print(f"  identity   : {d.get('user')}   (user id {d.get('user_id')})")
print(f"  bot_id     : {d.get('bot_id', '(absent - this is a pure user token)')}")
PY

echo
echo "==============================================================="
echo " TEST 2  which scopes does Slack report for this token?"
echo "==============================================================="
SCOPES=$(tr -d '\r' < "$W/h.txt" | awk 'tolower($1)=="x-oauth-scopes:"{$1=""; print}' \
  | tr ',' '\n' | sed 's/^ *//' | grep -v '^$' | sort)

# Unknown is not safe: no scope list means the permissions cannot be verified.
if [ -z "$SCOPES" ]; then
  echo "  FATAL: Slack did not report this token's scopes, so its permissions"
  echo "         cannot be verified."
  exit 1
fi

unexpected=0
set -f          # a scope name is data; never let the shell expand it as a glob
for s in $SCOPES; do
  # The same allowlist the exporter enforces (ALLOWED_SCOPES): anything not named
  # here is flagged, whether or not its name looks like a write.
  case "$s" in
    channels:history|channels:read|groups:history|groups:read|\
    im:history|im:read|mpim:history|mpim:read|users:read|identify)
      printf '  %-22s  allowed\n' "$s" ;;
    *)
      printf '  %-22s  <-- UNEXPECTED, not in the manifest\n' "$s"
      unexpected=$((unexpected + 1)) ;;
  esac
done
set +f

echo
if [ "$unexpected" -gt 0 ]; then
  echo "  FAIL: $unexpected unexpected scope(s). The exporter will refuse to run with"
  echo "        this token. Check the app's permissions against the manifest."
  exit 1
fi
echo "  PASS: every scope is on the allowlist, and none of them can write."

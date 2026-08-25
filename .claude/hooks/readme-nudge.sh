#!/usr/bin/env bash
# Stop hook: when files changed but README.md didn't, ask Claude to consider a doc
# update before ending the turn. Fires at most once per session so it can't loop.

input=$(cat)
sentinel="/tmp/claude-readme-nudge-$(jq -r '.session_id // "unknown"' <<<"$input")"
[ -f "$sentinel" ] && exit 0

changed=$(git status --porcelain 2>/dev/null)
[ -n "$changed" ] || exit 0
case "$changed" in
  *README.md*) exit 0 ;;
esac

touch "$sentinel"
cat <<'JSON'
{"decision":"block","reason":"Files changed this session but README.md did not. Check whether README.md still describes what you changed — pipeline layout and stages, file contracts, how scripts are run, league/scoring assumptions. Update the affected sections if it is now stale. If nothing in the README is affected, say so in one line and stop."}
JSON

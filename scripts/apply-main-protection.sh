#!/usr/bin/env bash
# Administrator-only utility: NEVER invoked by the application or QPKG.
set -euo pipefail
umask 077
command -v curl >/dev/null || { echo "curl required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }
TOKEN="\${GITHUB_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  read -r -s -p "GitHub token (Administration read/write): " TOKEN
  printf '\n'
fi
[[ -n "$TOKEN" ]] || { echo "No token supplied" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; unset TOKEN' EXIT
API="https://api.github.com/repos/souten-yd/QnapDownloader"
NAME="Require PR for main (QnapDownloader)"
headers=(-H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2026-03-10' -H "Authorization: Bearer $TOKEN")
# Additive: never overwrite existing rulesets or branch protection.
curl --fail --silent --show-error "\${headers[@]}" "$API/rulesets?per_page=100" > "$TMP/existing.json"
if python3 - "$TMP/existing.json" "$NAME" <<'PY'
import json,sys
with open(sys.argv[1]) as stream:
    rules=json.load(stream)
assert isinstance(rules,list), "Expected list"
sys.exit(0 if any(x.get("name")==sys.argv[2] for x in rules) else 1)
PY
then
  echo "Named ruleset already exists; no changes made."
else
  cat > "$TMP/ruleset.json" <<'JSON'
{
  "name": "Require PR for main (QnapDownloader)",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
  "rules": [
    {"type": "pull_request", "parameters": {
      "dismiss_stale_reviews_on_push": true,
      "required_approving_review_count": 0,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": true
    }},
    {"type": "non_fast_forward"},
    {"type": "deletion"}
  ]
}
JSON
  curl --fail --silent --show-error "\${headers[@]}" -X POST \
    "$API/rulesets" -d @"$TMP/ruleset.json" > "$TMP/created.json"
  python3 - "$TMP/created.json" <<'PY'
import json,sys
j=json.load(open(sys.argv[1]))
assert j.get("enforcement")=="active", "Not active"
print("Created:", j.get("id"), j.get("name"))
PY
fi
curl --fail --silent --show-error "\${headers[@]}" "$API/rules/branches/main" > "$TMP/effective.json"
python3 - "$TMP/effective.json" <<'PY'
import json,sys
rules=json.load(open(sys.argv[1]))
types={x.get("type") for x in rules}
missing={"pull_request","non_fast_forward","deletion"}-types
if missing:
    raise SystemExit("ERROR: effective main rules not confirmed: "+", ".join(sorted(missing)))
print("Verified effective rules: pull_request / non_fast_forward / deletion")
PY
echo "PR creation Collaborators only is a separate setting: Settings > General > Features > Pull requests."

#!/usr/bin/env bash
# Print a GitHub App installation token for one org, minted from the app's id and private key.
# An app has its own API limit (at least 5,000 calls an hour per installation), so a timer using it never spends
# a person's personal limit. The token lasts an hour; mint a fresh one each pass.
# Usage: app-token.sh <app id> <private key .pem> <org>
set -euo pipefail

app_id="${1:?app id}"
key="${2:?private key path}"
org="${3:?org}"
api="${GITHUB_API_URL:-https://api.github.com}"

b64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

now="$(date +%s)"
header="$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)"
payload="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$app_id" | b64url)"
sig="$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$key" -binary | b64url)"
jwt="$header.$payload.$sig"

get() {
  curl -fsS -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" "$@"
}

inst="$(get "$api/orgs/$org/installation" 2>/dev/null || get "$api/users/$org/installation")"
inst_id="$(printf '%s' "$inst" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
get -X POST "$api/app/installations/$inst_id/access_tokens" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'

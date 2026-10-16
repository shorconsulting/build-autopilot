#!/usr/bin/env bash
# setup-github-gate.sh -- set up the GitHub side of the Build Autopilot gate (v2) for one repo.
#
# What it does, in order (each step is skipped when it is already done, so re-running is safe):
#   1. The Build Autopilot GitHub App for the repo's owner: created once per org through GitHub's
#      "create an app from a manifest" flow (your browser opens; you click "Create GitHub App").
#      Its private key is saved on this computer only: ~/.build-autopilot/keys/<owner>.pem (mode 600).
#   2. Installs the app on the repo (adds the repo to an existing installation, or opens the install page).
#   3. Two environments locked to the default branch: autopilot-review and autopilot.
#   4. Secrets and variables in those environments: the app key, App ID and Client ID, and the
#      Anthropic API key (you type it here; it is never shown or written to disk).
#   5. Repo settings: allow auto-merge; workflow token read-only; Actions may not approve pull requests.
#   6. With --ruleset: the "build-autopilot" branch ruleset (pull request required, the app's
#      autopilot/gate check required, plus the repo's test checks). Only needed for level auto-merge
#      or autopilot. Note: after this, nobody can push straight to the default branch.
#
# It does not add the gate's files to the repo: run /autopilot apply for that (one pull request a person merges).
#
# Usage:
#   scripts/setup-github-gate.sh OWNER/REPO [--ruleset] [--test-check "NAME"]... [--dry-run] [--yes]
#                                           [--key-file PATH] [--rotate-anthropic-key]
#
#   --ruleset              also create/update the branch ruleset (asks which test checks to require)
#   --test-check NAME      a test check the ruleset requires (repeat); default: offer the checks GitHub
#                          Actions reported on the default branch's latest commit
#   --dry-run              show what would change; make no changes (read-only calls only)
#   --yes                  don't ask before each change
#   --key-file PATH        use this .pem for the app (for an app created earlier, or a regenerated key)
#   --rotate-anthropic-key ask for the Anthropic key again even if the secret is already set
#
# Several repos at once: scripts/setup-github-gate-many.sh (asks for the Anthropic key once).
#
# Needs: gh (logged in as an org owner or GitHub App manager, and an admin of the repo), jq, python3, openssl, curl.

set -euo pipefail

PROG=$(basename "$0")
STATE_DIR="${BUILD_AUTOPILOT_HOME:-$HOME/.build-autopilot}"
ACTIONS_APP_ID=15368          # GitHub Actions' own app id: the integration test checks report as
RULESET_NAME="build-autopilot"

REPO=""
DO_RULESET=0
DRY_RUN=0
ASSUME_YES=0
KEY_FILE=""
ROTATE_ANTHROPIC=0
TEST_CHECKS=()

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# Whatever happens (an error, Ctrl-C), remove the temp folder that may hold the app key and turn echo back on.
CLEANUP_DIR=""
GATE_TMP=""
cleanup() {
  stty echo 2>/dev/null </dev/tty || true
  if [ -n "$CLEANUP_DIR" ]; then rm -rf "$CLEANUP_DIR"; fi
  if [ -n "$GATE_TMP" ]; then rm -rf "$GATE_TMP"; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# confirm "question" -> returns 0 for yes. --yes answers yes; --dry-run answers no (and says so).
confirm() {
  if [ "$DRY_RUN" -eq 1 ]; then say "  (dry run) would: $1"; return 1; fi
  if [ "$ASSUME_YES" -eq 1 ]; then return 0; fi
  local ans
  printf '%s [y/N] ' "$1"
  read -r ans </dev/tty || ans=""
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --ruleset) DO_RULESET=1 ;;
    --test-check) [ $# -ge 2 ] || die "--test-check needs a name"; TEST_CHECKS+=("$2"); shift ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    --key-file) [ $# -ge 2 ] || die "--key-file needs a path"; KEY_FILE="$2"; shift ;;
    --rotate-anthropic-key) ROTATE_ANTHROPIC=1 ;;
    -h|--help) usage 0 ;;
    -*) die "unknown option: $1 (see $PROG --help)" ;;
    *) [ -z "$REPO" ] || die "only one OWNER/REPO at a time"; REPO="$1" ;;
  esac
  shift
done
[ -n "$REPO" ] || usage 1
# A key handed over by setup-github-gate-many.sh: keep it in an unexported variable, so no program this script
# starts (jq, curl, a browser) inherits it. It is only ever piped into gh secret set.
GATE_ANTHROPIC_KEY="${ANTHROPIC_API_KEY_FOR_GATE:-}"
unset ANTHROPIC_API_KEY_FOR_GATE
case "$REPO" in */*) ;; *) die "give the repo as OWNER/REPO" ;; esac
OWNER=${REPO%%/*}

# ---------------------------------------------------------------- preflight
step "Checking tools and login"
command -v gh >/dev/null 2>&1 || die "gh is not installed (https://cli.github.com)"
command -v jq >/dev/null 2>&1 || die "jq is not installed (brew install jq)"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not logged in: run gh auth login"
SCOPES=$(gh auth status 2>&1 | sed -n 's/.*Token scopes: //p' | head -1)
case "$SCOPES" in
  *repo*) ;;
  *) warn "your gh token may lack the 'repo' scope (scopes: ${SCOPES:-unknown}). If a step fails, run: gh auth refresh -s repo,read:org" ;;
esac
ME=$(gh api user --jq .login)
REPO_JSON=$(gh api "repos/$REPO") || die "can't read $REPO with this login"
REPO=$(printf '%s' "$REPO_JSON" | jq -r .full_name)     # GitHub's own spelling, so later comparisons match
OWNER=${REPO%%/*}
say "Logged in as $ME. Repo: $REPO.$( [ "$DRY_RUN" -eq 1 ] && printf ' DRY RUN: nothing will change.' )"
DEFAULT_BRANCH=$(printf '%s' "$REPO_JSON" | jq -r .default_branch)
IS_ADMIN=$(printf '%s' "$REPO_JSON" | jq -r '.permissions.admin // false')
[ "$IS_ADMIN" = "true" ] || die "$ME is not an admin of $REPO; an admin has to run this"
OWNER_TYPE=$(gh api "users/$OWNER" --jq .type)
say "Default branch: $DEFAULT_BRANCH. Owner type: $OWNER_TYPE."

mkdir -p "$STATE_DIR/apps" "$STATE_DIR/keys"
chmod 700 "$STATE_DIR" "$STATE_DIR/keys"
APP_META="$STATE_DIR/apps/$OWNER.json"
APP_KEY="$STATE_DIR/keys/$OWNER.pem"

# ---------------------------------------------------------------- 1. the app
step "1. The Build Autopilot GitHub App for $OWNER"

app_exists() {  # app_exists SLUG -> 0 if GitHub knows the app
  gh api "apps/$1" >/dev/null 2>&1
}

create_app() {
  local name tmp code
  name="Build Autopilot ($OWNER)"
  name=$(printf '%s' "$name" | cut -c1-34)        # GitHub's limit for app names
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/build-autopilot.XXXXXX")
  chmod 700 "$tmp"
  CLEANUP_DIR="$tmp"
  # A tiny local web page posts the app manifest to GitHub; GitHub sends the browser back to it with a one-time code.
  cat > "$tmp/manifest_flow.py" <<'PY_EOF'
import html, http.server, json, re, secrets, sys, threading, time, urllib.parse, webbrowser

owner, owner_type, name = sys.argv[1], sys.argv[2], sys.argv[3]
state = secrets.token_urlsafe(24)
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
got = {}

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Host") != "127.0.0.1:%d" % port:   # refuse other names (DNS rebinding)
            self.reply("<p>Wrong address.</p>", 403)
            return
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            self.reply(
                "<!doctype html><title>Build Autopilot</title><body style='font-family:sans-serif'>"
                "<p>Sending you to GitHub to create the Build Autopilot app for <b>%s</b>...</p>"
                "<form id=f method=post action='%s'><input type=hidden name=manifest value='%s'>"
                "<button>Continue to GitHub</button></form><script>document.getElementById('f').submit()</script>"
                % (html.escape(owner), html.escape(action, quote=True), html.escape(json.dumps(manifest), quote=True)))
        elif url.path == "/callback":
            q = urllib.parse.parse_qs(url.query)
            code = q.get("code", [""])[0]
            if q.get("state", [""])[0] != state or not CODE_RE.match(code) or "code" in got:
                self.reply("<p>That link doesn't match this setup run. Go back to the terminal.</p>", 400)
                return
            got["code"] = code
            self.reply("<p>Done: GitHub created the app. You can close this tab and go back to the terminal.</p>")
        else:
            self.reply("<p>Not found.</p>", 404)

server = http.server.HTTPServer(("127.0.0.1", 0), Handler)   # one socket, no gap for another process to take the port
port = server.server_address[1]
base = "http://127.0.0.1:%d" % port
manifest = {
    "name": name,
    "url": "https://github.com/shorconsulting/build-autopilot",
    "hook_attributes": {"url": "https://example.com/", "active": False},
    "redirect_url": base + "/callback",
    "public": False,
    "default_permissions": {"contents": "write", "pull_requests": "write", "checks": "write",
                            "issues": "write", "metadata": "read"},
    "default_events": [],
}
if owner_type == "Organization":
    action = "https://github.com/organizations/%s/settings/apps/new?state=%s" % (urllib.parse.quote(owner), state)
else:
    action = "https://github.com/settings/apps/new?state=%s" % state
threading.Thread(target=server.serve_forever, daemon=True).start()
sys.stderr.write("Opening your browser at %s/ . If it doesn't open, paste that address into it.\n" % base)
sys.stderr.write("On GitHub, check the name and click 'Create GitHub App'.\n")
webbrowser.open(base + "/")
deadline = time.time() + 900
while "code" not in got and time.time() < deadline:
    time.sleep(0.5)
server.shutdown()
if "code" not in got:
    sys.stderr.write("Timed out after 15 minutes without hearing back from GitHub.\n")
    sys.exit(1)
print(got["code"])
PY_EOF
  code=$(python3 "$tmp/manifest_flow.py" "$OWNER" "$OWNER_TYPE" "$name") || die "the app wasn't created"
  printf '%s' "$code" | grep -Eq '^[A-Za-z0-9_-]{1,100}$' || die "GitHub sent back an unexpected code"
  # Swap the one-time code for the app's details. The response holds the private key: it goes straight to the
  # key file (mode 600) and is never printed.
  ( umask 077; gh api -X POST "app-manifests/$code/conversions" > "$tmp/conv.json" ) \
    || die "GitHub didn't accept the code (it expires after an hour); run this again"
  # Keep the key only if this is the app we asked for: our org, exactly the permissions in the manifest.
  jq -e --arg owner "$OWNER" '
      (.owner.login | ascii_downcase) == ($owner | ascii_downcase)
      and .permissions == {"checks":"write","contents":"write","issues":"write","metadata":"read","pull_requests":"write"}
      and ((.events // []) | length) == 0
      and (.id | type) == "number"
      and (.pem | type) == "string" and (.pem | startswith("-----BEGIN"))' "$tmp/conv.json" >/dev/null \
    || die "the app GitHub returned isn't the one requested (owner, permissions or key differ). Delete it in $OWNER's settings > GitHub Apps and run this again"
  ( umask 077; jq -r .pem "$tmp/conv.json" > "$APP_KEY" )
  chmod 600 "$APP_KEY"
  jq '{id, slug, client_id, owner: .owner.login, name, created_at, html_url}' "$tmp/conv.json" > "$APP_META"
  rm -rf "$tmp"; CLEANUP_DIR=""
  say "Created $(jq -r .name "$APP_META") (App ID $(jq -r .id "$APP_META")). Key saved to $APP_KEY."
}

if [ -f "$APP_META" ] && app_exists "$(jq -r .slug "$APP_META")"; then
  say "Already set up: $(jq -r .name "$APP_META"), App ID $(jq -r .id "$APP_META")."
else
  [ -f "$APP_META" ] && warn "$APP_META names an app GitHub no longer knows; making a new one"
  if confirm "Create the Build Autopilot app for $OWNER now (opens your browser)?"; then
    create_app
  else
    [ "$DRY_RUN" -eq 1 ] || die "the app is needed for everything after this step"
  fi
fi

if [ -n "$KEY_FILE" ]; then
  [ -f "$KEY_FILE" ] || die "no such key file: $KEY_FILE"
  head -1 "$KEY_FILE" | grep -q '^-----BEGIN .*PRIVATE KEY-----$' || die "$KEY_FILE doesn't look like a GitHub App private key"
  if confirm "Use $KEY_FILE as the app key (copied to $APP_KEY)?"; then
    ( umask 077; cp "$KEY_FILE" "$APP_KEY" ); chmod 600 "$APP_KEY"
  fi
fi

if [ "$DRY_RUN" -eq 1 ] && [ ! -f "$APP_META" ]; then
  say "(dry run) no app yet, so the later steps show placeholders"
  APP_ID="<app id>"; APP_SLUG="<app slug>"; APP_CLIENT_ID="<client id>"
else
  APP_ID=$(jq -r .id "$APP_META"); APP_SLUG=$(jq -r .slug "$APP_META"); APP_CLIENT_ID=$(jq -r .client_id "$APP_META")
  case "$APP_ID" in ''|*[!0-9]*) die "$APP_META has no numeric App ID; delete it and re-run" ;; esac
  head -1 "$APP_KEY" 2>/dev/null | grep -q '^-----BEGIN' || die "no usable key for the app at $APP_KEY. In the app's settings (https://github.com/apps/$APP_SLUG), generate a private key, then re-run with --key-file PATH"
fi

# ---------------------------------------------------------------- 2. install on the repo
step "2. Install the app on $REPO"

# Whether the app is installed on a repo is something only the app itself can be asked (a person's gh login gets
# "403: must authenticate with a GitHub App"). So the script signs a short-lived app token (a JWT, valid 9 minutes)
# with the app's key, writes it to a private temp file and hands it to curl from there, never on a command line.
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
app_get() {  # app_get PATH -> prints the HTTP status; the body is left in $GATE_TMP/body.json
  local now h p sig
  now=$(date +%s)
  h=$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | b64url)
  p=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$APP_ID" | b64url)
  sig=$(printf '%s.%s' "$h" "$p" | openssl dgst -sha256 -sign "$APP_KEY" -binary | b64url)
  ( umask 077; printf 'Authorization: Bearer %s.%s.%s\n' "$h" "$p" "$sig" > "$GATE_TMP/auth" )
  curl -sS -o "$GATE_TMP/body.json" -w '%{http_code}' -H @"$GATE_TMP/auth" \
       -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
       "https://api.github.com/$1" 2>/dev/null || printf '000'
  rm -f "$GATE_TMP/auth"
}
open_page() {
  ( command -v open >/dev/null 2>&1 && open "$1" ) || ( command -v xdg-open >/dev/null 2>&1 && xdg-open "$1" ) || true
  say "  $1"
}

if [ "$APP_SLUG" = "<app slug>" ]; then
  say "  (dry run) would check the app is installed on $REPO, and open the install page if it isn't"
else
  command -v openssl >/dev/null 2>&1 || die "openssl is needed to check the app's installation"
  command -v curl >/dev/null 2>&1 || die "curl is needed to check the app's installation"
  GATE_TMP=$(mktemp -d "${TMPDIR:-/tmp}/build-autopilot.XXXXXX"); chmod 700 "$GATE_TMP"
  ST=$(app_get "repos/$REPO/installation")
  if [ "$ST" = "200" ]; then
    jq -e '.suspended_at == null' "$GATE_TMP/body.json" >/dev/null \
      || die "the app's installation in $OWNER is suspended; unsuspend it in $OWNER's settings > GitHub Apps, then re-run"
    say "Already installed on $REPO."
  else
    [ "$ST" = "404" ] || die "couldn't ask GitHub whether the app is installed on $REPO (HTTP $ST). Is $APP_KEY the app's current key?"
    ALSO=""
    [ -n "${GATE_INSTALL_ALSO:-}" ] && ALSO=" (and, while you're there, also: $GATE_INSTALL_ALSO)"
    if [ "$OWNER_TYPE" = "Organization" ]; then ST=$(app_get "orgs/$OWNER/installation"); else ST=$(app_get "users/$OWNER/installation"); fi
    if [ "$ST" = "200" ]; then
      IID=$(jq -r .id "$GATE_TMP/body.json")
      if [ "$OWNER_TYPE" = "Organization" ]; then
        URL="https://github.com/organizations/$OWNER/settings/installations/$IID"
      else
        URL="https://github.com/settings/installations/$IID"
      fi
      say "The app is installed in $OWNER but not on $REPO. On the page that opens, under 'Repository access',"
      say "  add $REPO$ALSO, then click Save."
    else
      URL="https://github.com/apps/$APP_SLUG/installations/new"
      say "The app isn't installed in $OWNER yet. On the page that opens, choose 'Only select repositories',"
      say "  pick $REPO$ALSO, then click Install."
    fi
    if confirm "Open that page?"; then
      open_page "$URL"
      printf 'Press Enter once that is saved... '; read -r _ </dev/tty || true
      [ "$(app_get "repos/$REPO/installation")" = "200" ] || die "the app still isn't installed on $REPO; re-run when it is"
      jq -e '.suspended_at == null' "$GATE_TMP/body.json" >/dev/null || die "the app's installation in $OWNER is suspended"
      say "Installed."
    elif [ "$DRY_RUN" -eq 0 ]; then
      die "the app has to be installed on $REPO before the rest"
    fi
  fi
fi

# ---------------------------------------------------------------- 3. locked environments
step "3. Environments locked to $DEFAULT_BRANCH"
lock_environment() {  # lock_environment NAME
  local env="$1" policies others
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  (dry run) would create/lock environment $env to branch $DEFAULT_BRANCH only"
    return 0
  fi
  printf '%s' '{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}' \
    | gh api -X PUT "repos/$REPO/environments/$env" --input - >/dev/null
  policies=$(gh api "repos/$REPO/environments/$env/deployment-branch-policies" --jq '.branch_policies[] | "\(.type // "branch") \(.name)"')
  others=$(printf '%s\n' "$policies" | grep -v -x "branch $DEFAULT_BRANCH" | grep -v '^$' || true)
  if [ -n "$others" ]; then
    die "environment $env also allows: $others. Remove those in Settings > Environments > $env (the gate's secrets must be reachable only from $DEFAULT_BRANCH), then re-run"
  fi
  if ! printf '%s\n' "$policies" | grep -qx "branch $DEFAULT_BRANCH"; then
    gh api -X POST "repos/$REPO/environments/$env/deployment-branch-policies" \
      -f name="$DEFAULT_BRANCH" -f type=branch >/dev/null
  fi
  say "  $env: only $DEFAULT_BRANCH may use it."
}
# env_state NAME -> prints locked | missing | unlocked | extra (allows more than the default branch)
env_state() {
  local env="$1" info policies others
  info=$(gh api "repos/$REPO/environments/$env" 2>/dev/null) || { echo missing; return 0; }
  if [ "$(printf '%s' "$info" | jq -r '.deployment_branch_policy.custom_branch_policies // false')" != "true" ]; then
    echo unlocked; return 0
  fi
  policies=$(gh api --paginate "repos/$REPO/environments/$env/deployment-branch-policies" \
               --jq '.branch_policies[] | "\(.type // "branch") \(.name)"')
  others=$(printf '%s\n' "$policies" | grep -v -x "branch $DEFAULT_BRANCH" | grep -v '^$' || true)
  if [ -n "$others" ]; then echo extra; return 0; fi
  if printf '%s\n' "$policies" | grep -qx "branch $DEFAULT_BRANCH"; then echo locked; else echo unlocked; fi
}
# require_locked NAME: never store a secret in an environment other branches can use
require_locked() {
  local st
  st=$(env_state "$1")
  [ "$st" = "locked" ] || die "environment $1 is $st, not locked to $DEFAULT_BRANCH only; re-run and let step 3 lock it first"
}
say "  Now: autopilot-review is $(env_state autopilot-review); autopilot is $(env_state autopilot)."
if confirm "Create or lock the environments autopilot-review and autopilot?"; then
  lock_environment autopilot-review
  lock_environment autopilot
  require_locked autopilot-review
  require_locked autopilot
fi

# ---------------------------------------------------------------- 4. secrets and variables
step "4. Secrets and variables"
if confirm "Store the app key, App ID and Client ID in the autopilot environment?"; then
  require_locked autopilot
  gh secret set AUTOPILOT_APP_PRIVATE_KEY --env autopilot --repo "$REPO" < "$APP_KEY"
  gh variable set AUTOPILOT_APP_ID --env autopilot --repo "$REPO" --body "$APP_ID"
  gh variable set AUTOPILOT_APP_CLIENT_ID --env autopilot --repo "$REPO" --body "$APP_CLIENT_ID"
  say "  Stored."
fi

HAS_ANTHROPIC=$(gh secret list --env autopilot-review --repo "$REPO" --json name --jq '[.[] | select(.name == "ANTHROPIC_API_KEY")] | length' 2>/dev/null || echo 0)
if [ "$HAS_ANTHROPIC" != "0" ] && [ "$ROTATE_ANTHROPIC" -eq 0 ]; then
  say "  ANTHROPIC_API_KEY is already set for autopilot-review (use --rotate-anthropic-key to replace it)."
elif [ -n "$GATE_ANTHROPIC_KEY" ]; then
  # Given once by setup-github-gate-many.sh, through the environment (never on a command line).
  if confirm "Store the Anthropic API key you entered at the start for the review job?"; then
    require_locked autopilot-review
    printf '%s' "$GATE_ANTHROPIC_KEY" | gh secret set ANTHROPIC_API_KEY --env autopilot-review --repo "$REPO"
    say "  Stored."
  fi
elif confirm "Set the Anthropic API key for the review job (you type it; it isn't shown)?"; then
  require_locked autopilot-review
  say "  Tip: make a separate key in its own Anthropic workspace with a monthly spend limit."
  printf '  Anthropic API key: '
  read -rs ANTHROPIC_KEY </dev/tty || ANTHROPIC_KEY=""
  printf '\n'
  case "$ANTHROPIC_KEY" in
    sk-ant-*) ;;
    "") die "no key entered" ;;
    *) warn "that doesn't start with sk-ant-; storing it anyway" ;;
  esac
  printf '%s' "$ANTHROPIC_KEY" | gh secret set ANTHROPIC_API_KEY --env autopilot-review --repo "$REPO"
  ANTHROPIC_KEY=""
  say "  Stored."
fi

# ---------------------------------------------------------------- 5. repo settings
step "5. Repo settings"
if confirm "Allow auto-merge, make the workflow token read-only, and stop Actions approving pull requests?"; then
  gh api -X PATCH "repos/$REPO" -F allow_auto_merge=true >/dev/null
  gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
    -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false >/dev/null
  say "  Done."
fi

# ---------------------------------------------------------------- 6. ruleset
if [ "$DO_RULESET" -eq 1 ]; then
  step "6. Branch ruleset \"$RULESET_NAME\""
  # The ruleset requires the gate's check; until the gate's workflow is on the default branch nothing could ever
  # pass it, including the pull request that installs the gate.
  if ! gh api "repos/$REPO/contents/.github/workflows/autopilot.yml?ref=$DEFAULT_BRANCH" >/dev/null 2>&1; then
    die "the gate isn't on $DEFAULT_BRANCH yet, so a ruleset requiring it would block every merge. Run /autopilot apply, merge that pull request, then re-run this with --ruleset"
  fi
  if [ "${#TEST_CHECKS[@]}" -eq 0 ]; then
    HEAD_SHA=$(gh api "repos/$REPO/commits/$DEFAULT_BRANCH" --jq .sha)
    FOUND=$(gh api --paginate "repos/$REPO/commits/$HEAD_SHA/check-runs?app_id=$ACTIONS_APP_ID&per_page=100" \
              --jq '.check_runs[].name' | sort -u)
    [ -n "$FOUND" ] || die "no GitHub Actions checks on $DEFAULT_BRANCH's latest commit; pass them with --test-check NAME"
    say "Test checks GitHub Actions reported on $DEFAULT_BRANCH's latest commit:"
    printf '%s\n' "$FOUND" | sed 's/^/  - /'
    if [ "$DRY_RUN" -eq 1 ] || confirm "Require all of these before a merge?"; then
      while IFS= read -r line; do [ -n "$line" ] && TEST_CHECKS+=("$line"); done <<EOF
$FOUND
EOF
    else
      die "pass the checks to require with --test-check NAME (repeat it)"
    fi
  fi
  CHECKS_JSON=$(printf '%s\n' "${TEST_CHECKS[@]}" | jq -R . | jq -s --argjson actions "$ACTIONS_APP_ID" \
                 'map({context: ., integration_id: $actions})')
  case "$APP_ID" in ''|*[!0-9]*) RULE_APP_ID=0 ;; *) RULE_APP_ID=$APP_ID ;; esac   # 0 only in a dry run
  RULESET=$(jq -n --arg name "$RULESET_NAME" --argjson app "$RULE_APP_ID" --argjson tests "$CHECKS_JSON" '{
    name: $name, target: "branch", enforcement: "active",
    conditions: {ref_name: {include: ["~DEFAULT_BRANCH"], exclude: []}},
    bypass_actors: [],
    rules: [
      {type: "deletion"},
      {type: "non_fast_forward"},
      {type: "pull_request", parameters: {required_approving_review_count: 0, dismiss_stale_reviews_on_push: false,
        require_code_owner_review: false, require_last_push_approval: false, required_review_thread_resolution: false}},
      {type: "required_status_checks", parameters: {strict_required_status_checks_policy: false,
        required_status_checks: ([{context: "autopilot/gate", integration_id: $app}] + $tests)}}
    ]}')
  say "Required before any merge into $DEFAULT_BRANCH: a pull request, autopilot/gate from the app, and:"
  printf '%s\n' "${TEST_CHECKS[@]}" | sed 's/^/  - /'
  say "Nobody (you included) can push straight to $DEFAULT_BRANCH after this, and there is no bypass list."
  EXISTING=$(gh api --paginate "repos/$REPO/rulesets?includes_parents=false" --jq ".[] | select(.name == \"$RULESET_NAME\") | .id" | head -1)
  if confirm "${EXISTING:+Update}${EXISTING:-Create} the ruleset now?"; then
    if [ -n "$EXISTING" ]; then
      printf '%s' "$RULESET" | gh api -X PUT "repos/$REPO/rulesets/$EXISTING" --input - >/dev/null
    else
      printf '%s' "$RULESET" | gh api -X POST "repos/$REPO/rulesets" --input - >/dev/null
    fi
    say "  Done."
  fi
else
  step "6. Branch ruleset: skipped (add --ruleset for level auto-merge or autopilot)"
fi

# ---------------------------------------------------------------- done
step "Done"
say "Next: in $REPO, run /autopilot apply. It opens one pull request adding the gate's files; a person merges it."
say "Then do the checks in step 5 of docs/v2-install.md (README-only PR, a workflow PR, /autopilot stop, the spoof test)."
say "The app key stays at $APP_KEY on this computer; keep it out of every repo and chat."

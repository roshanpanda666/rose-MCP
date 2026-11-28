#!/usr/bin/env bash
# Run a deepsec security scan (https://github.com/vercel-labs/deepsec) over ROSE and the phone app.
#
# It never runs on the live working trees. It copies the git-tracked files of both repos (with their
# current, uncommitted contents) into a fresh temp folder, so .env files, ~/rose state and untracked
# secrets are left behind. The deepsec workspace config lives in security/deepsec/ (config, ROSE matchers,
# per-project INFO.md threat models).
#
# The AI review uses the machine's logged-in `claude` CLI (--model-auth local): no API key, no Vercel.
# deepsec acts like a coding agent with a shell, so only run it on code you trust.
#
#   scripts/deepsec.sh              scan + AI review + revalidate + export
#   scripts/deepsec.sh scan         free pattern scan only
#   KEEP=1 scripts/deepsec.sh       keep the temp folder (it holds resumable state)
#
# Findings land in security/findings/<date>/ (gitignored).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PHONE="${ROSE_PHONE_APP:-$HERE/../rose-phone-app}"
MODE="${1:-full}"
WORK="${DEEPSEC_WORKDIR:-$(mktemp -d -t rose-deepsec-XXXXXX)}"
OUT="$HERE/security/findings/$(date +%Y-%m-%d_%H%M)"

snapshot() {  # tracked files only, working-tree contents
  local src="$1" dst="$2"
  mkdir -p "$dst"
  git -C "$src" ls-files -z | rsync -a --from0 --files-from=- "$src/" "$dst/"
}

echo "deepsec: snapshot into $WORK"
snapshot "$HERE" "$WORK/rose-MCP"
[ -d "$PHONE" ] && snapshot "$PHONE" "$WORK/rose-phone-app"

WS="$WORK/rose-MCP/.deepsec"
mkdir -p "$WS"
cp -r "$HERE/security/deepsec/." "$WS/"
cd "$WS"
[ -d node_modules ] || npm install --no-audit --no-fund --silent

PROJECTS=(rose-MCP)
[ -d "$WORK/rose-phone-app" ] && PROJECTS+=(rose-phone-app)

for p in "${PROJECTS[@]}"; do
  npx deepsec scan --project-id "$p"
  if [ "$MODE" != "scan" ]; then
    CI=1 npx deepsec process --project-id "$p" --agent claude --concurrency 3
    CI=1 npx deepsec revalidate --project-id "$p" --agent claude --concurrency 3 || true
  fi
  mkdir -p "$OUT/$p"
  npx deepsec export --project-id "$p" --format md-dir --out "$OUT/$p" || true
done

echo "deepsec: findings in $OUT"
[ "${KEEP:-0}" = 1 ] && echo "deepsec: workspace kept at $WORK" || rm -rf "$WORK"

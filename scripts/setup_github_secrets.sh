#!/bin/bash
# scripts/setup_github_secrets.sh
#
# One-time setup: copies all required secrets from ~/.env and
# ~/gmail_token.pickle into GitHub repository secrets so the
# .github/workflows/pipeline.yml workflow can run.
#
# Requires the `gh` CLI installed and authenticated:
#   brew install gh && gh auth login
#
# Usage:
#   bash scripts/setup_github_secrets.sh

set -euo pipefail

REPO="${REPO:-wm215/JobSearchPipeline}"

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: 'gh' CLI not installed. Run: brew install gh"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh not authenticated. Run: gh auth login"
  exit 1
fi

ENV_FILE="$HOME/.env"
TOKEN_FILE="$HOME/gmail_token.pickle"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found"
  exit 1
fi

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "ERROR: $TOKEN_FILE not found"
  exit 1
fi

# Helper: read a key from ~/.env, strip quotes, push to GitHub secret.
# Wrapped in `|| true` because grep returns 1 when key is absent and we have set -e.
push_secret() {
  local key="$1"
  local raw
  raw=$(grep "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 || true)
  if [[ -z "$raw" ]]; then
    echo "  SKIP   $key (not in ~/.env)"
    return 0
  fi
  local value
  value=$(printf '%s' "$raw" | cut -d= -f2- | sed 's/^["'\'']//; s/["'\'']$//')
  if [[ -z "$value" ]]; then
    echo "  SKIP   $key (empty value)"
    return 0
  fi
  echo "  PUSH   $key"
  printf '%s' "$value" | gh secret set "$key" --repo "$REPO" --body -
}

echo "Pushing secrets to https://github.com/$REPO ..."
push_secret USAJOBS_API_KEY
push_secret USAJOBS_EMAIL
push_secret EMAIL_FROM
push_secret EMAIL_TO
push_secret EMAIL_USER
push_secret EMAIL_PASS
push_secret OPENAI_API_KEY
push_secret GOOGLE_SHEETS_ID
push_secret HEALTHCHECK_URL

echo ""
echo "Encoding gmail_token.pickle as GMAIL_TOKEN_B64 ..."
base64 -i "$TOKEN_FILE" | gh secret set GMAIL_TOKEN_B64 --repo "$REPO" --body -
echo "  PUSH   GMAIL_TOKEN_B64"

echo ""
echo "All secrets pushed. Verify at: https://github.com/$REPO/settings/secrets/actions"
echo ""
echo "Trigger a manual run:"
echo "  gh workflow run pipeline.yml --repo $REPO -f mode=pipeline"
echo ""
echo "Watch the run:"
echo "  gh run watch --repo $REPO"

#!/usr/bin/env bash
# One-off: copy Orbit's production integration keys from the frontend's Vercel
# project into Secret Manager as MARKAI_ORBIT_*. Prints names only, never values.
#   bash deploy/vm/import-vercel-secrets.sh ~/projects/orbits/orbit
# The pulled file lives in a temp dir that is removed on exit. The app picks
# the secrets up on the next deploy.
set -euo pipefail

FRONTEND=${1:?usage: import-vercel-secrets.sh <path to the Vercel-linked Orbit frontend>}
HERE=$(cd "$(dirname "$0")" && pwd)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
(cd "$FRONTEND" && vercel env pull "$tmp/production.env" --environment=production --yes >/dev/null 2>&1)

# Vercel name -> backend name
MAP=(
  CLOUDINARY_CLOUD_NAME:CLOUDINARY_CLOUD_NAME
  CLOUDINARY_API_KEY:CLOUDINARY_API_KEY
  CLOUDINARY_API_SECRET:CLOUDINARY_API_SECRET
  RESEND_API_KEY:RESEND_API_KEY
  EMAIL_FROM:EMAIL_FROM
  VAPID_PRIVATE_KEY:VAPID_PRIVATE_KEY
  NEXT_PUBLIC_VAPID_PUBLIC_KEY:VAPID_PUBLIC_KEY
  VAPID_SUBJECT:VAPID_SUBJECT
)

# Prints one value from the dotenv file (KEY="..." with JSON-style escapes).
value_of() {
  python3 -I - "$tmp/production.env" "$1" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
for line in open(path, encoding="utf-8"):
    name, sep, raw = line.rstrip("\n").partition("=")
    if sep and name.strip() == key:
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = json.loads(raw)
        sys.stdout.write(raw)
        break
PY
}

for pair in "${MAP[@]}"; do
  src=${pair%%:*}; dst=${pair##*:}
  value=$(value_of "$src")
  if [ -z "$value" ]; then
    echo "skip   $dst  ($src is empty in the pull: missing, or a Sensitive var Vercel will not return)"
    continue
  fi
  printf '%s' "$value" | bash "$HERE/set-secret.sh" "$dst" >/dev/null
  echo "stored MARKAI_ORBIT_$dst  (from $src)"
done

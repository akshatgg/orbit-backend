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
  SMTP_HOST:SMTP_HOST
  SMTP_PORT:SMTP_PORT
  SMTP_USER:SMTP_USER
  SMTP_PASS:SMTP_PASS
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

# Vercel does not return the value of a Sensitive variable: `env pull` writes
# the same short placeholder for every one of them. Any value shared by two
# different variables is therefore that placeholder, never a real key.
placeholders=$(python3 -I - "$tmp/production.env" <<'PY'
import collections, json, sys
seen = collections.Counter()
for line in open(sys.argv[1], encoding="utf-8"):
    name, sep, raw = line.rstrip("\n").partition("=")
    raw = raw.strip()
    if sep and raw:
        seen[json.loads(raw) if raw.startswith('"') else raw] += 1
for value, count in seen.items():
    if count > 1:
        print(value)
PY
)

for pair in "${MAP[@]}"; do
  src=${pair%%:*}; dst=${pair##*:}
  value=$(value_of "$src")
  if [ -z "$value" ]; then
    echo "skip   $dst  ($src is empty in the pull)"
    continue
  fi
  if printf '%s\n' "$placeholders" | grep -qxF -- "$value"; then
    echo "skip   $dst  ($src is a Sensitive Vercel var; its value cannot be read back — use set-secret.sh)"
    continue
  fi
  printf '%s' "$value" | bash "$HERE/set-secret.sh" "$dst" >/dev/null
  echo "stored MARKAI_ORBIT_$dst  (from $src)"
done

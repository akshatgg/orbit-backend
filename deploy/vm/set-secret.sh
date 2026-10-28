#!/usr/bin/env bash
# Store one markai-orbit setting in Secret Manager and let the VM read it.
#   bash deploy/vm/set-secret.sh GOOGLE_CLIENT_ID
#   printf %s "$value" | bash deploy/vm/set-secret.sh GOOGLE_CLIENT_ID
# Prompts for the value without echoing it (or reads it from a pipe).
# Creates MARKAI_ORBIT_<NAME>, or
# adds a new version if it exists. The app picks it up on the next deploy
# (any push to master, or bin/deploy on the VM).
set -euo pipefail

PROJECT=markai-499512
SA="markai-orbit-runtime@$PROJECT.iam.gserviceaccount.com"
NAME=${1:?usage: set-secret.sh <NAME>   e.g. RESEND_API_KEY}
ID="MARKAI_ORBIT_$NAME"

if [ -t 0 ]; then
  read -r -s -p "Value for $NAME: " value; echo
else
  value=$(cat)
fi
[ -n "$value" ] || { echo "empty value for $NAME, nothing stored" >&2; exit 1; }

if gcloud secrets describe "$ID" --project "$PROJECT" >/dev/null 2>&1; then
  printf '%s' "$value" | gcloud secrets versions add "$ID" --project "$PROJECT" --data-file=-
else
  printf '%s' "$value" | gcloud secrets create "$ID" --project "$PROJECT" \
    --replication-policy automatic --labels app=markai-orbit --data-file=-
fi
gcloud secrets add-iam-policy-binding "$ID" --project "$PROJECT" --quiet --format=none \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor
echo "stored $ID"

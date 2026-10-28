#!/usr/bin/env bash
# One-time GCP setup for the markai-orbit VM. Run from the repo root:
#   bash deploy/vm/bootstrap-gcp.sh
# Safe to re-run: every step skips what already exists. Secrets are generated
# here and piped straight into Secret Manager; nothing is printed or written.
set -euo pipefail

PROJECT=markai-499512
REGION=us-central1
ZONE=us-central1-a
NAME=markai-orbit
SA="$NAME-runtime@$PROJECT.iam.gserviceaccount.com"
G=(--project "$PROJECT" --quiet)

step() { echo; echo "==> $*"; }

step "service account $SA"
gcloud iam service-accounts describe "$SA" "${G[@]}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$NAME-runtime" "${G[@]}" --display-name "markai-orbit VM runtime"

step "roles: log writer, image reader, backup uploader"
gcloud projects add-iam-policy-binding "$PROJECT" --quiet --condition=None --format=none \
  --member "serviceAccount:$SA" --role roles/logging.logWriter
gcloud artifacts repositories add-iam-policy-binding markai "${G[@]}" --location "$REGION" --format=none \
  --member "serviceAccount:$SA" --role roles/artifactregistry.reader
gcloud storage buckets add-iam-policy-binding gs://markai-cms-db-backups --format=none \
  --member "serviceAccount:$SA" --role roles/storage.objectCreator

step "secrets MARKAI_ORBIT_{JWT_SECRET,CRON_SECRET,POSTGRES_PASSWORD}"
for s in JWT_SECRET CRON_SECRET POSTGRES_PASSWORD; do
  id="MARKAI_ORBIT_$s"
  if ! gcloud secrets describe "$id" "${G[@]}" >/dev/null 2>&1; then
    openssl rand -hex 32 | tr -d '\n' \
      | gcloud secrets create "$id" "${G[@]}" --replication-policy automatic \
          --labels app=markai-orbit --data-file=-
  fi
  # Read access on these secrets only, not project-wide.
  gcloud secrets add-iam-policy-binding "$id" "${G[@]}" --format=none \
    --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor
done

step "static IP $NAME-ip"
gcloud compute addresses describe "$NAME-ip" "${G[@]}" --region "$REGION" >/dev/null 2>&1 \
  || gcloud compute addresses create "$NAME-ip" "${G[@]}" --region "$REGION"

step "firewall: web open, SSH via IAP only"
fw() { gcloud compute firewall-rules describe "$1" "${G[@]}" >/dev/null 2>&1 || gcloud compute firewall-rules create "$@" "${G[@]}"; }
fw "$NAME-allow-web"     --network default --direction INGRESS --priority 1000 \
   --action ALLOW --rules tcp:80,tcp:443 --source-ranges 0.0.0.0/0 --target-tags "$NAME"
fw "$NAME-allow-iap-ssh" --network default --direction INGRESS --priority 900 \
   --action ALLOW --rules tcp:22 --source-ranges 35.235.240.0/20 --target-tags "$NAME"
fw "$NAME-deny-ssh"      --network default --direction INGRESS --priority 1000 \
   --action DENY --rules tcp:22,tcp:3389 --source-ranges 0.0.0.0/0 --target-tags "$NAME"

step "VM $NAME (e2-small, 20 GB pd-balanced, Ubuntu 24.04)"
if ! gcloud compute instances describe "$NAME" "${G[@]}" --zone "$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$NAME" "${G[@]}" --zone "$ZONE" \
    --machine-type e2-small \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 20GB --boot-disk-type pd-balanced \
    --address "$NAME-ip" --tags "$NAME" --labels app=markai-orbit \
    --service-account "$SA" --scopes cloud-platform \
    --metadata-from-file startup-script=deploy/vm/startup.sh \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --deletion-protection
fi

step "daily disk snapshots (policy cms-vm-daily)"
gcloud compute disks add-resource-policies "$NAME" "${G[@]}" --zone "$ZONE" \
  --resource-policies cms-vm-daily 2>/dev/null || echo "(already attached)"

step "done"
gcloud compute instances describe "$NAME" "${G[@]}" --zone "$ZONE" \
  --format="value(name,status,networkInterfaces[0].accessConfigs[0].natIP)"

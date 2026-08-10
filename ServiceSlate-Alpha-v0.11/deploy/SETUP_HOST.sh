#!/bin/sh
set -eu
cd "$(dirname "$0")"

if ! docker compose version >/dev/null 2>&1; then
  echo "ServiceSlate shared hosting needs Docker with the Compose plugin." >&2
  exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "OpenSSL is required once to generate safe setup secrets." >&2
  exit 2
fi

if [ -f .env ]; then
  echo "ServiceSlate hosting is already configured in deploy/.env."
  printf "Start/update it now? [Y/n] "
  read answer || true
  case "${answer:-Y}" in n|N) exit 0;; esac
else
  echo "ServiceSlate Shared Hosting Setup"
  echo "This creates the private PostgreSQL database, HTTPS gateway, file store, malware scan, and backup service."
  printf "Public DNS name (example: service.company.com): "
  read domain
  if [ -z "${domain:-}" ]; then echo "A DNS name is required." >&2; exit 2; fi
  dbpass="$(openssl rand -base64 36 | tr '/+' '_-' | tr -d '\n')"
  storepass="$(openssl rand -base64 36 | tr '/+' '_-' | tr -d '\n')"
  masterkey="$(openssl rand -base64 32 | tr '/+' '_-' | tr -d '\n')"
  sessionsecret="$(openssl rand -base64 48 | tr '/+' '_-' | tr -d '\n')"
  cat > .env <<EOF
SERVICESLATE_DOMAIN=$domain
POSTGRES_DB=serviceslate
POSTGRES_USER=serviceslate
POSTGRES_PASSWORD=$dbpass
MINIO_ROOT_USER=serviceslate
MINIO_ROOT_PASSWORD=$storepass
SERVICESLATE_MASTER_KEY=$masterkey
SERVICESLATE_SESSION_SECRET=$sessionsecret
SERVICESLATE_REQUIRE_UPLOAD_SCAN=1
BACKUP_S3_ENDPOINT=http://minio:9000
BACKUP_S3_BUCKET=serviceslate
BACKUP_S3_ACCESS_KEY=serviceslate
BACKUP_S3_SECRET_KEY=$storepass
BACKUP_RETENTION_DAYS=30
EOF
  chmod 600 .env
  echo "Private setup values were generated in deploy/.env. Keep this file out of email/chat/source control."
fi

docker compose up -d --build

echo
echo "ServiceSlate is starting."
echo "Open: https://$(grep '^SERVICESLATE_DOMAIN=' .env | cut -d= -f2-)"
echo "Next: create the real admin account, run Production Readiness, configure company Google Drive/Microsoft 365, and run a restore drill."
echo "For true off-site database backup, change BACKUP_S3_* to a company-approved external S3-compatible target instead of bundled local storage."

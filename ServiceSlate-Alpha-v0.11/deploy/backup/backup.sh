#!/bin/sh
set -eu
: "${POSTGRES_HOST:=db}"
: "${POSTGRES_DB:=serviceslate}"
: "${POSTGRES_USER:=serviceslate}"
: "${BACKUP_INTERVAL_SECONDS:=86400}"
: "${BACKUP_RETENTION_DAYS:=30}"
: "${BACKUP_S3_ENDPOINT:=http://minio:9000}"
: "${BACKUP_S3_BUCKET:=serviceslate}"
: "${BACKUP_S3_PREFIX:=database-backups}"

if [ -z "${PGPASSWORD:-}" ] || [ -z "${BACKUP_S3_ACCESS_KEY:-}" ] || [ -z "${BACKUP_S3_SECRET_KEY:-}" ]; then
  echo "Backup service requires database and storage credentials" >&2
  exit 2
fi

until pg_isready -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do sleep 3; done
until mc alias set target "$BACKUP_S3_ENDPOINT" "$BACKUP_S3_ACCESS_KEY" "$BACKUP_S3_SECRET_KEY"; do sleep 3; done
mc mb --ignore-existing "target/$BACKUP_S3_BUCKET" >/dev/null

while true; do
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  file="/tmp/ServiceSlate-PostgreSQL-$stamp.dump"
  if pg_dump -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges -f "$file"; then
    sha256sum "$file" > "$file.sha256"
    mc cp "$file" "target/$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX/$(basename "$file")"
    mc cp "$file.sha256" "target/$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX/$(basename "$file.sha256")"
    echo "$(date -u +%FT%TZ) ServiceSlate PostgreSQL backup uploaded"
    mc rm --recursive --force --older-than "${BACKUP_RETENTION_DAYS}d" "target/$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX/" >/dev/null 2>&1 || true
  else
    echo "$(date -u +%FT%TZ) PostgreSQL backup FAILED" >&2
  fi
  rm -f "$file" "$file.sha256"
  sleep "$BACKUP_INTERVAL_SECONDS"
done

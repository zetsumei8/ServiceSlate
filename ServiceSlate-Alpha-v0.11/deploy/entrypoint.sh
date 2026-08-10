#!/bin/sh
set -eu
# Refresh malware signatures when possible. If the update service is temporarily
# unavailable, the container still starts and ServiceSlate health/readiness will
# expose upload-scan problems rather than deleting operational records.
if command -v freshclam >/dev/null 2>&1; then
  freshclam --quiet || echo "ClamAV signature refresh needs attention" >&2
fi
exec uvicorn serviceslate.app:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'

#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker compose down
echo "ServiceSlate containers stopped. Database and file volumes were preserved."

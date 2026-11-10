#!/usr/bin/env bash
# Expose the local-only ports to the tailnet (persists across reboots with --bg).
#   5000 MLflow · 8080 Airflow · 9000 RustFS S3 API · 9001 RustFS console
set -euo pipefail
for port in 5000 8080 9000 9001; do
  sudo tailscale serve --bg --tcp="$port" "tcp://localhost:$port"
done
tailscale serve status

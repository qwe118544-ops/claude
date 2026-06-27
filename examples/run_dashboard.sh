#!/usr/bin/env bash
# Run the trading dashboard on the simulated universe (offline, no network).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m weather_alpha.dashboard --port 8787 --markets 60 --interval 0.5

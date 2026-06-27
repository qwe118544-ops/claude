#!/usr/bin/env bash
# Offline demo — no network required. Proves the no-lookahead backtest +
# calibration pipeline recovers correct calibration on synthetic data with a
# known error model, and that forecast skill degrades with lead time.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "### Lead 1 day — strong skill, well calibrated"
python3 -m weather_alpha --source synthetic --threshold 65 --lead 1 --days 600

echo
echo "### Lead 5 days — skill degrades, EMOS widens sigma automatically"
python3 -m weather_alpha --source synthetic --threshold 65 --lead 5 --days 600

echo
echo "### Run the correctness test suite"
PYTHONPATH=. python3 tests/test_framework.py

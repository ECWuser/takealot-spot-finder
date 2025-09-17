#!/usr/bin/env bash
set -euxo pipefail
pip install -r requirements.txt
python -m playwright install --with-deps chromium

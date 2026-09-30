#!/usr/bin/env bash
# Генерация всех конфигов мониторинга из ЕДИНОГО инвентаря targets/inventory.ini:
#   targets/targets.yaml        -> ilo-exporter (web/redfish/ribcl-IML/IEL)
#   targets/hpilo-targets.json  -> hpilo-exporter через Prometheus file_sd
# Использование: ./scripts/gen-targets.sh [-i inventory] [--check]
set -euo pipefail
exec python3 "$(dirname "$0")/gen_targets.py" "$@"

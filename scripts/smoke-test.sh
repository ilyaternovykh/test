#!/usr/bin/env bash
# Smoke-тест стенда: экспортёр отвечает, метрики на месте, Prometheus/Grafana живы.
set -euo pipefail

BASE=${BASE:-http://localhost}
fail=0
check() { # name url grep-pattern
  local name=$1 url=$2 pat=${3:-}
  if body=$(curl -fsS --max-time 10 "$url" 2>/dev/null); then
    if [[ -n "$pat" ]] && ! grep -q "$pat" <<<"$body"; then
      echo "FAIL $name: нет '$pat' в ответе $url"; fail=1
    else
      echo "OK   $name"
    fi
  else
    echo "FAIL $name: $url недоступен"; fail=1
  fi
}

check "ilo-exporter /healthz"        "$BASE:9127/healthz"
check "ilo-exporter /metrics web_up" "$BASE:9127/metrics" "ilo_web_up"
check "ilo-exporter /metrics rf_up"  "$BASE:9127/metrics" "ilo_redfish_up"
check "prometheus ready"             "$BASE:9090/-/ready"
check "prometheus targets up"        "$BASE:9090/api/v1/targets" '"health":"up"'
check "grafana health"               "$BASE:3000/api/health"
check "alertmanager ready"           "$BASE:9093/-/ready"

exit $fail

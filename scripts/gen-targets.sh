#!/usr/bin/env bash
# Генератор targets.yaml по списку хостов/IP (по одному iLO в строке).
#   ./scripts/gen-targets.sh hosts.txt > targets/generated.yaml
# формат строки: <dns-name-or-ip> [display_name]
set -euo pipefail
[[ $# -eq 1 && -f $1 ]] || { echo "usage: $0 hosts.txt" >&2; exit 1; }

echo "# Сгенерировано scripts/gen-targets.sh $(date -Is)"
cat <<'HDR'
listen:
  host: 0.0.0.0
  port: 9127
  collect_interval: 60
  http_workers: 8

defaults:
  username: monitor_ro
  password: CHANGE_ME
  timeout: 15
  verify_tls: false
  enabled_collectors: [web, redfish]
  extra_labels:
    model: dl380p-gen8

targets:
HDR

n=0
while read -r host name _; do
  [[ -z "$host" || "$host" == \#* ]] && continue
  n=$((n+1))
  dn=${name:-$(printf 'dl380g8-%02d' "$n")}
  url="$host"; [[ "$url" != http* ]] && url="https://$host"
  printf '  - {name: %s, url: "%s", labels: {idx: "%02d"}}\n' "$dn" "$url" "$n"
done < "$1"
echo "# всего targets: $n" >&2

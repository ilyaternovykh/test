#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор конфигов мониторинга из ЕДИНОГО инвентаря targets/inventory.ini.

Один файл с IP и именами серверов -> три потребителя:
  1. targets/targets.yaml         — конфиг нашего ilo-exporter;
  2. targets/hpilo-targets.json   — file_sd для Prometheus (hpilo-exporter);
  3. docker/prometheus/prometheus.yml — перечитать file_sd не нужно, он сам подхватит.

Использование:
    ./scripts/gen-targets.sh                 # из targets/inventory.ini
    ./scripts/gen-targets.sh -i other.ini    # свой инвентарь
    ./scripts/gen-targets.sh --check         # только валидация без записи

Формат инвентаря см. в комментариях targets/inventory.ini.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KNOWN_KEYS = {"ansible_host", "host_header", "rack", "model", "site",
              "user", "pass", "collectors", "ilo_version", "ribcl"}


def parse_inventory(path: str) -> tuple[dict, list[dict]]:
    """Ansible-подобный INI: [defaults] key=value; [servers] имя ansible_host=IP ..."""
    if not os.path.isfile(path):
        sys.exit(f"inventory not found: {path}")
    defaults: dict[str, str] = {}

    servers: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    section = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
            continue
        if section == "defaults":
            k, _, v = s.partition("=")
            defaults[k.strip()] = v.strip()
            continue
        if section != "servers":
            continue
        toks = s.split()
        name = toks[0]
        kv = {}
        for token in toks[1:]:
            if "=" not in token:
                sys.exit(f"inventory[{name}]: token '{token}' is not key=value")
            k, v = token.split("=", 1)
            kv[k] = v
        unknown = set(kv) - KNOWN_KEYS
        if unknown:
            sys.exit(f"inventory[{name}]: unknown keys {sorted(unknown)}")
        host = kv.get("ansible_host")
        if not host:
            sys.exit(f"inventory[{name}]: ansible_host (IP/DNS iLO) is required")
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        s = {
            "name": name,
            "url": host,
            "host_header": kv.get("host_header", ""),
            "rack": kv.get("rack", defaults.get("rack", "")),
            "model": kv.get("model", defaults.get("model", "dl380p-gen8")),
            "site": kv.get("site", defaults.get("site", "")),
            "user": kv.get("user", ""),
            "pass": kv.get("pass", ""),
            "collectors": [c.strip() for c in
                           kv.get("collectors", defaults.get("collectors", "web,redfish")).split(",") if c.strip()],
            "ilo_version": kv.get("ilo_version", defaults.get("ilo_version", "ilo4")),
            "ribcl": kv.get("ribcl", defaults.get("ribcl", "")).lower() in ("1", "true", "yes"),
        }
        if s["ribcl"] and "ribcl" not in s["collectors"]:
            s["collectors"].append("ribcl")
        servers.append(s)
    return defaults, servers


def gen_exporter_yaml(defaults: dict, servers: list[dict]) -> str:
    """targets.yaml для нашего ilo-exporter."""
    lines = [
        "# СГЕНЕРИРОВАНО scripts/gen-targets.sh из targets/inventory.ini — не редактируйте вручную!",
        "",
        "listen:",
        "  host: 0.0.0.0",
        "  port: 9127",
        "  path: /metrics",
        "  collect_interval: 60",
        "  http_workers: 8",
        "",
        "defaults:",
        "  username: ${ILO_USER}",
        "  password: ${ILO_PASS}",
        "  timeout: 15",
        "  verify_tls: false",
        "  enabled_collectors: [web, redfish]",
        "  # подмена Host разрешена только на тестовом стенде (mock-парк);",
        "  # для реальных целей поле host_header пустое -> Host берётся из URL",
        "  allow_sni_mismatch: true",
        f"  extra_labels:",
        f"    model: {defaults.get('model', 'dl380p-gen8')}",
        f"    site: {defaults.get('site', 'lab')}",
        "",
        "targets:",
    ]
    for s in servers:
        lines.append(f"  - name: {s['name']}")
        lines.append(f"    url: \"{s['url']}\"")
        if s["host_header"]:
            lines.append(f"    host_header: {s['host_header']}")
        labels = {k: v for k, v in (("rack", s["rack"]), ("model", s["model"]), ("site", s["site"])) if v}
        lines.append("    labels: " + json.dumps(labels, ensure_ascii=False))
        lines.append(f"    enabled_collectors: [{', '.join(s['collectors'])}]")
        if s["user"]:
            lines.append(f"    username: {s['user']}")
        if s["pass"]:
            lines.append(f"    password: {s['pass']}")
        if "ribcl" in s["collectors"]:
            lines.append('    ribcl_enabled: true')
            lines.append('    ribcl_command: "GET IML"')
    lines.append(f"# всего targets: {len(servers)}")
    return "\n".join(lines) + "\n"


def gen_hpilo_json(defaults: dict, servers: list[dict]) -> str:
    """file_sd конфигурация для hpilo-exporter (github.com/hpilo-exporter/hpilo-exporter).

    hpilo-exporter работает по схеме on-demand-proxy-scrape:
      GET http://hpilo-exporter:9416/metrics?ilo_host=<IP>&ilo_port=<порт>
    Секреты (ilo_user/ilo_password) — через ENV контейнера экспортёра,
    в файл sd они НЕ попадают.

    Labels: server/target/name совпадают с метками нашего ilo-exporter,
    поэтому переменная $server и алерты работают по всему парку одинаково.
    Дашборд Grafana 13709 (hp-ilo) использует легенду {{ilo_host}} —
    label ilo_host проставляется здесь же.
    """
    out = []
    for s in servers:
        hostport = s["url"].split("//", 1)[-1].rstrip("/")
        host, _, port = hostport.partition(":")
        entry = {
            "targets": ["hpilo-exporter:9416"],
            "labels": {
                "server": s["name"],
                "target": s["name"],
                "name": s["name"],
                "ilo_host": host,
                "ilo_addr": hostport,
                "rack": s["rack"],
                "model": s["model"],
                "site": s["site"],
            },
            "params": {"ilo_host": [host]},
        }
        if port:
            entry["params"]["ilo_port"] = [port]
        out.append(entry)
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-i", "--inventory", default=os.path.join(REPO, "targets", "inventory.ini"))
    ap.add_argument("--check", action="store_true", help="только валидация")
    args = ap.parse_args(argv)

    defaults, servers = parse_inventory(args.inventory)
    if not servers:
        sys.exit("inventory: no servers parsed")

    yaml_path = os.path.join(REPO, "targets", "targets.yaml")
    json_path = os.path.join(REPO, "targets", "hpilo-targets.json")

    body_yaml = gen_exporter_yaml(defaults, servers)
    body_json = gen_hpilo_json(defaults, servers)

    if args.check:
        print(f"OK: {len(servers)} servers; exporters config valid")
        return 0

    with open(yaml_path, "w", encoding="utf-8") as fh:
        fh.write(body_yaml)
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(body_json)
    print(f"wrote {yaml_path} ({len(servers)} targets)")
    print(f"wrote {json_path} ({len(servers)} file_sd groups)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

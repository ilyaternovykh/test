"""Конфигурация ilo-exporter.

Источники (по убыванию приоритета):
  1. Переменные окружения (ILO_EXPORTER_*)
  2. YAML-файл (--config / $ILO_EXPORTER_CONFIG)
  3. Значения по умолчанию

Пример YAML:

    listen:
      host: 0.0.0.0
      port: 9127
      path: /metrics

    defaults:
      username: monitor
      password: secret
      timeout: 15
      verify_tls: false          # iLO самоподписанный сертификат
      enabled_collectors: [web, redfish]
      extra_labels: {}

    targets:
      - name: dl380g8-01
        url: https://ilo-dl380g8-01.lab.local
        labels:
          room: a1
          rack: r04
      - name: dl380g8-02
        url: https://10.10.5.2
        username: ro_user
        password: ro_pass
        enabled_collectors: [web]     # только проверка доступности web iLO

      # задел на будущее: Gen9+/iLO5 умеют RIBCL через Redfish
      - name: dl360g10-01
        url: https://ilo-dl360g10-01.lab.local
        ribcl_enabled: true
        ribcl_command: "GET IML"      # или "GET IEL"
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from typing import Any

DEFAULTS: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 9127,
    "path": "/metrics",
    "collect_interval": 60,
    "http_workers": 8,
    "username": "",
    "password": "",
    "timeout": 15,
    "verify_tls": False,
    "enabled_collectors": ["web", "redfish"],
    "extra_labels": {},
    "ribcl_enabled": False,
    "ribcl_command": "",
    "labels": {},
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


@dataclass
class TargetConfig:
    """Один наблюдаемый iLO."""

    name: str
    url: str
    username: str = ""
    password: str = ""
    timeout: float = 15
    verify_tls: bool = False
    enabled_collectors: list[str] = field(default_factory=lambda: ["web", "redfish"])
    labels: dict[str, str] = field(default_factory=dict)
    # --- задел на будущее: IML / IEL через virtual media RIBCL (Gen9+/iLO5) ---
    ribcl_enabled: bool = False
    ribcl_command: str = ""
    # виртуальный Host для HTTP-запросов (тестовый стенд: один mock на весь парк)
    host_header: str = ""

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")


@dataclass
class Config:
    host: str = DEFAULTS["host"]
    port: int = DEFAULTS["port"]
    path: str = DEFAULTS["path"]
    collect_interval: int = DEFAULTS["collect_interval"]
    http_workers: int = DEFAULTS["http_workers"]
    targets: list[TargetConfig] = field(default_factory=list)

    def target_names(self) -> list[str]:
        return [t.name for t in self.targets]


def _load_yaml(path: str) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            f"Не могу прочитать {path}: pyyaml не установлен "
            "(в docker-образе установлен; локально: pip install pyyaml)"
        ) from exc
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Config file {path}: ожидался mapping в корне документа")
    return _expand_env(data)


def _expand_env(obj: Any) -> Any:
    """Рекурсивно подставляет ${VAR} / $VAR из окружения в строки конфига.

    Позволяет хранить секреты вне YAML: username: ${ILO_USER}.
    Если переменная окружения не задана, os.path.expandvars оставляет
    ссылку как есть — в этом случае она заменяется на пустую строку.
    """
    if isinstance(obj, str):
        expanded = os.path.expandvars(obj)
        if "$" in expanded:  # осталась неразвёрнутая ссылка ${VAR}/$VAR
            expanded = re.sub(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*", "", expanded)
        return expanded
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    return obj


def _merge_target(raw: dict, defaults: dict, index: int) -> TargetConfig:
    merged = {
        k: defaults.get(k, DEFAULTS[k])
        for k in (
            "username", "password", "timeout", "verify_tls",
            "enabled_collectors", "ribcl_enabled", "ribcl_command", "host_header",
        )
    }
    merged.update({k: v for k, v in raw.items() if k != "labels"})

    url = str(merged.get("url", "")).strip()
    if not url:
        raise RuntimeError(f"target #{index}: поле 'url' обязательно")
    name = str(merged.get("name") or url.split("//")[-1].split("/")[0])

    collectors = merged.get("enabled_collectors") or []
    if isinstance(collectors, str):
        collectors = [c.strip() for c in collectors.split(",") if c.strip()]

    labels = dict(defaults.get("extra_labels") or {})
    labels.update(raw.get("labels") or {})

    return TargetConfig(
        name=name,
        url=url,
        username=str(merged.get("username") or ""),
        password=str(merged.get("password") or ""),
        timeout=float(merged.get("timeout", 15)),
        verify_tls=_as_bool(merged.get("verify_tls", False)),
        enabled_collectors=[str(c) for c in collectors],
        labels={str(k): str(v) for k, v in labels.items()},
        ribcl_enabled=_as_bool(merged.get("ribcl_enabled", False)),
        ribcl_command=str(merged.get("ribcl_command") or ""),
        host_header=str(merged.get("host_header") or ""),
    )


def load_config(path: str | None = None) -> Config:
    cfg = Config()

    listen: dict = {}
    defaults: dict = {}
    raw_targets: list = []

    # 1) YAML
    path = path or os.environ.get("ILO_EXPORTER_CONFIG")
    if path and os.path.exists(path):
        data = _load_yaml(path)
        listen = data.get("listen") or {}
        defaults = data.get("defaults") or {}
        raw_targets = data.get("targets") or []

    # 2) ENV overrides
    env = os.environ
    cfg.host = env.get("ILO_EXPORTER_HOST", str(listen.get("host", cfg.host)))
    cfg.port = _as_int(env.get("ILO_EXPORTER_PORT"), int(listen.get("port", cfg.port)))
    cfg.path = env.get("ILO_EXPORTER_PATH", str(listen.get("path", cfg.path)))
    cfg.collect_interval = _as_int(
        env.get("ILO_EXPORTER_COLLECT_INTERVAL"),
        int(listen.get("collect_interval", defaults.get("collect_interval", cfg.collect_interval))),
    )
    cfg.http_workers = _as_int(
        env.get("ILO_EXPORTER_HTTP_WORKERS"),
        int(listen.get("http_workers", cfg.http_workers)),
    )

    # 3) targets: из YAML, либо из JSON в ILO_EXPORTER_TARGETS
    if not raw_targets:
        inline = env.get("ILO_EXPORTER_TARGETS", "").strip()
        if inline:
            try:
                raw_targets = json.loads(inline)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"ILO_EXPORTER_TARGETS: некорректный JSON: {exc}") from exc
        # 4) одиночный target через ILO_URL/ILO_USERNAME/ILO_PASSWORD — для тестов
        if not raw_targets and env.get("ILO_URL"):
            raw_targets = [{"name": env.get("ILO_NAME", "ilo"), "url": env["ILO_URL"]}]

    seen: set[str] = set()
    for idx, raw in enumerate(raw_targets, start=1):
        t = _merge_target(dict(raw), defaults, idx)
        if t.name in seen:
            raise RuntimeError(f"Дублирующееся имя target '{t.name}'")
        seen.add(t.name)
        cfg.targets.append(t)

    return cfg

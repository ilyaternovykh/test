# -*- coding: utf-8 -*-
"""Базовые примитивы для коллекторов ilo-exporter.

Каждый коллектор:
  * получает на вход TargetConfig и Session (HTTP-клиент с переиспользованием соединений);
  * возвращает список Metric;
  * никогда не бросает исключение наружу — ошибочный путь показывается
    метрикой <prefix>_collect_success = 0 (см. run_collector).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def sanitize_name(name: str) -> str:
    """Приводит имя элемента iLO к валидному значению label Prometheus."""
    out = re.sub(r"[^A-Za-z0-9_.:/-]+", "_", str(name).strip())
    return out.strip("_") or "unknown"


@dataclass
class Metric:
    name: str
    value: float
    labels: dict = field(default_factory=dict)
    mtype: str = "gauge"          # gauge | counter | untyped
    help: str = ""
    unit: str = ""                # prometheus unit, напр. 'seconds', 'celsius'

    def sample(self) -> tuple:
        return (self.name, self.labels, float(self.value), self.mtype, self.help, self.unit)


class Session:
    """Тонкая обёртка над requests.Session c базовым URL и basic-auth."""

    def __init__(self, base_url: str, username: str = "", password: str = "",
                 timeout: float = 15, verify: bool = False, host_header: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if verify:
            self.session.verify = True
        else:
            self.session.verify = False
        if username:
            self.session.auth = (username, password)
        self.session.headers.update({
            "User-Agent": "ilo-exporter/1.0",
            "Accept": "application/json",
        })
        if host_header:
            # Виртуальный хост: один эндпоинт может эмулировать несколько iLO
            # (mock-ilo для теста парка из N серверов).
            # ВАЖНО: requests/urllib3 игнорируют заголовок "Host" в session.headers
            # (он управляется на уровне соединения), поэтому подмена Host
            # выполняется через transport adapter, переписывающий PreparedRequest.
            self.session.mount("http://", _HostOverrideAdapter(host_header))
            self.session.mount("https://", _HostOverrideAdapter(host_header))

    def get(self, path: str, timeout: Optional[float] = None,
            headers: Optional[dict] = None, stream: bool = False):
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        return self.session.get(url, timeout=timeout or self.timeout,
                                headers=headers, stream=stream)

    def head(self, path: str, timeout: Optional[float] = None,
             allow_redirects: bool = True):
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        return self.session.head(url, timeout=timeout or self.timeout,
                                 allow_redirects=allow_redirects)


class _HostOverrideAdapter(requests.adapters.HTTPAdapter):
    """Транспорт, подменяющий HTTP-заголовок Host (виртуальные хосты)."""

    def __init__(self, host_header: str, *args, **kwargs):
        self._host_header = host_header
        super().__init__(*args, **kwargs)

    def send(self, request, *args, **kwargs):
        request.headers["Host"] = self._host_header
        return super().send(request, *args, **kwargs)


@dataclass
class CollectorResult:
    metrics: list
    error: str = ""
    duration: float = 0.0


def run_collector(fn: Callable, target, prefix: str) -> CollectorResult:
    """Выполняет коллектор, конвертируя любые исключения в collect_success=0."""
    started = time.monotonic()
    try:
        metrics = fn(target) or []
        success, err = 1.0, ""
    except requests.RequestException as exc:
        metrics, success = [], 0.0
        err = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"[:200]
    except Exception as exc:  # noqa: BLE001 - коллектор не должен ронять экспортёр
        metrics, success = [], 0.0
        err = f"{type(exc).__name__}: {exc}"[:200]
    duration = time.monotonic() - started

    base_labels = {"target": target.name, "collector": prefix}
    metrics.append(Metric(f"{prefix}_collect_duration_seconds", duration,
                          dict(base_labels), help=f"Длительность сбора {prefix}"))
    metrics.append(Metric(f"{prefix}_collect_success", success, dict(base_labels),
                          help="1 — сбор прошёл успешно, 0 — ошибка"))
    if err:
        metrics.append(Metric(f"{prefix}_collect_error_info", 1,
                              {**base_labels, "error": err}, "untyped",
                              help="Текст последней ошибки сбора"))
    return CollectorResult(metrics=metrics, error=err, duration=duration)


def first_number(source: dict, *keys: str) -> Optional[float]:
    """Возвращает первое встречаемое числовое значение среди ключей словаря."""
    for key in keys:
        val = source.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                pass
    return None


def status_to_bool(value: Any) -> Optional[float]:
    """ОК/Enabled/True -> 1, иначе 0; None если распознать не удалось."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if text in ("ok", "enabled", "true", "yes", "good", "intest", "normal", "online"):
        return 1.0
    if text in ("disabled", "false", "no", "offline", "absent"):
        return 0.0
    return None

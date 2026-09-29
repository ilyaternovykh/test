# -*- coding: utf-8 -*-
"""Базовые примитивы для коллекторов ilo-exporter.

Каждый коллектор:
  * получает на вход TargetConfig и Session (HTTP-клиент с переиспользованием соединений);
  * возвращает список Metric;
  * никогда не бросает исключение наружу — ошибочный путь показывается
    метрикой <prefix>_collect_success = 0 (см. run_collector).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

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
    """Тонкая обёртка над requests.Session c базовым URL и basic-auth.

    ВАЖНО для реальных iLO4 (Gen8/Gen9): прошивка iLO жёстко проверяет
    заголовок Host и отвечает 400 Bad Request на «чужой» (IP-адрес, имя
    контейнера и т.п.). Поэтому по умолчанию в Host всегда отправляется
    каноническое имя хоста из URL цели — запросы к реальным iLO работают
    как раньше. Подмена Host на виртуальное имя нужна только тестовому
    стенду (один mock-контейнер эмулирует весь парк) — включается флагом
    allow_sni_mismatch и не влияет на дефолтное поведение.
    """

    def __init__(self, base_url: str, username: str = "", password: str = "",
                 timeout: float = 15, verify: bool = False, host_header: str = "",
                 allow_sni_mismatch: bool = False):
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
        # Canonical Host из URL цели — то, что реально ожидает iLO.
        canonical_host = urlsplit(self.base_url).netloc
        if host_header and allow_sni_mismatch:
            # Тестовый стенд: виртуальный хост при другом фактическом IP/SNI.
            self.session.mount("http://", _HostOverrideAdapter(host_header))
            self.session.mount("https://", _HostOverrideAdapter(host_header))
        elif host_header and canonical_host.split(":")[0] != host_header.split(":")[0]:
            # Реальный сервер, но в конфиге проставлен host_header (например,
            # скопированный из примера мока): НЕ подменяем Host — иначе iLO4
            # вернёт 400 и все метрик-коллекторы умрут. Предупреждаем один раз.
            logging.getLogger("ilo-exporter").warning(
                "host_header=%r не совпадает с хостом URL %r — подмена Host отключена "
                "(поле 'host_header' нужно только для mock-стенда; для реальных iLO "
                "оставьте его пустым)", host_header, base_url)

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


# (Классы соединений не требуются: urllib3>=2 сам пропускает автоматический
# Host, если он присутствует в заголовках запроса — см. _HostOverrideAdapter.send)


class _HostOverrideAdapter(requests.adapters.HTTPAdapter):
    """Транспорт для mock-стенда: свой Host при чужом IP/TLS-сервере.

    requests/urllib3 игнорируют "Host" в session.headers (заголовок
    управляется на уровне соединения), поэтому подмена выполняется через
    ConnectionCls пулов. Реальные iLO этот путь не используют: там Host
    всегда канонический из URL цели (см. Session).
    """

    def __init__(self, host_header: str, *args, **kwargs):
        self._host_header = host_header
        super().__init__(*args, **kwargs)

    def send(self, request, *args, **kwargs):
        # requests кладёт "Host" в заголовки; urllib3 (>=1.26/2.x) видит его и
        # пропускает автоматический Host соединения -> на провод уходит наш.
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

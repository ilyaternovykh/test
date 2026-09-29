#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ilo-exporter — единый экспортёр метрик iLO для парка серверов HPE.

Режимы работы (см. docs/ARCHITECTURE.md):
  * push-режим (рекомендуется, «один сборщик на сервер»):
        фоновый цикл опрашивает все targets каждые ILO_EXPORTER_COLLECT_INTERVAL
        секунд и публикует общий snapshot на /metrics;
  * scrape-per-request (опционально): GET /metrics?target=dl380g8-01 —
        собирает данные только по одному target'у прямо в момент запроса.

Запуск:
    python3 -m exporter --config /etc/ilo-exporter/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import (CONTENT_TYPE_LATEST, CollectorRegistry, core,
                               generate_latest)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily


class FamilyCollector:
    """Коллектор одного семейства метрик для произвольного CollectorRegistry.

    Использует GaugeMetricFamily/CounterMetricFamily — официальный API
    prometheus_client для кастомных коллекторов (совместим со всеми
    версиями библиотеки).
    """

    def __init__(self, name: str, mtype: str, documentation: str, unit: str = ""):
        self.name = name
        self.mtype = "counter" if mtype == "counter" else "gauge"
        self.documentation = documentation or name
        self.unit = unit
        self.samples: list[tuple[dict, float]] = []

    def add(self, labels: dict, value: float) -> None:
        self.samples.append((labels, float(value)))

    def collect(self):
        labelnames = sorted({k for lbl, _ in self.samples for k in lbl})
        cls = CounterMetricFamily if self.mtype == "counter" else GaugeMetricFamily
        kwargs = {"name": self.name, "documentation": self.documentation,
                  "labels": labelnames}
        if cls is GaugeMetricFamily and self.unit:
            kwargs["unit"] = self.unit
        try:
            fam = cls(**kwargs)
        except TypeError:  # pragma: no cover - старые версии без unit=
            fam = cls(self.name, self.documentation, labels=labelnames)
        for lbl, val in self.samples:
            fam.add_metric([lbl.get(k, "") for k in labelnames], val)
        return [fam]

from .config import Config, TargetConfig, load_config
from .collectors import REGISTRY, resolve

LOG = logging.getLogger("ilo-exporter")


class Snapshot:
    """Потокобезопасное хранилище последнего результата сбора."""

    def __init__(self):
        self._lock = threading.Lock()
        self._registry: CollectorRegistry | None = None
        self._payload: bytes = b""
        self.updated: float = 0.0
        self.scrape_errors: int = 0

    def publish(self, registry: CollectorRegistry, payload: bytes) -> None:
        with self._lock:
            self._registry = registry
            self._payload = payload
            self.updated = time.time()

    def render(self) -> tuple[bytes, str]:
        with self._lock:
            return self._payload, CONTENT_TYPE_LATEST


class CollectorLoop(threading.Thread):
    """Фоновый обход всех targets с переиспользованием пула потоков."""

    daemon = True

    def __init__(self, cfg: Config, snapshot: Snapshot):
        super().__init__(name="collector-loop")
        self.cfg = cfg
        self.snapshot = snapshot
        self.stop_event = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max(cfg.http_workers, 2))

    # ---------- сбор одного target ----------
    def _collect_target(self, target: TargetConfig) -> list:
        metrics: list = []
        collectors = resolve(target.enabled_collectors or list(REGISTRY), target)
        for name, fn in collectors.items():
            try:
                metrics.extend(fn(target))
            except Exception as exc:  # noqa: BLE001
                LOG.exception("collector %s/%s упал: %s", target.name, name, exc)
        return metrics

    # ---------- сбор всего парка ----------
    def run_once(self) -> None:
        started = time.monotonic()
        registry = CollectorRegistry()

        all_metrics: list = []
        futures = {self._pool.submit(self._collect_target, t): t for t in self.cfg.targets}
        for fut, tgt in futures.items():
            try:
                all_metrics.extend(fut.result(timeout=self.cfg.collect_interval))
            except Exception as exc:  # noqa: BLE001
                LOG.error("target %s: сбор не завершился: %s", tgt.name, exc)
                self.snapshot.scrape_errors += 1

        families: dict[str, FamilyCollector] = {}
        for m in all_metrics:
            fam = families.get(m.name)
            if fam is None:
                fam = FamilyCollector(m.name, m.mtype, m.help, m.unit)
                families[m.name] = fam
            fam.add(m.labels, m.value)

        for name, fam in sorted(families.items()):
            try:
                registry.register(fam)
            except ValueError as exc:
                LOG.warning("метрика %s пропущена: %s", name, exc)

        # служебные метрики самого экспортёра
        duration = time.monotonic() - started
        for name, doc, value in (
            ("ilo_exporter_targets", "Число настроенных targets", float(len(self.cfg.targets))),
            ("ilo_exporter_last_run_duration_seconds",
             "Длительность полного цикла сбора", duration),
            ("ilo_exporter_last_run_timestamp_seconds",
             "Unix-время завершения последнего цикла", time.time()),
        ):
            registry.register(_ConstGauge(name, doc, value))
        build = FamilyCollector("ilo_exporter_build_info", "gauge", "Сборка экспортёра")
        build.add({"version": "1.0.0"}, 1)
        registry.register(build)

        self.snapshot.publish(registry, generate_latest(registry))
        LOG.info("cycle done: %d targets, %d metric families, %.2fs",
                 len(self.cfg.targets), len(families), duration)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                LOG.exception("цикл сбора завершился аварийно")
            self.stop_event.wait(self.cfg.collect_interval)

    def shutdown(self) -> None:
        self.stop_event.set()
        self._pool.shutdown(wait=False)


class _ConstGauge:
    """Коллектор одной константной gauge-метрики без лейблов."""

    def __init__(self, name: str, doc: str, value: float):
        self._name, self._doc, self._value = name, doc, value

    def collect(self):
        fam = GaugeMetricFamily(self._name, self._doc)
        fam.add_metric([], self._value)
        return [fam]


class Handler(BaseHTTPRequestHandler):
    server_version = "ilo-exporter/1.0"
    cfg: Config = None            # внедряется из main()
    snapshot: Snapshot = None     # внедряется из main()
    loop: CollectorLoop = None    # внедряется из main()

    def log_message(self, fmt, *args):  # тишина в access-логе
        LOG.debug("http: " + fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            names = ", ".join(t.name for t in self.cfg.targets)
            html = (f"<html><body><h2>ilo-exporter</h2>"
                    f"<p><a href='{self.cfg.path}'>Metrics</a></p>"
                    f"<p>targets ({len(self.cfg.targets)}): {names}</p></body></html>")
            return self._send(200, html.encode())

        if parsed.path == "/healthz":
            age = time.time() - self.snapshot.updated
            ok = self.snapshot.updated and age < self.cfg.collect_interval * 3
            status = b"OK\n" if ok else b"STALE\n"
            return self._send(200 if ok else 503, status)

        if parsed.path.rstrip("/") == self.cfg.path.rstrip("/"):
            single = qs.get("target", [""])[0].strip()
            if single:
                # scrape-per-request для конкретного target
                tgt = next((t for t in self.cfg.targets if t.name == single), None)
                if tgt is None:
                    return self._send(404, f"unknown target '{single}'\n".encode())
                metrics = self.loop._collect_target(tgt)
                registry = CollectorRegistry()
                families: dict[str, FamilyCollector] = {}
                for m in metrics:
                    fam = families.get(m.name)
                    if fam is None:
                        fam = FamilyCollector(m.name, m.mtype, m.help, m.unit)
                        families[m.name] = fam
                    fam.add(m.labels, m.value)
                for fam in families.values():
                    try:
                        registry.register(fam)
                    except ValueError:
                        pass
                return self._send(200, generate_latest(registry), CONTENT_TYPE_LATEST)

            payload, ctype = self.snapshot.render()
            if not payload:
                return self._send(503, b"no data yet\n")
            return self._send(200, payload, ctype)

        return self._send(404, b"not found\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HPE iLO metrics exporter")
    ap.add_argument("--config", help="путь к YAML-конфигурации targets")
    ap.add_argument("--once", action="store_true", help="один цикл сбора и выход")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        LOG.error("конфигурация: %s", exc)
        return 2
    if not cfg.targets:
        LOG.error("не задано ни одного target'а (см. targets/targets.yaml.example)")
        return 2

    snapshot = Snapshot()
    loop = CollectorLoop(cfg, snapshot)

    if args.once:
        loop.run_once()
        print(snapshot.render()[0].decode(errors="replace"))
        return 0

    Handler.cfg, Handler.snapshot, Handler.loop = cfg, snapshot, loop
    loop.start()

    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    LOG.info("listening on http://%s:%d%s (%d targets, interval %ds)",
             cfg.host, cfg.port, cfg.path, len(cfg.targets), cfg.collect_interval)

    def _shutdown(signum, frame):  # noqa: ARG001
        LOG.info("сигнал %s — останавливаюсь", signum)
        loop.shutdown()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        httpd.serve_forever()
    finally:
        loop.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

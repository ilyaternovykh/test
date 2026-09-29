# -*- coding: utf-8 -*-
"""Коллектор доступности web-интерфейса iLO.

Отвечает на вопрос «жив ли сервер вообще»: HTTP(S)-запрос к корню web iLO
с базовой авторизацией. Работает на любом поколении iLO (2/4/5) и не требует
Redfish — поэтому это fallback-проверка для всех 22 серверов.

Метрики:
    ilo_web_up{target}                       1 если веб-интерфейс отвечает
    ilo_web_response_time_seconds{target}    время ответа (0 при ошибке)
    ilo_web_ssl_verify_ok{target}            1 если TLS-цепочка доверяется системе
    ilo_web_cert_expire_days{target}         дней до истечения сертификата iLO
    ilo_web_info{target, status_code, ...}   1, служебная (для подписи в Grafana)
"""

from __future__ import annotations

import datetime as dt
import ssl

from ..base import Metric, Session, run_collector

PREFIX = "ilo_web"


def _ssl_probe(target) -> tuple[float, float | None]:
    """Проверяет, доверяется ли сертификат системой, и читает срок действия."""
    verify_ok, expire_days = 0.0, None
    try:
        import socket
        host = target.base_url.split("//")[-1].split("/")[0]
        hostname, _, explicit = host.partition(":")
        port = int(explicit) if explicit else 443

        ctx = ssl.create_default_context()          # строгая проверка цепочки
        try:
            with socket.create_connection((hostname, port), timeout=target.timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=hostname) as tls:
                    verify_ok = 1.0
                    cert = tls.getpeercert()
        except ssl.SSLCertVerificationError:
            verify_ok = 0.0
            # сертификат самоподписанный — получим срок действия без проверки
            loose = ssl.create_default_context()
            loose.check_hostname = False
            loose.verify_mode = ssl.CERT_NONE
            with socket.create_connection((hostname, port), timeout=target.timeout) as raw:
                with loose.wrap_socket(raw, server_hostname=hostname) as tls:
                    der = tls.getpeercert(binary_form=True)
            if der:
                parsed = ssl.DER_cert_to_PEM_cert(der)
                # простой разбор notAfter через cryptography недоступен → используем ssl
                import re as _re
                m = _re.search(r"notAfter=([A-Za-z]{3} +\d+ \d{2}:\d{2}:\d{2} \d{4})", parsed)
                if m:
                    when = dt.datetime.strptime(m.group(1), "%b %d %H:%M:%S %Y")
                    expire_days = (when - dt.datetime.utcnow()).total_seconds() / 86400.0
    except Exception:
        pass
    return verify_ok, expire_days


def _collect(target) -> list[Metric]:
    labels = {"target": target.name, **target.labels}
    metrics: list[Metric] = []

    session = Session(target.base_url, target.username, target.password,
                      timeout=min(target.timeout, 10), verify=False,
                      host_header=getattr(target, "host_header", ""),
                      allow_sni_mismatch=getattr(target, "allow_sni_mismatch", False))
    up, resp_time, code, location = 0.0, 0.0, "0", ""
    try:
        resp = session.get("/", timeout=min(target.timeout, 10))
        resp_time = resp.elapsed.total_seconds()
        code = str(resp.status_code)
        location = resp.headers.get("X-iLO-Server-Type", "") or resp.headers.get("Server", "")
        # iLO2 отдаёт 200/302 на логин; считаем веб живым при любом валидном HTTP-ответе
        if resp.status_code < 500:
            up = 1.0
    except Exception:
        # HEAD как запасной вариант (некоторые прошивки не любят GET / с авторизацией)
        try:
            resp = session.head("/", timeout=min(target.timeout, 10))
            resp_time = resp.elapsed.total_seconds()
            code = str(resp.status_code)
            if resp.status_code < 500:
                up = 1.0
        except Exception:
            pass

    metrics.append(Metric(f"{PREFIX}_up", up, labels,
                          help="1 — web-интерфейс iLO доступен"))
    metrics.append(Metric(f"{PREFIX}_response_time_seconds", resp_time, labels,
                          help="Время ответа web iLO, сек"))
    # status_code в info-метрике только при реальном HTTP-ответе
    info_labels = dict(labels)
    if code != "0":
        info_labels["status_code"] = code
    info_labels["server"] = sanitize(location)
    metrics.append(Metric(f"{PREFIX}_info", 1, info_labels,
                          "untyped", help="Информационная метрика web iLO"))

    verify_ok, expire_days = _ssl_probe(target)
    metrics.append(Metric(f"{PREFIX}_ssl_verify_ok", verify_ok, labels,
                          help="1 — сертификат iLO доверяется системному хранилищу CA"))
    if expire_days is not None:
        metrics.append(Metric(f"{PREFIX}_cert_expire_days", expire_days, labels,
                              help="Дней до истечения сертификата iLO"))
    return metrics


def sanitize(value: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))[:64] or "unknown"


def collect(target) -> list[Metric]:
    return run_collector(_collect, target, PREFIX).metrics

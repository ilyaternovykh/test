# -*- coding: utf-8 -*-
"""RIBCL-коллектор — задел на будущее для сбора IML и IEL.

Почему это «задел»:
  * На Gen8 (iLO2) полный дамп IML/IEL доступен только по порту 443 через
    XML-протокол RIBCL (login/password + <RIBCL>), либо SNMP (HP iLO MIB).
  * Начиная с iLO4 (Gen9) HP добавляет «Virtual Media RIBCL» через Redfish:
      POST /redfish/v1/SessionService/Sessions -> X-Auth-Token
      GET  /redfish/v1/Managers/iLO/RIBCL      -> тело = вывод команды RIBCL
    Поэтому код ниже умеет и то, и другое; включается per-target флагом
    ribcl_enabled=true в конфигурации экспортёра.

Команды: "GET IML" (Integrated Management Log), "GET IEL" (Instant Event Log),
а также любая RIBCL-цепочка ("GET ALL CONFIG", "LOGIN ... OUT ...").

Метрики:
    ilo_ribcl_up{target}                          1 — удалось получить вывод
    ilo_ribcl_response_time_seconds{target}       длительность
    ilo_ribcl_iml_entries{target}                 число строк-событий в дампе IML
    ilo_ribcl_iel_entries{target}                 то же для IEL
    ilo_ribcl_dump_bytes{target}                  размер полученного дампа

Сырые тексты логов НЕ публикуются как метрики (высокая кардинальность).
Для полноценного хранения IML/IEL используйте Loki + promtail/Alloy
(см. docs/ARCHITECTURE.md, секция «Логи»).
"""

from __future__ import annotations

import re
import xml.sax.saxutils as saxutils

from ..base import Metric, Session, run_collector

PREFIX = "ilo_ribcl"

RIBCL_TEMPLATE = """<?xml version="1.0" encoding="ISO-8859-1"?>
<RIBCL VERSION="2.0">
<LOGIN USER_LOGIN="{user}" PASSWORD="{password}">
<RIB_INFO MODE="read">
{command_block}
</RIB_INFO>
</LOGIN>
</RIBCL>
"""


def _build_script(command: str) -> str:
    """Оборачивает 'GET IML'/'GET IEL' в корректный RIBCL-запрос."""
    cmd = command.strip().upper()
    if cmd in ("GET IML", "IML"):
        block = '<GET_IML/>\n<GET_IML_LDSTATE/>'
    elif cmd in ("GET IEL", "IEL"):
        block = '<GET_IEL/>\n<GET_IEL_LDSTATE/>'
    elif cmd.startswith("<"):
        block = command  # пользователь прислал готовый XML-фрагмент
    else:
        block = f"<{cmd.lower().replace(' ', '_')}/>"
    return RIBCL_TEMPLATE.format(user="{user}", password="{password}",
                                 command_block=block)


def _count_events(text: str) -> int:
    """Эвристика: строки вида '00012 | 06/18/26 | 12:00:00 | Имя события |'."""
    return len(re.findall(r"^\s*\d{4,6}\s*\|", text, re.MULTILINE))


def _collect(target) -> list[Metric]:
    labels = {"target": target.name, **target.labels}
    metrics: list[Metric] = []
    command = (target.ribcl_command or "GET IML").strip()
    kind = "iel" if "IEL" in command.upper() else "iml"

    session = Session(target.base_url, "", "", timeout=target.timeout,
                      verify=target.verify_tls,
                      host_header=getattr(target, "host_header", ""),
                      allow_sni_mismatch=getattr(target, "allow_sni_mismatch", False))

    body = None
    # Путь A: iLO4/iLO5 — RIBCL через Redfish (рекомендуемый, начиная с Gen9)
    try:
        resp = session.session.post(
            f"{session.base_url}/redfish/v1/SessionService/Sessions",
            json={"UserName": target.username, "Password": target.password},
            timeout=session.timeout,
        )
        token = resp.headers.get("X-Auth-Token", "")
        if resp.status_code in (200, 201) and token:
            script = _build_script(command).format(
                user=saxutils.escape(target.username),
                password=saxutils.escape(target.password))
            r2 = session.session.get(
                f"{session.base_url}/redfish/v1/Managers/iLO/RIBCL",
                headers={"X-authentication-token": token,
                         "Content-Type": "text/ribcl"},
                data=script.encode(), timeout=session.timeout)
            if r2.status_code == 200:
                body = r2.text
            # завершаем сессию, чтобы не исчерпать лимит сессий iLO
            sid = resp.headers.get("Location", "")
            if sid:
                session.session.delete(sid, headers={"X-Auth-Token": token},
                                       timeout=session.timeout)
    except Exception:
        body = None

    # Путь B: классический RIBCL поверх HTTPS POST /ribcl (iLO2..iLO5)
    if body is None:
        script = _build_script(command).format(
            user=saxutils.escape(target.username),
            password=saxutils.escape(target.password))
        resp = session.session.post(
            f"{session.base_url}/ribcl",
            data=script.encode(),
            headers={"Content-Type": "text/ribcl"},
            timeout=session.timeout)
        if resp.ok:
            body = resp.text

    if body is None:
        metrics.append(Metric(f"{PREFIX}_up", 0.0, labels,
                              help="1 — дамп RIBCL получен"))
        return metrics

    events = _count_events(body)
    metrics.append(Metric(f"{PREFIX}_up", 1.0, labels, help="1 — дамп RIBCL получен"))
    metrics.append(Metric(f"{PREFIX}_dump_bytes", float(len(body)),
                          {**labels, "log": kind}, help="Размер дампа RIBCL, байт"))
    metrics.append(Metric(f"{PREFIX}_{kind}_entries", float(events), labels,
                          help=f"Оценок событий в дампе {kind.upper()}"))
    return metrics


def collect(target) -> list[Metric]:
    return run_collector(_collect, target, PREFIX).metrics

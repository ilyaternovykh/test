# -*- coding: utf-8 -*-
"""Redfish-коллектор для iLO (HP/Dell, универсальный парсер).

Стратегия обнаружения ресурсов (Discovery):
  * GET /redfish/v1/                      -> версии, id сессии
  * GET /redfish/v1/Systems/{id}          -> процессоры, память, состояние, power
  * GET /redfish/v1/Chassis/{id}          -> температура, питание, вентиляторы
  * GET /redfish/v1/Managers/{id}         -> версия прошивки iLO
  * GET /redfish/v1/UpdateService/FirmwareInventory -> список FirmwareImage
  * GET /redfish/v1/Systems/{id}/LogServices -> записи журналов (IML/IEL)

Парсер написан «широко»: читает как стандартные поля Redfish, так и
специфичные для iLO (Health, Status.State, PowerState и т.п.), поэтому один
и тот же код работает против iLO на Gen8 (ограниченный Redfish), Gen9/Gen10
(iLO4/iLO5) и даже Dell iDRAC. Всё, чего нет у конкретного устройства,
просто не создаёт метрик.

Метрики (префикс ilo_redfish_*):
    ilo_redfish_up{target}
    ilo_redfish_power_state{target,state}                 1 активное состояние
    ilo_redfish_thermal_temperature_celsius{target,name,id}
    ilo_redfish_thermal_fan_rpm{target,name,id}
    ilo_redfish_cpu_cores{target,name,id}
    ilo_redfish_cpu_usage_percent{target,name,id}
    ilo_redfish_memory_total_bytes{target}
    ilo_redfish_dimm_count{target}
    ilo_redfish_psu_present{target,name,id}
    ilo_redfish_psu_input_watts{target,name,id}
    ilo_redfish_psu_output_watts{target,name,id}
    ilo_redfish_status_ok{target,resource,id}             1 если Health==OK
    ilo_redfish_ilo_firmware_version_info{target,fw_version}
    ilo_redfish_firmware_version_info{target,name,id,fw_version}
    ilo_redfish_log_entry{target,log,type,severity,created}   (задел IML/IEL)
    ilo_redfish_event_count{target,severity}
"""

from __future__ import annotations

from typing import Optional

from ..base import (Metric, Session, first_number, sanitize_name, status_to_bool,
                    run_collector)

PREFIX = "ilo_redfish"


class RedfishClient:
    """Ленивый клиент Redfish поверх Session."""

    def __init__(self, session: Session):
        self.session = session
        self._cache: dict[str, Optional[dict]] = {}

    def get_json(self, path: str) -> Optional[dict]:
        if path in self._cache:
            return self._cache[path]
        try:
            resp = self.session.get(path)
            if resp.status_code != 200:
                self._cache[path] = None
                return None
            data = resp.json()
        except Exception:
            self._cache[path] = None
            return None
        self._cache[path] = data
        return data

    def collection_members(self, path: str) -> list[tuple[str, Optional[dict]]]:
        """Возвращает [(href, body|None)] для Redfish-коллекции."""
        coll = self.get_json(path)
        if not coll:
            return []
        members = coll.get("Members") or []
        out: list[tuple[str, Optional[dict]]] = []
        for m in members:
            href = m.get("@odata.id") if isinstance(m, dict) else None
            if not href:
                continue
            # часть ответов уже содержит тело (Actions/наследие некоторых прошивок)
            body = m if ("Id" in m or "Name" in m) else self.get_json(href)
            out.append((href, body))
        return out

    def service_root(self) -> Optional[dict]:
        return self.get_json("/redfish/v1/")

    def _first_collection_href(self, root_path: str) -> Optional[str]:
        coll = self.get_json(root_path)
        if not coll:
            return None
        members = coll.get("Members") or []
        if members and isinstance(members[0], dict):
            return members[0].get("@odata.id")
        return None

    def system_href(self) -> Optional[str]:
        root = self.service_root() or {}
        systems = (root.get("Systems") or {}).get("@odata.id")
        return self._first_collection_href(systems) if systems else None

    def chassis_href(self) -> Optional[str]:
        root = self.service_root() or {}
        chassis = (root.get("Chassis") or {}).get("@odata.id")
        return self._first_collection_href(chassis) if chassis else None

    def manager_href(self) -> Optional[str]:
        root = self.service_root() or {}
        managers = (root.get("Managers") or {}).get("@odata.id")
        return self._first_collection_href(managers) if managers else None


def _status_labels(resource: dict, base: dict, rid: str, kind: str) -> list[Metric]:
    """Метрика health по полю Status{State,Health}. Возвращает [] если поля нет."""
    status = resource.get("Status") or {}
    health = str(status.get("Health", "")).strip()
    state = str(status.get("State", "")).strip()
    if not health and not state:
        return []
    ok = status_to_bool(health) if health else None
    metrics = []
    labels = {**base, "resource": kind, "id": rid,
              "health": sanitize_name(health or "unknown"),
              "state": sanitize_name(state or "unknown")}
    metrics.append(Metric(f"{PREFIX}_status_ok",
                          1.0 if (ok == 1.0 and state.lower() in ("", "enabled")) else 0.0,
                          labels, help="1 — ресурс в состоянии OK/Enabled"))
    return metrics


def _collect_system(client: RedfishClient, base: dict) -> list[Metric]:
    metrics: list[Metric] = []
    sys_href = client.system_href()
    if not sys_href:
        return metrics
    system = client.get_json(sys_href) or {}

    # --- PowerState ---
    power_state = str(system.get("PowerState", "")).strip()
    if power_state:
        known = {"On": 1, "Off": 0, "Paused": 0}
        val = known.get(power_state, 0)
        metrics.append(Metric(f"{PREFIX}_power_state", float(val),
                              {**base, "state": sanitize_name(power_state)},
                              help="Состояние питания сервера (1=On)"))

    # --- общая доступность Systems + health ---
    metrics.extend(_status_labels(system, base, sanitize_name(str(system.get("Id", "system"))), "System"))

    # --- процессоры ---
    proc_coll = (system.get("ProcessorSummary") or {})
    total_cores = first_number(proc_coll, "TotalCores")
    if total_cores is not None:
        metrics.append(Metric(f"{PREFIX}_cpu_total_cores", total_cores, base,
                              help="Суммарное число ядер CPU (ProcessorSummary)"))
    cpu_count = first_number(proc_coll, "Count")
    if cpu_count is not None:
        metrics.append(Metric(f"{PREFIX}_cpu_count", cpu_count, base,
                              help="Число физических CPU (ProcessorSummary.Count)"))
    logical = first_number(proc_coll, "LogicalProcessorCount")
    if logical is not None:
        metrics.append(Metric(f"{PREFIX}_cpu_logical_processors", logical, base,
                              help="Число логических процессоров"))
    proc_health = str(proc_coll.get("Status", ""))
    if proc_health:
        ok = status_to_bool(proc_health)
        metrics.append(Metric(f"{PREFIX}_processor_summary_ok",
                              1.0 if ok == 1.0 else 0.0, base,
                              help="ProcessorSummary.Status OK"))

    procs_href = (system.get("Processors") or {}).get("@odata.id")
    if procs_href:
        for href, proc in client.collection_members(procs_href):
            if not proc:
                continue
            pid = sanitize_name(str(proc.get("Id", href.rsplit("/", 1)[-1])))
            pname = sanitize_name(str(proc.get("Name", pid)))
            labels = {**base, "id": pid, "name": pname}
            cores = first_number(proc, "TotalCores", "CoreCount")
            threads = first_number(proc, "ThreadCount", "TotalThreads")
            if cores is not None:
                metrics.append(Metric(f"{PREFIX}_cpu_cores", cores, labels,
                                      help="Ядер у процессора"))
            if threads is not None:
                metrics.append(Metric(f"{PREFIX}_cpu_threads", threads, labels,
                                      help="Потоков у процессора"))
            usage = (proc.get("MemoryMetrics") or {}).get("AverageBandwidthPercent") \
                or (proc.get("ProcessorMetrics") or {}).get("AverageFrequencyMHz")
            if isinstance(usage, (int, float)):
                metrics.append(Metric(f"{PREFIX}_cpu_avg_freq_mhz", float(usage), labels,
                                      help="Средняя частота CPU, МГц"))
            metrics.extend(_status_labels(proc, labels, pid, "Processor"))

    # --- память ---
    mem_summary = system.get("MemorySummary") or {}
    total_mem = first_number(mem_summary, "TotalSystemMemoryGiB")
    if total_mem is not None:
        metrics.append(Metric(f"{PREFIX}_memory_total_bytes", total_mem * 1024 ** 3, base,
                              help="Объём установленной памяти, байт"))
    mirrored = first_number(mem_summary, "TotalMirroredMemoryMiB")
    if mirrored is not None:
        metrics.append(Metric(f"{PREFIX}_memory_mirrored_bytes", mirrored * 1024 ** 2, base,
                              help="Зеркалированная память, байт"))

    mem_coll_href = (system.get("Memory") or {}).get("@odata.id")
    dimm_count = 0
    if mem_coll_href:
        for href, mem in client.collection_members(mem_coll_href):
            dimm_count += 1
            if not mem:
                continue
            mid = sanitize_name(str(mem.get("Id", href.rsplit("/", 1)[-1])))
            mname = sanitize_name(str(mem.get("Name", mid)))
            labels = {**base, "id": mid, "name": mname}
            size = first_number(mem, "CapacityMiB")
            if size is not None:
                metrics.append(Metric(f"{PREFIX}_dimm_size_bytes", size * 1024 ** 2, labels,
                                      help="Размер DIMM, байт"))
            speed = first_number(mem, "OperatingSpeedMhz")
            if speed is not None:
                metrics.append(Metric(f"{PREFIX}_dimm_speed_mhz", speed, labels,
                                      help="Частота DIMM, МГц"))
            present = status_to_bool(mem.get("MemoryDeviceType") and "Installed" or None)
            if "Populated" in mem:
                metrics.append(Metric(f"{PREFIX}_dimm_present",
                                      1.0 if mem.get("Populated") else 0.0, labels,
                                      help="DIMM установлен"))
            metrics.extend(_status_labels(mem, labels, mid, "Memory"))
    if mem_coll_href:
        metrics.append(Metric(f"{PREFIX}_dimm_count", float(dimm_count), base,
                              help="Число распознанных DIMM"))

    # --- Boot / BIOS version ---
    bios = str(system.get("BiosVersion", "")).strip()
    if bios:
        metrics.append(Metric(f"{PREFIX}_bios_version_info", 1,
                              {**base, "bios_version": sanitize_name(bios)}, "untyped",
                              help="Версия BIOS"))
    return metrics


def _collect_chassis(client: RedfishClient, base: dict) -> list[Metric]:
    metrics: list[Metric] = []
    ch_href = client.chassis_href()
    if not ch_href:
        return metrics
    chassis = client.get_json(ch_href) or {}
    cid = sanitize_name(str(chassis.get("Id", "chassis")))
    metrics.extend(_status_labels(chassis, base, cid, "Chassis"))

    # --- Thermal: температуры + вентиляторы ---
    thermal_href = (chassis.get("Thermal") or {}).get("@odata.id")
    if thermal_href:
        thermal = client.get_json(thermal_href) or {}
        for idx, temp in enumerate(thermal.get("Temperatures") or []):
            name = sanitize_name(str(temp.get("Name") or temp.get("SensorName") or f"Temp{idx}"))
            val = first_number(temp, "ReadingCelsius")
            if val is None:
                continue
            labels = {**base, "id": sanitize_name(str(temp.get("MemberId", idx))), "name": name}
            metrics.append(Metric(f"{PREFIX}_thermal_temperature_celsius", val, labels,
                                  help="Температура датчика, °C"))
            upper_crit = first_number(temp, "UpperThresholdCritical")
            if upper_crit is not None:
                metrics.append(Metric(f"{PREFIX}_thermal_temp_upper_critical_celsius",
                                      upper_crit, labels, help="Критический верхний порог, °C"))
            ts = temp.get("Status") or {}
            if ts:
                metrics.extend(_status_labels(temp, labels, name, "Temperature"))
        for idx, fan in enumerate(thermal.get("Fans") or []):
            name = sanitize_name(str(fan.get("Name") or fan.get("FanName") or f"Fan{idx}"))
            rpm = first_number(fan, "Reading", "ReadingRPM")
            if rpm is None:
                continue
            labels = {**base, "id": sanitize_name(str(fan.get("MemberId", idx))), "name": name}
            metrics.append(Metric(f"{PREFIX}_thermal_fan_rpm", rpm, labels,
                                  help="Обороты вентилятора, RPM"))
            min_rpm = first_number(fan, "LowerThresholdCritical")
            if min_rpm is not None:
                metrics.append(Metric(f"{PREFIX}_thermal_fan_lower_critical_rpm", min_rpm, labels,
                                      help="Нижний критический порог вентилятора, RPM"))
    # --- Power: блоки питания ---
    power_href = (chassis.get("Power") or {}).get("@odata.id")
    if power_href:
        power = client.get_json(power_href) or {}
        for idx, psu in enumerate(power.get("PowerSupplies") or []):
            name = sanitize_name(str(psu.get("Name") or psu.get("MemberId") or f"PSU{idx}"))
            labels = {**base, "id": sanitize_name(str(psu.get("MemberId", idx))), "name": name}
            present = status_to_bool(str(psu.get("Status", {}).get("State", "")) or None)
            metrics.append(Metric(f"{PREFIX}_psu_present", 1.0 if present else 0.0, labels,
                                  help="Блок питания присутствует/активен"))
            for field, metric in (("LineInputVoltage", f"{PREFIX}_psu_line_input_volts"),
                                  ("PowerCapacityWatts", f"{PREFIX}_psu_capacity_watts"),
                                  ("LastPowerOutputWatts", f"{PREFIX}_psu_output_watts")):
                val = first_number(psu, field)
                if val is not None:
                    metrics.append(Metric(metric, val, labels, help=f"PSU {field}"))
            metrics.extend(_status_labels(psu, labels, name, "PowerSupply"))
        redundancy = (power.get("Redundancy") or [])
        if redundancy:
            r0 = redundancy[0] or {}
            mode = sanitize_name(str(r0.get("Mode", "")))
            metrics.append(Metric(f"{PREFIX}_power_redundancy_ok",
                                  1.0 if str(r0.get("Status", {}).get("Health", "")).lower() == "ok" else 0.0,
                                  {**base, "mode": mode}, help="Резервирование питания OK"))
    return metrics


def _collect_manager(client: RedfishClient, base: dict) -> list[Metric]:
    metrics: list[Metric] = []
    mgr_href = client.manager_href()
    if not mgr_href:
        return metrics
    mgr = client.get_json(mgr_href) or {}
    mid = sanitize_name(str(mgr.get("Id", "manager")))
    fw = str(mgr.get("FirmwareVersion", "")).strip()
    if fw:
        metrics.append(Metric(f"{PREFIX}_ilo_firmware_version_info", 1,
                              {**base, "fw_version": sanitize_name(fw)}, "untyped",
                              help="Версия прошивки iLO"))
    model = str(mgr.get("Model", "")).strip()
    if model:
        metrics.append(Metric(f"{PREFIX}_manager_model_info", 1,
                              {**base, "model": sanitize_name(model)}, "untyped",
                              help="Модель менеджера (iLO)"))
    metrics.extend(_status_labels(mgr, base, mid, "Manager"))
    return metrics


def _collect_firmware_inventory(client: RedfishClient, base: dict) -> list[Metric]:
    metrics: list[Metric] = []
    inv_href = ((client.service_root() or {}).get("UpdateService") or {}).get("@odata.id")
    if not inv_href:
        return metrics
    update_service = client.get_json(inv_href) or {}
    fw_coll = (update_service.get("FirmwareInventory") or {}).get("@odata.id")
    if not fw_coll:
        return metrics
    for href, item in client.collection_members(fw_coll):
        if not item:
            continue
        iid = sanitize_name(str(item.get("Id", href.rsplit("/", 1)[-1])))
        name = sanitize_name(str(item.get("Name", iid)))
        ver = str(item.get("Version", "")).strip()
        labels = {**base, "id": iid, "name": name}
        if ver:
            metrics.append(Metric(f"{PREFIX}_firmware_version_info", 1,
                                  {**labels, "fw_version": sanitize_name(ver)}, "untyped",
                                  help="Компоненты FirmwareInventory"))
        metrics.extend(_status_labels(item, labels, iid, "Firmware"))
    return metrics


def _collect_logs(client: RedfishClient, base: dict) -> list[Metric]:
    """Задел на IML/IEL: читаем LogServices и последние записи.

    Для iLO4 это /redfish/v1/Systems/{id}/LogServices/IML и .../Logs,
    а также Managers/{id}/LogServices/SecurityOverlay и HPRESTILogService.
    Записи публикуются как событийная метрика ilo_redfish_log_entry;
    полный текст IML/IEL сохраняется отдельным пайплайном (см. ribcl.py).
    """
    metrics: list[Metric] = []
    sev_counter: dict[str, int] = {}
    targets = []
    sys_href = client.system_href()
    if sys_href:
        targets.append((f"{sys_href}/LogServices", "system"))
    mgr_href = client.manager_href()
    if mgr_href:
        targets.append((f"{mgr_href}/LogServices", "manager"))

    for ls_href, origin in targets:
        ls_coll = client.get_json(ls_href)
        if not ls_coll:
            continue
        for svc_href, svc in client.collection_members(ls_href):
            if not svc:
                continue
            svc_id = sanitize_name(str(svc.get("Id", svc_href.rsplit("/", 1)[-1])))
            logs_href = (svc.get("Entries") or {}).get("@odata.id")
            if not logs_href:
                continue
            entries_coll = client.get_json(logs_href) or {}
            count = 0
            for entry in (entries_coll.get("Members") or [])[-50:]:
                if not isinstance(entry, dict):
                    continue
                ehref = entry.get("@odata.id")
                body = entry if "EntryType" in entry else (client.get_json(ehref) if ehref else None)
                if not body:
                    continue
                sev = sanitize_name(str(body.get("Severity", "Unknown")).upper())
                created = sanitize_name(str(body.get("Created", "")))
                etype = sanitize_name(str(body.get("EntryType", "Event")))
                labels = {**base, "log": f"{origin}:{svc_id}", "type": etype,
                          "severity": sev, "created": created}
                metrics.append(Metric(f"{PREFIX}_log_entry", 1, labels, "untyped",
                                      help="Запись журнала Redfish (IML/IEL/Event)"))
                sev_counter[sev] = sev_counter.get(sev, 0) + 1
                count += 1
            metrics.append(Metric(f"{PREFIX}_log_entries_total", float(count),
                                  {**base, "log": f"{origin}:{svc_id}"}, "counter",
                                  help="Число записей в логе (последняя выборка)"))
    for sev, cnt in sev_counter.items():
        metrics.append(Metric(f"{PREFIX}_event_count", float(cnt),
                              {**base, "severity": sev}, help="Событий по severity за выборку"))
    return metrics


def _collect(target) -> list[Metric]:
    base = {"target": target.name, **target.labels}
    metrics: list[Metric] = []

    session = Session(target.base_url, target.username, target.password,
                      timeout=target.timeout, verify=target.verify_tls,
                      host_header=getattr(target, "host_header", ""),
                      allow_sni_mismatch=getattr(target, "allow_sni_mismatch", False))
    client = RedfishClient(session)

    root = client.service_root()
    if root is None:
        metrics.append(Metric(f"{PREFIX}_up", 0.0, base, help="1 — Redfish API отвечает"))
        return metrics
    metrics.append(Metric(f"{PREFIX}_up", 1.0, base, help="1 — Redfish API отвечает"))

    rf_version = str((root.get("RedfishVersion") or "")).strip()
    if rf_version:
        metrics.append(Metric(f"{PREFIX}_service_root_info", 1,
                              {**base, "redfish_version": sanitize_name(rf_version)},
                              "untyped", help="Версия протокола Redfish"))

    metrics.extend(_collect_system(client, base))
    metrics.extend(_collect_chassis(client, base))
    metrics.extend(_collect_manager(client, base))
    metrics.extend(_collect_firmware_inventory(client, base))
    metrics.extend(_collect_logs(client, base))
    return metrics


def collect(target) -> list[Metric]:
    return run_collector(_collect, target, PREFIX).metrics

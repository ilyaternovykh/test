#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фейковый iLO на Redfish для тестов без «железа».

Имитирует DL380 Gen8 (Redfish 1.0, ограниченно) и DL360 Gen10 / iLO5
(полный Redfish): Systems/Processors/Memory, Chassis/Thermal/Power,
Managers, UpdateService/FirmwareInventory, LogServices (IML-like записи),
а также POST /ribcl (дамп IML) и SessionService (для RIBCL-through-Redfish).

POST /ribcl дополнительно эмулирует RIBCL-протокол библиотеки python-hpilo
(которую использует hpilo-exporter): распознаёт <LOGIN .../> c проверкой
учётки, GET_EMBEDDED_HEALTH_DATA (health_at_a_glance/temperature/fans/
power_supplies/processors/memory/storage/nic_information),
GET_PRODUCT_NAME / GET_SERVER_NAME / GET_HOST_POWER_STATUS / GET_FW_VERSION.
Ответ — «сырой» HTTP (заголовки в теле), как это делает настоящая прошивка:
библиотека сама парсит "HTTP/1.1 200\r\n...\r\n\r\n<?xml...".

Запуск:   python3 tests/mock_ilo.py --port 8443 [--plain]
Тесты:    ILO_URL=https://localhost:8443 python3 -m exporter --once
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import ssl
import subprocess
import tempfile
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _profile() -> dict:
    """Уникальный «отпечаток» сервера по имени хоста запроса.

    Один mock-контейнер эмулирует весь парк: exporter шлёт HTTP заголовок
    Host: mock-ilo-N (см. targets.yaml), поэтому каждый target получает
    свои значения датчиков — как на реальном железе.
    """
    host = (getattr(getattr(_handler_local, "srv", None), "host", "") or "mock-dl380g8-01")
    host = host.partition(":")[0]
    # стабильный seed между перезапусками mock (hash() с PYTHONHASHSEED случайна)
    seed = int(zlib.crc32(host.encode())) % 1000
    m = re.search(r"(\d+)$", host)
    idx = int(m.group(1)) if m else 1
    rng = random.Random(seed)
    model = ("ProLiant DL360p Gen8" if idx % 2 == 0 else "ProLiant DL380p Gen8")
    return {"idx": idx, "seed": seed, "rng": rng, "host": host, "model": model}


class _HandlerLocal:
    srv = None


_handler_local = _HandlerLocal()


def thermal():
    p = _profile()
    base_temps = [40 + p["idx"], 43 + p["idx"], 26 + p["idx"] % 5,
                  20 + p["idx"] % 4, 32 + p["idx"] % 6]
    temps = list(zip(("CPU 1", "CPU 2", "Board", "Inlet", "Outlet"), base_temps))
    fans = [(f"Fan {i}", 4200 + (p["seed"] + i * 137) % 1900) for i in range(1, 7)]
    return {
        "@odata.id": "/redfish/v1/Chassis/1/Thermal",
        "Id": "Thermal", "Name": "Thermal",
        "Temperatures": [
            {"MemberId": str(i), "Name": n, "ReadingCelsius": t + p["rng"].randint(-2, 2),
             "UpperThresholdCritical": 95, "Status": {"State": "Enabled", "Health": "OK"}}
            for i, (n, t) in enumerate(temps)],
        "Fans": [
            {"MemberId": str(i), "Name": n, "Reading": rpm,
             "LowerThresholdCritical": 400,
             "Status": {"State": "Enabled", "Health": "OK"}}
            for i, (n, rpm) in enumerate(fans)],
        "Status": {"State": "Enabled", "Health": "OK"},
    }


def power():
    p = _profile()
    load = 150 + (p["seed"] % 9) * 10
    return {
        "@odata.id": "/redfish/v1/Chassis/1/Power",
        "Id": "Power", "Name": "Power",
        "PowerSupplies": [
            {"MemberId": "0", "Name": "PSU 1", "LineInputVoltage": 220 + p["idx"],
             "PowerCapacityWatts": 800,
             "LastPowerOutputWatts": load + p["rng"].randint(0, 40),
             "Status": {"State": "Enabled", "Health": "OK"}},
            {"MemberId": "1", "Name": "PSU 2", "LineInputVoltage": 224 + p["idx"],
             "PowerCapacityWatts": 800,
             "LastPowerOutputWatts": load - 10 + p["rng"].randint(0, 40),
             "Status": {"State": "Enabled", "Health": "OK"}},
        ],
        "Redundancy": [{"Mode": "Failover", "Status": {"State": "Enabled", "Health": "OK"}}],
        "Status": {"State": "Enabled", "Health": "OK"},
    }


LOG_ENTRIES = [
    {"Severity": "OK", "Message": "Temperature sensor CPU 1 normalized."},
    {"Severity": "Warning", "Message": "Memory DIMM 14 POST failure corrected."},
    {"Severity": "OK", "Message": "Server power on."},
    {"Severity": "Critical", "Message": "IML: fan 3 speed below threshold."},
    {"Severity": "OK", "Message": "iLO firmware updated to 2.82."},
]


def _ribcl_embedded_health(p: dict) -> str:
    """GET_EMBEDDED_HEALTH_DATA в формате, который разбирает python-hpilo
    (атрибуты вместо дочерних тегов там, где библиотека читает .get('VALUE'))."""
    rng = p["rng"]
    idx = p["idx"]
    temps = [("01-Inlet Ambient", 20 + idx % 4), ("02-CPU 1", 40 + idx),
             ("03-CPU 2", 43 + idx), ("04-P/S 1", 32 + idx % 6),
             ("05-Board", 26 + idx % 5)]
    fans = [(f"Fan {i}", 4200 + (p["seed"] + i * 137) % 1900, "OK") for i in range(1, 7)]
    parts = ['<?xml version="1.0"?>', '<RIBCL VERSION="2.23">',
             # RESPONSE STATUS=0x0 ("No error") — как настоящая прошивка;
             # hpilo игнорирует его и берёт следующий message (payload).
             '<RESPONSE STATUS="0x00000000" MESSAGE="No error" VER_ERR="0"/>',
             # ВАЖНО: python-hpilo (_process_info_tag) ищет тег
             # GET_EMBEDDED_HEALTH_DATA ЧЕРЕЗ message.find(...), т.е. только как
             # ПРЯМОГО дочернего элемента корня <RIBCL>. Обёртка SERVER_INFO или
             # GET_EMBEDDED_HEALTH выше уровнем ломает поиск ("Expected tag ...
             # not found"). Значит корневой payload-тег должен быть именно
             # GET_EMBEDDED_HEALTH_DATA, а его дети — HEALTH_AT_A_GLANCE и др.
             '<GET_EMBEDDED_HEALTH_DATA>']
    # health_at_a_glance
    hag = {"BATTERY": "OK", "BIOS_HARDWARE": "OK", "MEMORY": "OK",
           "PROCESSOR": "OK", "VRM": "OK", "DRIVE": "OK",
           "FANS": "OK", "POWER_SUPPLIES": "OK", "TEMPERATURE": "OK",
           "STORAGE": "OK" if idx % 7 else "Degraded",
           "NETWORK": "Link Down" if idx % 5 == 0 else "OK"}
    # ВАЖНО: формат XML для RIBCL-ответа скопирован с реальных дампов iLO4 —
    # python-hpilo разбирает дочерние теги по АТРИБУТУ VALUE (или текстовому
    # узлу), а не по произвольным атрибутам вроде STATUS/SPEED/LABEL.
    parts.append("<HEALTH_AT_A_GLANCE>")
    for k, v in hag.items():
        parts.append(f'<{k} VALUE="{v}"'
                     + (' REDUNDANCY="Redundant"' if k in ("FANS", "POWER_SUPPLIES") else "")
                     + "/>")
    parts.append("</HEALTH_AT_A_GLANCE>")
    # temperature
    parts.append("<TEMPERATURE>")
    for name, val in temps:
        parts.append(f'<CURRENTICREADING><LABEL>{name}</LABEL>'
                     f'<VALUE>{val}</VALUE></CURRENTICREADING>')
    parts.append("</TEMPERATURE>")
    # fans
    parts.append("<FANS>")
    for name, speed, st in fans:
        pct = speed // 100
        parts.append(f'<FAN><LABEL>{name}</LABEL><PRESENT VALUE="Yes" />'
                     f'<SPEED VALUE="{pct}" UNIT="%"/><STATUS VALUE="{st}"/></FAN>')
    parts.append("</FANS>")
    # power supplies
    parts.append("<POWER_SUPPLIES>")
    for i in (1, 2):
        st, pr = ("OK", "Yes") if idx != i else ("Absent", "No")
        parts.append(f'<POWERSUPPLY><LABEL>Power Supply {i}</LABEL>'
                     f'<MODEL>NDAA-RTCW</MODEL>'
                     f'<CAPACITY VALUE="560" UNIT="Watt"/>'
                     f'<SERIALNUMBER>SN{p["seed"]}{i}</SERIALNUMBER>'
                     f'<FIRMWARE_VERSION>2012-06-08,1.2</FIRMWARE_VERSION>'
                     f'<PRESENT VALUE="{pr}" /><SPARE VALUE="N"/>'
                     f'<STATUS VALUE="{st}"/></POWERSUPPLY>')
    parts.append("</POWER_SUPPLIES>")
    parts.append('<POWER_SUPPLY_SUMMARY><PRESENT_POWER_READING VALUE="%d" UNIT="Watt"/>'
                 '</POWER_SUPPLY_SUMMARY>' % (150 + rng.randint(0, 120)))
    # processors
    parts.append("<PROCESSORS>")
    for c in (1, 2):
        parts.append(f'<CPU><LABEL>Processor {c}</LABEL><SOCKET>CPU{c}</SOCKET>'
                     f'<CORES>8</CORES><SPEED>2400 MHz</SPEED>'
                     f'<NAME>Intel Xeon E5-2620 v2</NAME>'
                     f'<STATUS VALUE="OK"/></CPU>')
    parts.append("</PROCESSORS>")
    # memory
    parts.append("<MEMORY><MEMORY_DETAILS_SUMMARY>")
    for c in (1, 2):
        parts.append(f'<CPU{c}><TOTAL_MEMORY_SIZE VALUE="64 GiB"/>'
                     f'<OPERATING_FREQUENCY VALUE="1333 MHz"/>'
                     f'<OPERATING_VOLTAGE VALUE="1.35 V"/>'
                     f'<MEMORY_STATUS VALUE="OK"/></CPU{c}>')
    parts.append("</MEMORY_DETAILS_SUMMARY></MEMORY>")
    # storage (Smart Array P220i)
    parts.append('<STORAGE><CONTROLLER>'
                 '<LABEL>Controller on System Board, Smart Array P220i Controller</LABEL>'
                 '<MODEL>P220i</MODEL><SERIALNUMBER>PTUSA0BRH2CBZT</SERIALNUMBER>'
                 '<CACHE_MODULE_STATUS VALUE="OK"/><CONTROLLER_STATUS VALUE="OK">OK</CONTROLLER_STATUS>'
                 '<DRIVE_ENCLOSURES><ENCLOSURE><STATUS VALUE="OK"/></ENCLOSURE></DRIVE_ENCLOSURES>'
                 '<LOGICAL_DRIVES><LOGICAL_DRIVE>'
                 '<CAPACITY VALUE="279 GiB"/><FAULT_TOLERANCE VALUE="RAID 1/RAID 1+0"/>'
                 '<LOGICAL_DRIVE_STATUS VALUE="OK"/>'
                 '<PHYSICAL_DRIVES>'
                 '<PHYSICAL_DRIVE><MODEL>EG0300FCSPH</MODEL>'
                 '<CAPACITY VALUE="279 GiB"/><LOCATION>Port 1I Box 1 Bay 1</LOCATION>'
                 '<STATUS VALUE="OK"/></PHYSICAL_DRIVE>'
                 '<PHYSICAL_DRIVE><MODEL>EG0300FCSPH</MODEL>'
                 '<CAPACITY VALUE="279 GiB"/><LOCATION>Port 1I Box 1 Bay 2</LOCATION>'
                 '<STATUS VALUE="OK"/></PHYSICAL_DRIVE>'
                 '</PHYSICAL_DRIVES>'
                 '</LOGICAL_DRIVE></LOGICAL_DRIVES></CONTROLLER></STORAGE>')
    # nic information (iLO4 путь)
    parts.append("<NIC_INFORMATION>")
    parts.append(f'<NIC><LABEL>Nic Port 1</LABEL><STATUS VALUE="Link Up"/>'
                 f'<IP_ADDRESS>10.0.0.{idx}</IP_ADDRESS>'
                 '<SPEED VALUE="1000" UNIT="Mbps"/><LINK>Full Duplex</LINK></NIC>')
    parts.append('<NIC><LABEL>Nic Port 2</LABEL><STATUS VALUE="Link Down"/>'
                 '<IP_ADDRESS>0.0.0.0</IP_ADDRESS>'
                 '<SPEED VALUE="Unknown" UNIT="Mbps"/><LINK>Unknown</LINK></NIC>')
    parts.append("</NIC_INFORMATION>")
    parts.append("</GET_EMBEDDED_HEALTH_DATA>")
    parts.append('<RIBCL_INFO><RIBCL_MESSAGE level="low">Logged in successfully</RIBCL_MESSAGE></RIBCL_INFO>')
    parts.append("</RIBCL>")
    return "\r\n".join(parts)


def _ribcl_message(status_hex: str, message: str) -> str:
    """Одиночное RIBCL-сообщение с RESPONSE (как настоящая прошивка).

    level='low' + 'logged in successfully' игнорируется библиотекой hpilo
    (фильтр в _parse_message), поэтому LOGIN-успех не «съедает» payload."""
    return ('<?xml version="1.0"?>\r\n<RIBCL VERSION="2.23">\r\n'
            f'<RESPONSE STATUS="{status_hex}" MESSAGE="{message}"'
            ' VER_ERR="0"/><RIBCL_INFO>\r\n'
            '<RIBCL_MESSAGE level="low">Mock iLO: request processed</RIBCL_MESSAGE>'
            '</RIBCL_INFO>\r\n</RIBCL>')


def _ribcl_response(raw: bytes, login_ok: bool = True) -> tuple[int, str]:
    """Разбор RIBCL-запроса от python-hpilo (hpilo-exporter) и нашего
    ribcl-коллектора -> (http_code, xml_body).

    Важно (проверено по исходникам python-hpilo 4.x):
      * библиотека шлёт `POST /ribcl HTTP/1.1` СЫРЫМИ байтами поверх TLS;
      * ждёт ответ, начинающийся ровно с "HTTP/1.1 200" (иначе — ошибка);
      * пустой запрос <RIBCL/> (протокольная детекция) должен вернуть
        RESPONSE STATUS=0x400 'syntax error' — иначе hpilo переключится
        на RAW-протокол и дальше всё сломается;
      * тело ответа может содержать НЕСКОЛЬКО XML-сообщений подряд
        (каждое со своим <?xml ...?>) — библиотека режет их по '<?xml'.
    login_ok=False эмулирует неверную учётку -> IloLoginFailed (0x005f)."""
    text = raw.decode("utf-8", "replace")
    p = _profile()

    # --- протокольная детекция (python-hpilo._detect_protocol) ---
    # Библиотека определяет, HTTP-это-iLO или RAW-порт, по наличию заголовка
    # "HTTP/1.1" в начале ОТВЕТА. Настоящий iLO4 всегда отвечает как HTTP-сервер
    # (в т.ч. синтаксической ошибкой на мусор), поэтому эмулируем то же:
    # запрос без RIBCL-корня -> RESPONSE 0x400 'syntax error', но всё равно
    # с полным HTTP-префиксом (пишется в _serve_raw_ribcl/do_POST).
    if "<RIBCL" not in text.upper():
        return 200, _ribcl_message("0x00000400", "Request contained a syntax error.")

    # --- аутентификация ---
    if not login_ok:
        return 200, _ribcl_message("0x0000005f", "Login failed")

    parts: list[str] = []
    up = text.upper()

    # ВАЖНО: python-hpilo шлёт тег <GET_EMBEDDED_HEALTH/> (БЕЗ суффикса _DATA),
    # поэтому матчить надо по "GET_EMBEDDED_HEALTH" — старый паттерн
    # "GET_EMBEDDED_HEALTH_DATA" никогда не совпадал, mock молчал, и hpilo
    # падал с "Expected tag 'GET_EMBEDDED_HEALTH_DATA' not found".
    if "GET_EMBEDDED_HEALTH" in up:
        parts.append(_ribcl_embedded_health(p))
    if "GET_PRODUCT_NAME" in up:
        parts.append('<?xml version="1.0"?>\r\n<RIBCL VERSION="2.23">\r\n'
                     '<SERVER_INFO VALUE="0"><GET_PRODUCT_NAME VALUE="%s"/>'
                     '</SERVER_INFO>\r\n</RIBCL>' % p["model"])
    # ВАЖНО: python-hpilo ищет в ответе ТЕГ РЕЗУЛЬТАТА, а не тег запроса:
    # get_server_name -> SERVER_NAME, get_host_power_status -> GET_HOST_POWER
    # (см. _info_tag(returntags) в hpilo.py). Настоящий iLO4 отвечает именно так.
    if "GET_SERVER_NAME" in up:
        parts.append('<?xml version="1.0"?>\r\n<RIBCL VERSION="2.23">\r\n'
                     '<SERVER_INFO VALUE="0"><SERVER_NAME VALUE="%s"/>'
                     '</SERVER_INFO>\r\n</RIBCL>' % (p["host"] or "mock-dl380g8-01"))
    if "GET_HOST_POWER_STATUS" in up:
        parts.append('<?xml version="1.0"?>\r\n<RIBCL VERSION="2.23">\r\n'
                     '<SERVER_INFO VALUE="0"><GET_HOST_POWER HOST_POWER="true"/>'
                     '</SERVER_INFO>\r\n</RIBCL>')
    if "GET_FW_VERSION" in up or "GET_ALL_FIRMWARE_VERSIONS" in up:
        parts.append('<?xml version="1.0"?>\r\n<RIBCL VERSION="2.23">\r\n'
                     '<RIB_INFO><GET_FW_VERSION MANAGEMENT_PROCESSOR="iLO4"'
                     ' FIRMWARE_VERSION="2.82" BOOT_CODE="1.61"'
                     ' FPGA_IMAGE_VERSION="1.07" UEFI_STORED_VERSION="2.50"/>'
                     '</RIB_INFO>\r\n</RIBCL>')
    if "GET_IML" in up or "GET_IML_HEADER" in up:
        # текстовый дамп IML — для нашего ribcl-коллектора (парсит plain-text)
        iml = "\n".join(
            f"{i:05d} | 09/29/26 | {12 + i}:00:00 | {e['Message']} |"
            for i, e in enumerate(LOG_ENTRIES, 1))
        parts.append('<RIBCL VERSION="2.23">\n<RIB_INFO>\n<GET_IML>\n'
                     + iml + '\n</GET_IML>\n</RIB_INFO>\n</RIBCL>\n')

    if not parts:  # неизвестный запрос с валидным LOGIN — просто успех
        return 200, _ribcl_message("0x00000000", "No error")
    return 200, "\r\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    # IMPORTANT: python-hpilo пишет «сырой» запрос (заголовки + тело XML слитно,
    # без пустой строки-терминатора). При protocol_version == HTTP/1.1 BaseHTTP-
    # ServerReader ждёт терминатор и read() до EOF впадает во взаимный deadlock с
    # клиентом. С HTTP/1.0 стандартный парсер читает ровно Content-Length байт —
    # этот же путь используют и обычные запросы (requests/hpilo ILO_HTTP).
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("OData-Version", "4.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        # profile по заголовку Host (mock-ilo-N) — эмуляция разных серверов парка
        _handler_local.srv = type("S", (), {"host": self.headers.get("Host", "")})()
        p = self.path.split("?")[0].rstrip("/") or "/"
        if p == "/":
            host = self.headers.get("Host", "")
            name = host.partition(":")[0] or "mock-dl380g8-01"
            return self._json({"iLO": "web interface mock", "up": True, "name": name})
        if p == "/redfish/v1":
            p = "/redfish/v1/"

        routes = {
            "/redfish/v1/": {
                "@odata.id": "/redfish/v1/", "Id": "Root",
                "RedfishVersion": "1.2.0",
                "Systems": {"@odata.id": "/redfish/v1/Systems"},
                "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
                "Managers": {"@odata.id": "/redfish/v1/Managers"},
                "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
                "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
            },
            "/redfish/v1/Systems": {
                "@odata.id": "/redfish/v1/Systems", "MembersCount": 1,
                "Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
            "/redfish/v1/Systems/1": {
                "@odata.id": "/redfish/v1/Systems/1", "Id": "1", "Name": "Computer System",
                "PowerState": "On", "BiosVersion": "P29 06/18/2024",
                "Manufacturer": "HPE",
                "Model": _profile().get("model", "ProLiant DL380p Gen8"),
                "ProcessorSummary": {"Count": 2, "LogicalProcessorCount": 16},
                "MemorySummary": {"TotalSystemMemoryGiB": 64},
                "Processors": {"@odata.id": "/redfish/v1/Systems/1/Processors"},
                "Memory": {"@odata.id": "/redfish/v1/Systems/1/Memory"},
                "LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices"},
                "Status": {"State": "Enabled", "Health": "OK"}},
            "/redfish/v1/Systems/1/Processors": {
                "@odata.id": "/redfish/v1/Systems/1/Processors", "MembersCount": 2,
                "Members": [{"@odata.id": f"/redfish/v1/Systems/1/Processors/{i}"}
                            for i in (1, 2)]},
            "/redfish/v1/Systems/1/Memory": {
                "@odata.id": "/redfish/v1/Systems/1/Memory", "MembersCount": 8,
                "Members": [{"@odata.id": f"/redfish/v1/Systems/1/Memory/{i}"}
                            for i in range(1, 9)]},
            "/redfish/v1/Chassis": {
                "@odata.id": "/redfish/v1/Chassis", "MembersCount": 1,
                "Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
            "/redfish/v1/Chassis/1": {
                "@odata.id": "/redfish/v1/Chassis/1", "Id": "1", "Name": "Enclosure",
                "Thermal": {"@odata.id": "/redfish/v1/Chassis/1/Thermal"},
                "Power": {"@odata.id": "/redfish/v1/Chassis/1/Power"},
                "Status": {"State": "Enabled", "Health": "OK"}},
            "/redfish/v1/Chassis/1/Thermal": thermal(),
            "/redfish/v1/Chassis/1/Power": power(),
            "/redfish/v1/Managers": {
                "@odata.id": "/redfish/v1/Managers", "MembersCount": 1,
                "Members": [{"@odata.id": "/redfish/v1/Managers/iLO"}]},
            "/redfish/v1/Managers/iLO": {
                "@odata.id": "/redfish/v1/Managers/iLO", "Id": "iLO",
                "FirmwareVersion": "2.82", "Model": "iLO",
                "ManagerType": "ManagementController",
                "LogServices": {"@odata.id": "/redfish/v1/Managers/iLO/LogServices"},
                "Status": {"State": "Enabled", "Health": "OK"}},
            "/redfish/v1/UpdateService": {
                "@odata.id": "/redfish/v1/UpdateService",
                "FirmwareInventory": {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"}},
            "/redfish/v1/UpdateService/FirmwareInventory": {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory",
                "MembersCount": 3,
                "Members": [{"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/SystemRom"},
                            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/iLO"},
                            {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/HDDBackplane"}]},
            "/redfish/v1/UpdateService/FirmwareInventory/SystemRom": {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/SystemRom",
                "Id": "SystemRom", "Name": "System ROM", "Version": "P29_0618",
                "Status": {"State": "Enabled", "Health": "OK"}},
            "/redfish/v1/UpdateService/FirmwareInventory/iLO": {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/iLO",
                "Id": "iLO", "Name": "iLO Firmware", "Version": "2.82",
                "Status": {"State": "Enabled", "Health": "OK"}},
            "/redfish/v1/UpdateService/FirmwareInventory/HDDBackplane": {
                "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/HDDBackplane",
                "Id": "BP", "Name": "HDD Backplane", "Version": "1.42",
                "Status": {"State": "Enabled", "Health": "Warning"}},
            "/redfish/v1/Systems/1/LogServices": {
                "@odata.id": "/redfish/v1/Systems/1/LogServices", "MembersCount": 1,
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/LogServices/IML"}]},
            "/redfish/v1/Systems/1/LogServices/IML": {
                "@odata.id": "/redfish/v1/Systems/1/LogServices/IML", "Id": "IML",
                "Name": "Integrated Management Log",
                "Entries": {"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/Entries"}},
            "/redfish/v1/Managers/iLO/LogServices": {
                "@odata.id": "/redfish/v1/Managers/iLO/LogServices", "MembersCount": 1,
                "Members": [{"@odata.id": "/redfish/v1/Managers/iLO/LogServices/Security"}]},
            "/redfish/v1/Managers/iLO/LogServices/Security": {
                "@odata.id": "/redfish/v1/Managers/iLO/LogServices/Security",
                "Id": "Security", "Name": "Security Log",
                "Entries": {"@odata.id": "/redfish/v1/Managers/iLO/LogServices/Security/Entries"}},
        }

        if p.startswith("/redfish/v1/Systems/1/Processors/"):
            pid = p.rsplit("/", 1)[-1]
            return self._json({
                "@odata.id": p, "Id": pid, "Name": f"Processor {pid}",
                "Socket": f"CPU{pid}", "TotalCores": 8, "ThreadCount": 16,
                "MaxSpeedMHz": 2400,
                "ProcessorMetrics": {"AverageFrequencyMHz": random.randint(1200, 2400)},
                "Status": {"State": "Enabled", "Health": "OK"}})
        if p.startswith("/redfish/v1/Systems/1/Memory/"):
            mid = p.rsplit("/", 1)[-1]
            return self._json({
                "@odata.id": p, "Id": mid, "Name": f"DIMM slot {mid}",
                "CapacityMiB": 8192, "OperatingSpeedMhz": 1333, "Populated": True,
                "DeviceLocator": f"P{mid}",
                "Status": {"State": "Enabled", "Health": "OK"}})
        if p.endswith("/Entries"):
            origin = p[: -len("/Entries")]
            base = int(time.time())
            n = 2 + _profile()["idx"] % len(LOG_ENTRIES)   # у каждого сервера свой объём журнала
            return self._json({
                "@odata.id": p, "MembersCount": n,
                "Members": [
                    {"@odata.id": f"{p}/{i}", "Id": str(i), "EntryType": "Event",
                     "Created": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(base - i * 3600)),
                     "Severity": e["Severity"], "Message": {"Message": e["Message"]}}
                    for i, e in enumerate(LOG_ENTRIES[:n], 1)]})
        if p in routes:
            return self._json(routes[p])
        if p == "/ribcl":
            return self._json({"error": "use POST"}, 405)
        return self._json({"error": {"code": "ResourceMissing",
                                     "message": f"no such resource {p}"}}, 404)

    def handle_one_request(self):  # noqa: N802
        """Перехват «сырого» RIBCL поверх TLS.

        python-hpilo пишет в сокет байты `POST /ribcl HTTP/1.1\\r\\n...` без
        обязательного пустого терминального строки \\r\\n\\r\\n перед телом XML.
        Стандартный http.client-парсер запроса в этом случае не может отделить
        заголовки от тела (нет терминатора) и зависает/ошибается. Поэтому,
        если первая строка похожа на POST /ribcl, читаем всё соединение целиком
        (клиент шлёт один запрос и ждёт ответа; Connection: Close) и отдаём
        сырой ответ сами. Все остальные запросы — обычным путём."""
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline:
                self.close_connection = True
                return
            line = self.raw_requestline.decode("latin-1")
            if line.upper().startswith("POST /RIBCL"):
                # python-hpilo шлёт "POST /ribcl HTTP/1.1\r\nHost:...\r\n
                # Content-Length: N\r\nConnection: Close\r\n" и СРАЗУ тело XML —
                # без пустой терминальной строки. Поэтому читаем заголовки построчно
                # (readline не блокируется отсутствием терминатора), затем ровно
                # Content-Length байт тела. Гадать на read() до EOF нельзя: hpilo
                # после отправки ничего не пишет и ждёт ответ -> взаимный deadlock.
                length = 0
                while True:
                    hl = self.rfile.readline(65537)
                    if not hl or hl in (b"\r\n", b"\n"):
                        break
                    k, _, v = hl.decode("latin-1").partition(":")
                    if k.strip().lower() == "content-length":
                        try:
                            length = int(v.strip())
                        except ValueError:
                            length = 0
                body = self.rfile.read(length) if length else b""
                self._serve_raw_ribcl(line, body)
                self.close_connection = True
                return
            if self.parse_request():
                mname = "do_" + self.command
                method = getattr(self, mname, None)
                if method is None:
                    self.send_error(405, f"Unsupported method ({self.command!r})")
                else:
                    method()
                self.wfile.flush()
        except TimeoutError:
            self.log_error("Request timed out")
            self.close_connection = True

    def _serve_raw_ribcl(self, request_line: str, body: bytes) -> None:
        """Ответ на сырой RIBCL-запрос от python-hpilo: заголовки пишем вручную,
        ровно 'HTTP/1.1 200 OK\\r\\n...\\r\\n\\r\\n' + XML (библиотека требует
        именно такой префикс)."""
        # профиль сервера: python-hpilo шлёт HTTP-заголовок Host: localhost,
        # поэтому имя цели берём из SNI TLS-handshake (hpilo-exporter указывает
        # server_hostname=<имя цели>); если SNI нет — fallback на заголовок Host.
        sni = getattr(self.connection, "server_hostname", None) or ""
        if not sni or sni == "localhost":
            # headers ещё не распарсены (сырой путь) — Host ищем в байтах запроса
            mhost = re.search(rb"\r\nHost:\s*([^\r\n]+)", body, re.I)
            sni = mhost.group(1).decode("latin-1") if mhost else ""
        _handler_local.srv = type("S", (), {"host": sni or "mock-dl380g8-01"})()
        login_ok = True
        # проверка учётки: USER_LOGIN из <LOGIN ...>; mock принимает monitor/secret
        import re as _re
        m = _re.search(r'USER_LOGIN="([^"]*)"', body.decode("utf-8", "replace"))
        expected_user = os.environ.get("MOCK_ILO_USER", "monitor")
        if m and m.group(1) != expected_user:
            login_ok = False
        code, xml = _ribcl_response(body, login_ok=login_ok)
        status = "OK" if code == 200 else "Unauthorized"
        # ВАЖНО: python-hpilo при разборе HTTP-ответа безусловно читает
        # заголовок 'transfer-encoding' (KeyError, если его нет). Настоящий iLO4
        # отвечает chunked — эмулируем ровно так же.
        payload = xml.encode()
        chunks = hex(len(payload))[2:].encode() + b"\r\n" + payload + b"\r\n0\r\n\r\n"
        resp = (f"HTTP/1.1 {code} {status}\r\n"
                "Content-Type: text/ribcl\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n").encode() + chunks
        self.wfile.write(resp)
        self.wfile.flush()

    def do_POST(self):  # noqa: N802
        p = self.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if p == "/redfish/v1/SessionService/Sessions":
            try:
                creds = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                creds = {}
            token = f"mock-token-{random.randint(1000, 9999)}"
            loc = "/redfish/v1/SessionService/Sessions/mocksession"
            self.send_response(201)
            self.send_header("X-Auth-Token", token)
            self.send_header("Location", loc)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"@odata.id": loc,
                                         "UserName": creds.get("UserName", "mock")}).encode())
            return
        if p == "/ribcl":
            # Хорошо сформированный HTTP POST /ribcl. Два клиента:
            #  * наш ribcl-коллектор (requests) — profile берётся из заголовка Host;
            #  * python-hpilo (ILO_HTTP): пишет заголовки и тело XML ДВУМЯ отдельными
            #    TCP-пакетами (см. hpilo._communicate). BaseHTTPRequestHandler c
            #    protocol_version="HTTP/1.0" читает Content-Length байт сразу после
            #    пустой строки заголовков; если второй пакет ещё не пришёл — читаем
            #    недостающее из rfile дополнительно (с таймаутом сервера).
            length = int(self.headers.get("Content-Length", 0) or 0)
            if len(raw) < length:
                try:
                    raw += self.rfile.read(length - len(raw))
                except Exception:
                    pass
        if p == "/ribcl":
            sni = getattr(self.connection, "server_hostname", None) or ""
            host = sni or self.headers.get("Host", "")
            if not host or host.startswith("localhost"):
                mh = re.search(rb"\r?\nHost:\s*([^\r\n]+)", raw, re.I)
                if mh:
                    host = mh.group(1).decode("latin-1")
            _handler_local.srv = type("S", (), {"host": host or "mock-dl380g8-01"})()
            login_ok = True
            muser = re.search(rb'USER_LOGIN="([^"]*)"', raw)
            expected_user = os.environ.get("MOCK_ILO_USER", "monitor")
            if muser and muser.group(1).decode("latin-1") != expected_user:
                login_ok = False
            code, xml = _ribcl_response(raw, login_ok=login_ok)
            body = xml.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/ribcl")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(hex(len(body))[2:].encode() + b"\r\n" + body + b"\r\n0\r\n\r\n")
            return
        return self._json({"error": "unsupported"}, 404)

    def do_DELETE(self):  # noqa: N802
        self.send_response(204)
        self.end_headers()


def make_selfsigned(cert_dir: str) -> tuple[str, str]:
    key = f"{cert_dir}/mock.key"
    crt = f"{cert_dir}/mock.crt"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key, "-out", crt, "-days", "365", "-subj", "/CN=mock-ilo.local",
    ], check=True, capture_output=True)
    return crt, key


def main():
    ap = argparse.ArgumentParser(description="Mock HPE iLO Redfish server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--plain", action="store_true", help="HTTP вместо HTTPS")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    if args.plain:
        print(f"[mock-ilo] HTTP  on {args.host}:{args.port}", flush=True)
        httpd.serve_forever()
        return

    with tempfile.TemporaryDirectory() as tmp:
        crt, key = make_selfsigned(tmp)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print(f"[mock-ilo] HTTPS on {args.host}:{args.port} (self-signed)", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

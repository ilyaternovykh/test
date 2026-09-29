#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фейковый iLO на Redfish для тестов без «железа».

Имитирует DL380 Gen8 (Redfish 1.0, ограниченно) и DL360 Gen10 / iLO5
(полный Redfish): Systems/Processors/Memory, Chassis/Thermal/Power,
Managers, UpdateService/FirmwareInventory, LogServices (IML-like записи),
а также POST /ribcl (дамп IML) и SessionService (для RIBCL-through-Redfish).

Запуск:   python3 tests/mock_ilo.py --port 8443 [--plain]
Тесты:    ILO_URL=https://localhost:8443 python3 -m exporter --once
"""

from __future__ import annotations

import argparse
import json
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


class Handler(BaseHTTPRequestHandler):
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
            iml = "\n".join(
                f"{i:05d} | 09/29/26 | {12 + i}:00:00 | {e['Message']} |"
                for i, e in enumerate(LOG_ENTRIES, 1))
            body = ('<RIBCL VERSION="2.23">\n<RIB_INFO>\n<GET_IML>\n'
                    + iml + '\n</GET_IML>\n</RIB_INFO>\n</RIBCL>\n').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/ribcl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
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

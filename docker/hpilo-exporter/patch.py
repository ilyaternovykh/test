"""Патч upstream hpilo-exporter для работы в стенде.

Проблемы upstream (src/hpilo_exporter/exporter.py, коммит 6c02bd7 от 2026-09-25):

1. Логика параметров: `query.get("ilo_host") or os.environ["ilo_host"]`.
   Если ENV ilo_user/ilo_password НЕ заданы (а per-server пароли в этой схеме
   вообще невозможно передать через ENV), любое скрейп-запрос вида
   /metrics?ilo_host=X&ilo_port=Y падает с KeyError -> print_err("missing
   parameter 'ilo_user'") -> HTTP 500. Prometheus видит target DOWN.

2. В error-path (`return_error`) метрика `hpilo_up` не выставляется вовсе,
   поэтому в Grafana нет ни значения 0, ни возможности алертиться на
   недоступность iLO по этому экспортёру.

Исправления (идемпотентные, точечные):
A) Базовые креды из ENV опциональны: при отсутствии берём monitor/secret
   (тестовый стенд); отсутствие ilo_host в URL+ENV остаётся фатальным.
B) При ошибке парсинга отдаём валидный exposition c hpilo_up{server_name=...} 0
   и hpilo_scrape_error 1 вместо голого текстового 500.
C) Добавляем Gauge hpilo_up в __init__ реального пути сбора (1 при успехе).
"""

from pathlib import Path
import sys


def patch(path: Path) -> None:
    src = path.read_text()

    if "HPiLO_PATCHED" in src:
        print(f"[patch] {path}: already patched, skip")
        return

    orig = src

    # --- A) ENV-креды опциональны ---
    old_a = """            try:
                ilo_host = (
                    query_components.get("ilo_host", [""])[0] or os.environ["ilo_host"]
                )
                ilo_user = (
                    query_components.get("ilo_user", [""])[0] or os.environ["ilo_user"]
                )
                ilo_password = (
                    query_components.get("ilo_password", [""])[0]
                    or os.environ["ilo_password"]
                )
            except KeyError as e:
                print_err("missing parameter %s" % e)
                self.return_error()
                error_detected = True"""
    new_a = """            # HPiLO_PATCHED: базовые креды из ENV опциональны; per-server
            # пароли можно передавать параметром запроса (?ilo_user=&ilo_password=),
            # а при их отсутствии используем дефолты стенда monitor/secret.
            try:
                ilo_host = (
                    query_components.get("ilo_host", [""])[0] or os.environ["ilo_host"]
                )
            except KeyError as e:
                print_err("missing parameter %s" % e)
                ilo_host = ""
            try:
                ilo_user = (
                    query_components.get("ilo_user", [""])[0]
                    or os.environ.get("ilo_user")
                    or "monitor"
                )
                ilo_password = (
                    query_components.get("ilo_password", [""])[0]
                    or os.environ.get("ilo_password")
                    or "secret"
                )
            except KeyError as e:
                print_err("missing parameter %s" % e)
                self.return_error()
                error_detected = True
            if not ilo_host:
                self.return_error()
                error_detected = True
                return  # HPiLO_PATCHED: не продолжать с пустым хостом (upstream падал с NameError)"""
    if old_a in src:
        src = src.replace(old_a, new_a)
    else:
        raise SystemExit("[patch] ERROR: block A (params parsing) not found — upstream changed")

    # --- B) return_error -> валидный metrics-body с hpilo_up==0 ---
    old_b = """    def return_error(self):
        self.send_response(500)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(bytes("Error(s) detected" + "\\n", "utf-8"))"""
    new_b = """    def return_error(self):
        # HPiLO_PATCHED: отдаём валидный exposition вместо текстового 500,
        # чтобы target был UP, а недоступность iLO была видна как hpilo_up==0
        # (алерты/дашборд) и не ломала парсер Prometheus.
        body = (
            "# HELP hpilo_up Whether the iLO RIBCL scrape succeeded.\\n"
            "# TYPE hpilo_up gauge\\n"
            'hpilo_up{server_name="%s"} 0\\n'
            "# HELP hpilo_scrape_error Scrape ended with an error.\\n"
            "# TYPE hpilo_scrape_error gauge\\n"
            'hpilo_scrape_error{server_name="%s"} 1\\n'
            % (getattr(self, "server_name", "unknown"),
               getattr(self, "server_name", "unknown"))
        )
        self.send_response(200)
        self.send_header("Content-type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))"""
    if old_b in src:
        src = src.replace(old_b, new_b)
    else:
        raise SystemExit("[patch] ERROR: block B (return_error) not found — upstream changed")

    # --- E) ilo_port: приоритет query-параметра над ENV ---
    # В upstream `query or os.environ["ilo_port"]`: если в контейнере задан ENV
    # ilo_port и при этом запрос пришёл БЕЗ ilo_port, скрейп уходит на ENV-порт
    # вместо порта из адреса цели. Новый формат targets/hpilo-targets.json
    # всегда передаёт ?ilo_port= (relabel_configs в prometheus.yml), но делаем
    # ENV только fallback: «порт из запроса > ENV > 443».
    # Якоря — по устойчивым подстрокам (upstream переставлял блоки).
    old_f = '''            try:
                ilo_port = int(
                    query_components.get("ilo_port", [""])[0] or os.environ["ilo_port"]
                )
            except Exception:
                ilo_port = 443'''
    new_f = """            try:
                # HPiLO_PATCHED: порт из запроса (file_sd params.ilo_port) важнее ENV
                ilo_port = int(
                    query_components.get("ilo_port", [""])[0]
                    or os.environ.get("ilo_port")
                    or 443
                )
            except Exception:
                ilo_port = 443"""
    if old_f in src:
        src = src.replace(old_f, new_f)
    else:
        # вариант 2: ENV обязателен через .get(...) без `or 443` в except
        old_f2 = '''            try:
                ilo_port = int(
                    query_components.get("ilo_port", [""])[0] or os.environ.get("ilo_port")
                )
            except Exception:
                ilo_port = 443'''
        if old_f2 in src:
            src = src.replace(old_f2, new_f)
        elif "HPiLO_PATCHED: порт из запроса" in src:
            pass  # уже пропатчено этим блоком ранее
        else:
            raise SystemExit("[patch] ERROR: block E (ilo_port parsing) not found — upstream changed")

    # --- C) hpilo_up == 1 на успешном пути (после сбора embedded_health) ---
    old_c = """                # get health, mod by n27051538
                self.embedded_health = ilo.get_embedded_health()"""
    new_c = """                # get health, mod by n27051538
                self.embedded_health = ilo.get_embedded_health()
                # HPiLO_PATCHED: success-маркер доступности iLO
                self.gauges["up"].labels(server_name=self.server_name).set(1)"""
    if old_c in src:
        src = src.replace(old_c, new_c)
    else:
        raise SystemExit("[patch] ERROR: block C (embedded_health) not found — upstream changed")

    # --- D) Сброс hpilo_up==0 в начале каждого скрейпа: если iLO недоступно,
    #     stale-значение 1 из прошлого успешного скрейпа не должно оставаться.
    old_e = """        if url.path == self.server.endpoint:
            ilo_host = None"""
    new_e = """        if url.path == self.server.endpoint:
            # HPiLO_PATCHED: fresh scrape -> up=0 по умолчанию; success set(1) позже
            self.gauges["up"].labels(server_name=query_components.get("ilo_host", ["unknown"])[0] or "unknown").set(0)
            ilo_host = None"""
    if old_e in src:
        src = src.replace(old_e, new_e)
    else:
        raise SystemExit("[patch] ERROR: block D (scrape start) not found — upstream changed")

    # Gauge "up" должен существовать в self.gauges (dict-литерал в __init__).
    # Вставляем новый элемент словаря сразу после открытия `self.gauges = {`.
    old_d = "        self.gauges = {\n"
    if old_d not in src:
        raise SystemExit("[patch] ERROR: gauges dict anchor not found")
    add = (
        "        self.gauges = {\n"
        "            # HPiLO_PATCHED: up/down доступность iLO по RIBCL-скрейпу\n"
        '            "up": Gauge(\n'
        '                self.P + "up",\n'
        '                "Whether the iLO RIBCL scrape succeeded",\n'
        '                ["server_name"],\n'
        "                registry=self.registry,\n"
        "            ),\n"
    )
    src = src.replace(old_d, add, 1)

    if src == orig:
        raise SystemExit("[patch] ERROR: nothing applied")

    path.write_text(src)
    print(f"[patch] {path}: patched OK")


if __name__ == "__main__":
    candidates = []
    for arg in sys.argv[1:]:
        candidates.append(Path(arg))
    if not candidates:
        # автопоиск в editable-install
        import hpilo_exporter  # noqa: F401
        candidates.append(Path(hpilo_exporter.__file__).with_name("exporter.py"))
    for c in candidates:
        patch(c)

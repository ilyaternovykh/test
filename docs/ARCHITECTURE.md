# Мониторинг парка HPE ProLiant DL380p Gen8 (22 шт.) через iLO

## Схема

```
[iLO #1]──┐
[iLO #2]──┤   Redfish (/redfish/v1) + web-проверка (HTTPS GET /)
  ...     ├───► ilo-exporter (1 контейнер, push-обход парка) ──► /metrics
[iLO #22]─┘        │ 9127                            Prometheus :9090 ──► Grafana :3000
                   │                                 Alertmanager :9093 ◄─┘ (правила алертов)
             targets.yaml (список iLO + креды из .env)
```

* **Один сборщик на весь парк.** Exporter сам обходит все 22 iLO каждые 60 с
  (8 потоков), Prometheus лишь скрапит один эндпоинт `/metrics`. Это щадит iLO
  (у Gen8 веб-сервис слабый) и даёт мгновенный «парковый» обзор.
* **Проверка доступности сервера** — коллектор `web`: HTTPS-запрос к корню iLO.
  Работает даже там, где Redfish недоступен/устарел. Метрика `ilo_web_up`.
* **Gen8 оговорка:** на DL380p Gen8 установлен **iLO2**, полноценного Redfish
  у него нет (Redfish появился с iLO4/Gen9). Коллектор `redfish` написан
  «широко»: что устройство отдаёт — то становится метриками. Для Gen8
  гарантированно работают: web-доступность, сертификат, а также SNMP/RIBCL при
  необходимости. На Gen9+ (iLO4/iLO5) включаются все Redfish-метрики.
* **IML/IEL — задел:** коллектор `ribcl` умеет снимать дампы IML/IEL двумя
  путями: RIBCL-through-Redfish (POST SessionService → GET Managers/iLO/RIBCL,
  iLO4+) и классический POST `/ribcl` XML (iLO2..iLO5). Включается per-target:
  `ribcl_enabled: true`, `ribcl_command: "GET IML" | "GET IEL"`.
  Сырые тексты логов в метрики не кладём (кардинальность); для полного хранения
  следующий шаг — Loki + экспортер логов.

## Метрики (основные)

| Метрика | Смысл |
|---|---|
| `ilo_web_up{target}` | web iLO доступен — «сервер жив вообще» |
| `ilo_web_response_time_seconds` | пингвеб-интерфейса |
| `ilo_web_ssl_verify_ok`, `ilo_web_cert_expire_days` | TLS-цепочка и срок сертификата iLO |
| `ilo_redfish_up` | Redfish API отвечает |
| `ilo_redfish_power_state{state}` | On/Off сервера |
| `ilo_redfish_thermal_temperature_celsius{name}` | датчики температуры |
| `ilo_redfish_thermal_fan_rpm{name}` | вентиляторы |
| `ilo_redfish_psu_present/output_watts{name}` | блоки питания |
| `ilo_redfish_power_redundancy_ok` | резервирование питания |
| `ilo_redfish_status_ok{resource,id,health,state}` | Health любого ресурса |
| `ilo_redfish_cpu_cores/dimm_*` | конфигурация CPU/RAM |
| `ilo_redfish_ilo_firmware_version_info{fw_version}` | версия прошивки iLO |
| `ilo_redfish_log_entries_total{log}`, `ilo_redfish_event_count{severity}` | журналы (IML/IEL через Redfish LogServices) |
| `ilo_ribcl_up`, `ilo_ribcl_iml_entries`, `ilo_ribcl_dump_bytes` | дампы RIBCL (задел) |
| `ilo_*_collect_success{collector}` | 0 — коллектор упал (самодиагностика) |
| `ilo_exporter_last_run_timestamp_seconds` | цикл сбора завис? |

## Стенд (docker compose)

```bash
cp .env.example .env          # задать креды
docker compose up -d --build
./scripts/smoke-test.sh       # проверка всей цепочки
```

Порты: Grafana 3000 (admin/admin), Prometheus 9090, exporter 9127, AM 9093,
mock-iLO 8443 (HTTPS, самоподписанный; `tests/mock_ilo.py`).

Дашборд провижинится автоматически: папка «HPE iLO» → «HPE ProLiant iLO — парк
серверов». Алерты: `docker/prometheus/alerts.yml` (iLO down, перегрев, PSU,
вентиляторы, потеря redundancy, выключенный сервер, новые события IML).

## Переход в прод

1. Сгенерируйте список целей: `./scripts/gen-targets.sh hosts.txt > targets/targets.yaml`
   (или отредактируйте `targets/targets.yaml.example`).
2. Креды только из `.env`/secret-хранилища, аккаунт — read-only.
3. Для Gen8 проверьте: включён ли Remote CLI/Redfish в настройках доступа iLO;
   при недоступности Redfish остаётся полный контроль доступности по `web`.
4. Горизонтальное масштабирование не нужен до ~100 iLO; дальше — разбиение
   targets по шардам экспортёров.

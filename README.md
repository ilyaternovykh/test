# ilo-monitoring — мониторинг парка HPE ProLiant (DL380p Gen8 x22) через iLO

Единый сборщик (`exporter/`, Python, без внешних агентов) опрашивает iLO по
Redfish + проверяет доступность web-интерфейса, отдаёт метрики Prometheus;
стек Prometheus → Grafana (+ Alertmanager) поднимается одним `docker compose`.
Собран задел по съёму IML/IEL (RIBCL / Redfish LogServices).

## Быстрый старт (тесты)

```bash
cp .env.example .env
docker compose up -d --build
open http://localhost:3000   # Grafana, admin/admin, дашборд в папке "HPE iLO"
open http://localhost:9090/targets
./scripts/smoke-test.sh
```

Без Docker, локально:

```bash
python3 tests/mock_ilo.py --port 8443 &      # фейковый iLO
pip install -r exporter/requirements.txt
python3 -m exporter --config targets/targets.yaml --once   # дамп метрик
```

## Структура

```
exporter/            единый сборщик (web/redfish/ribcl коллекторы)
targets/             ЕДИНЫЙ ИНВЕНТАРЬ + сгенерированные конфиги (см. ниже)
docker-compose.yml   стенд: ilo-exporter + hpilo-exporter + prometheus + grafana + alertmanager + mock-ilo
docker/              Dockerfile экспортёра, конфиги prometheus/alerts/grafana
scripts/             smoke-test.sh, gen-targets.sh (генерация конфигов из inventory.ini)
docs/ARCHITECTURE.md детали, метрики, переход в прод
tests/mock_ilo.py    фейковый iLO (Redfish + RIBCL) для тестов
```

## Единый инвентарь серверов (IP + имя — один файл)

`targets/inventory.ini` — единственный источник правды про имена и адреса iLO.
После любой правки перегенерировать производные конфиги:

```bash
./scripts/gen-targets.sh        # или: python3 scripts/gen_targets.py
```

Что генерируется из него:
* `targets/targets.yaml`         — цели нашего ilo-exporter;
* `targets/hpilo-targets.json`   — Prometheus file_sd для hpilo-exporter;
* метки `server/target/name` совпадают у обоих сборщиков, поэтому переменная
  `$server` и алерты работают по всему парку одинаково.

Пример строки инвентаря (реальный сервер):
```ini
[servers]
dl380-prod-07 ansible_host=10.20.30.17 rack=r03 user=ilo_admin collectors=web,redfish,ribcl
```

## hpilo-exporter (дополнительный сборщик)

Собран из форка (`build: context: ./docker/hpilo-exporter` — upstream-Dockerfile +
применение `patch.py`, см. комментарии в нём), слушает :9416, секреты берёт из ENV
(`ILO_USER/ILO_PASS` из `.env`).
Prometheus скрейпит его job `hpilo` по схеме `/metrics?ilo_host=<IP>&ilo_port=<порт>`:
адрес каждой цели (`<хост>:<порт>` из inventory) лежит в `targets[]` файла file_sd и
подставляется в query-параметры relabel_configs'ами в `docker/prometheus/prometheus.yml`.
В файле file_sd **не должно** быть поля `params` — discovery-формат принимает только
`targets`/`labels` (строгая проверка: `json: unknown field "params"` роняет весь job). Дашборд Grafana 13709 «HP iLO» лежит в `docker/grafana/dashboards/hp-ilo-13709.json`
(папка "HPE iLO", datasource Prometheus uid `PBFA97CFB590B2093`).

Подробнее — docs/ARCHITECTURE.md.

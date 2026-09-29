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
targets/             конфигурация списка iLO (targets.yaml + example)
docker-compose.yml   стенд: exporter + prometheus + grafana + alertmanager + mock-ilo
docker/              Dockerfile экспортёра, конфиги prometheus/alerts/grafana
scripts/             smoke-test.sh, gen-targets.sh (генератор целей из списка хостов)
docs/ARCHITECTURE.md детали, метрики, переход в прод
tests/mock_ilo.py    фейковый iLO (Redfish + RIBCL) для тестов
```

Подробнее — docs/ARCHITECTURE.md.

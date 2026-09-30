# hpilo-exporter (форк с фиксом Docker-запуска)

Обёртка над https://github.com/hpilo-exporter/hpilo-exporter — дополнительный
сборщик метрик `hpilo_*` (RIBCL XML health_at_a_glance: вентиляторы, температура,
питание, контроллеры/диски, прошивка), обслуживающий дашборд Grafana 13709 «HP iLO».

## Почему не `build: context: git+https://...` напрямую

Upstream-Dockerfile использует `ENTRYPOINT ["hpilo-exporter"]`. В связке с
compose-`command` аргументы склеивались неверно, процесс падал на старте,
и job `hpilo` в Prometheus был пустым. Наш образ задаёт параметры через `CMD`,
что исключает конфликт ENTRYPOINT/CMD.

## Конфигурация

* Секреты: ENV `ilo_user`, `ilo_password`, `ilo_port` (см. docker-compose.yml;
  значения берутся из `.env`: `ILO_USER`/`ILO_PASS`).
* Цели: ЕДИНЫЙ инвентарь `targets/inventory.ini` → генерация file_sd
  `./scripts/gen-targets.sh` → `targets/hpilo-targets.json`.
  Prometheus перечитывает file_sd сам (refresh_interval 60s); рестарт не нужен.
* Схема скрейпа: on-demand proxy — Prometheus шлёт
  `GET http://hpilo-exporter:9416/metrics?ilo_host=<IP>&ilo_port=<порт>`;
  секреты в URL не попадают (только ENV контейнера).
* Label `ilo_host` проставляется file_sd — его использует легенда дашборда 13709.

## Прод vs тестовый стенд

* Стенд: mock-ilo слушает 8443 → `ilo_port: "8443"` в compose и
  `ansible_host=https://mock-ilo:8443` в inventory.ini.
* Прод: реальные IP iLO (порт 443) в inventory.ini + `ilo_port: "443"`.
  После правки inventory.ini обязательно перегенерировать цели.

## Известные ловушки (проверено E2E)

1. **`ilo_host=localhost` в params — запрещено.** Библиотека `hpilo-python` при
   hostname == 'localhost' переключается на локальный интерфейс через утилиту
   `hponcfg` (ILO_LOCAL), а не идёт по сети; в контейнере её нет →
   `IloCommunicationError: Cannot run /sbin/hponcfg`. В `params.ilo_host`
   всегда должен быть реальный адрес iLO (генератор `scripts/gen_targets.py`
   берёт его из `ansible_host` inventory).
2. **Отображаемое имя ≠ адрес подключения.** В `labels.ilo_host` (его читает
   дашборд 13709 и переменная `$server`) пишется ИМЯ сервера из inventory;
   фактический адрес — в `params.ilo_host` и `labels.ilo_addr`.
3. **Дашборд 13709 использует метрики `hpilo_*`** (другой префикс, чем нашего
   ilo-exportter) — панели «HP iLO» наполняются только job'ом `hpilo`; это
   ожидаемо и нормально (два независимых источника данных).
4. **Сборка через proxy.** В Dockerfile добавлены `ARG HTTP_PROXY/HTTPS_PROXY/NO_PROXY`
   (только build-time, в рантайме прокси сброшен — иначе сломается доступ к iLO).
   Перед сборкой экспортируйте переменные в той же shell-сессии:
   `export HTTP_PROXY=http://proxy:3128 HTTPS_PROXY=$HTTP_PROXY NO_PROXY=localhost,127.0.0.1,.svc,172.16.0.0/12`
   затем `docker compose build hpilo-exporter`. Альтернатива — `~/.docker/config.json`
   с `"proxies": {"default": {...}}` (подхватывается автоматически).
   Для корпоративного MITM-прокси с самоподписанным сертификатом смонтируйте CA:
   `build: extra_hosts` не нужен, достаточно тома с `.crt` + `update-ca-certificates`,
   либо отключите проверку для сборки (`GIT_SSL_NO_VERIFY=1`, `pip --trusted-host`).

## Диагностика: job `hpilo` не появился в /targets

Если в контейнере prometheus файл `/etc/prometheus/targets/hpilo-targets.json`
есть и содержит ваши серверы, а job всё равно отсутствует — значит в запущенном
контейнере старый `prometheus.yml` (volume монтируется при создании контейнера;
при обновлении репозитория на лету содержимое может не переехать без пересоздания):

```bash
docker compose up -d --force-recreate prometheus
# проверить, что конфиг реально загружен (должен быть job hpilo с file_sd_configs):
curl -s http://localhost:9090/api/v1/status/config | python3 -m json.tool | grep -A3 hpilo
```

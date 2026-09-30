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

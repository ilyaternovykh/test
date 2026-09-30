# Форк hpilo-exporter с фиксом запуска в docker compose.
#
# Проблемы upstream-Dockerfile (github.com/hpilo-exporter/hpilo-exporter, main):
#  1) ENTRYPOINT ["hpilo-exporter"] + compose `command: ["--address=0.0.0.0", ...]`
#     склеиваются в argv как "hpilo-exporter --address=... " — это работает только
#     если command передавать СООТВЕТСТВУЮЩЕГО формата; в нашем стенде вызывал
#     argparse error и контейнер падал -> hpilo job не появлялся в Prometheus.
#     Здесь: явный CMD со всеми флагами, без ENTRYPOINT-склейки.
#  2) psutil собирается из исходников (нужны linux-headers/build-base).
FROM python:3.13.3-alpine3.20

# Прокси для сборки (нужен, если у хоста мониторинга выход в интернет только через proxy).
# Задаются на уровне docker build:
#   HTTP_PROXY=http://proxy:3128 HTTPS_PROXY=http://proxy:3128 NO_PROXY=localhost,127.0.0.1 \
#     docker compose build hpilo-exporter
# Либо через ~/.docker/config.json -> "proxies": {"default": {...}} — тогда docker сам
# подставит встроенные ARG и эти строки станут no-op.
# ВАЖНО: только ARG (не ENV) — иначе прокси «зашьётся» в образ и будет ломать
# обращение к iLO/сетям compose в рантайме.
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    no_proxy=${NO_PROXY}

RUN apk --no-cache --virtual .build-steps add linux-headers build-base gcc musl-dev \
    && pip install --no-cache-dir psutil \
    && apk --no-cache del .build-steps

# Фиксируем коммит upstream для воспроизводимости сборки.
ARG HPILO_REF=main
RUN apk --no-cache add git \
    # git не читает *_PROXY без явной настройки; берём из того же ARG:
    && git config --global http.proxy "${HTTPS_PROXY}${HTTP_PROXY}" \
    && git clone --depth 1 --branch ${HPILO_REF} https://github.com/hpilo-exporter/hpilo-exporter.git /usr/src/hpilo_exporter \
    && pip install --no-cache-dir -e /usr/src/hpilo_exporter \
    && apk --no-cache del git

# Сбрасываем прокси: в рантайме контейнер ходит только на iLO внутри сети compose.
ENV http_proxy= https_proxy= no_proxy=

RUN addgroup -S hpilo_exporter && adduser -S hpilo_exporter -G hpilo_exporter
USER hpilo_exporter

EXPOSE 9416
# Явный CMD: listen на всех интерфейсах, порт/эндпоинт совпадают с job `hpilo`
# в prometheus.yml (metrics_path=/metrics, targets hpilo-exporter:9416).
CMD ["hpilo-exporter", "--address=0.0.0.0", "--port=9416", "--endpoint=/metrics"]

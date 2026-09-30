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

RUN apk --no-cache --virtual .build-steps add linux-headers build-base gcc musl-dev \
    && pip install --no-cache-dir psutil \
    && apk --no-cache del .build-steps

# Фиксируем коммит upstream для воспроизводимости сборки.
ARG HPILO_REF=main
RUN apk --no-cache add git \
    && git clone --depth 1 --branch ${HPILO_REF} https://github.com/hpilo-exporter/hpilo-exporter.git /usr/src/hpilo_exporter \
    && pip install --no-cache-dir -e /usr/src/hpilo_exporter \
    && apk --no-cache del git

RUN addgroup -S hpilo_exporter && adduser -S hpilo_exporter -G hpilo_exporter
USER hpilo_exporter

EXPOSE 9416
# Явный CMD: listen на всех интерфейсах, порт/эндпоинт совпадают с job `hpilo`
# в prometheus.yml (metrics_path=/metrics, targets hpilo-exporter:9416).
CMD ["hpilo-exporter", "--address=0.0.0.0", "--port=9416", "--endpoint=/metrics"]

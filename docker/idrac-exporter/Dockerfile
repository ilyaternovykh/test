# idrac_exporter (github.com/mrlhansen/idrac_exporter) — Redfish-экспортёр
# (iDRAC / iLO5+ / XClarity / Supermicro). Цели и креды берутся из ЕДИНОГО
# инвентаря: scripts/gen-targets.sh генерирует targets/idrac-config.yml,
# который монтируется в контейнер (см. docker-compose.yml).
#
# Берём статичный бинарник из release — проще и воспроизводимее, чем пересборка
# upstream-Dockerfile'ом (golang:alpine, непиненная версия). Версия и checksum
# пинятся явно.
FROM alpine:3.20

# Пиним релиз: https://github.com/mrlhansen/idrac_exporter/releases
ARG IDRAC_EXPORTER_VERSION=v2.6.3
# sha256 release-ассета (curl -sL <url> | sha256sum); "0" = не проверять (только отладка!)
ARG IDRAC_EXPORTER_SHA256=4e52e7101894646995d30219cceae22f2faaaeb1bde932306495a3d29081f054

RUN apk add --no-cache ca-certificates && mkdir -p /app/bin /etc/prometheus

ADD https://github.com/mrlhansen/idrac_exporter/releases/download/${IDRAC_EXPORTER_VERSION}/idrac_exporter-${IDRAC_EXPORTER_VERSION}-linux-amd64 /app/bin/idrac_exporter

RUN set -eux; \
    if [ "${IDRAC_EXPORTER_SHA256}" != "0" ]; then \
        echo "${IDRAC_EXPORTER_SHA256}  /app/bin/idrac_exporter" | sha256sum -c -; \
    fi; \
    chmod +x /app/bin/idrac_exporter

EXPOSE 9348

# Конфиг — из единого inventory (targets/idrac-config.yml, volume в compose);
# --config-expand-env разрешает ${VAR} в конфиге (креды default-хоста из ENV),
# --config-watch перезагружает конфиг после ./scripts/gen-targets.sh.
ENTRYPOINT ["/app/bin/idrac_exporter", \
            "--config", "/etc/prometheus/idrac.yml", \
            "--config-expand-env", \
            "--config-watch"]

# ilo-exporter — единый сборщик метрик iLO (Redfish + web-доступность + задел RIBCL/IML/IEL)
FROM python:3.12-slim

RUN groupadd -r exporter && useradd -r -g exporter exporter

WORKDIR /app

COPY exporter/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY exporter/ /app/exporter/

USER exporter
EXPOSE 9127

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:9127/healthz', timeout=4); sys.exit(0 if r.status==200 else 1)"

ENTRYPOINT ["python3", "-m", "exporter"]
CMD ["--config", "/etc/ilo-exporter/targets.yaml"]

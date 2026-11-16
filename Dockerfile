# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NETAGENT_DATA_DIR=/data \
    NETAGENT_HOST=0.0.0.0 \
    NETAGENT_PORT=8080

WORKDIR /app
COPY requirements.txt .
# Behind a TLS-inspecting proxy, pass your CA bundle so pip can verify it:
#   docker build --secret id=ca,src=/path/to/corp-ca.pem -t netagent .
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir -r requirements.txt

COPY netagent ./netagent
COPY profiles ./profiles
COPY knowledge ./knowledge
COPY config ./config

RUN useradd --system --uid 10001 --home /app netagent \
    && mkdir -p /data && chown -R netagent /data /app
USER netagent

VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "netagent", "serve"]

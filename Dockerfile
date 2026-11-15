FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PA_ENV=production \
    PA_DATA_DIR=/data \
    PA_BIND=0.0.0.0 \
    PA_CONTAINER_BIND=true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pa ./pa

RUN useradd -r -u 10001 pa && mkdir -p /data && chown pa /data
USER pa
VOLUME /data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys;\
sys.exit(0 if urllib.request.urlopen(\
'http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
# single process: waitress serves WSGI, worker + discord bot embedded
CMD ["python", "-m", "pa", "serve"]

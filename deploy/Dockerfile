# amrit-adapter — API + worker share one image. Build context = adapter/ (run: docker build -f deploy/Dockerfile .)
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY amrit_adapter ./amrit_adapter
COPY migrations ./migrations
COPY config ./config
RUN useradd -r -u 10001 adapter && mkdir -p /data && chown adapter /data
USER adapter
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health').status==200 else 1)"
CMD ["uvicorn", "amrit_adapter.api:app", "--host", "0.0.0.0", "--port", "8787"]

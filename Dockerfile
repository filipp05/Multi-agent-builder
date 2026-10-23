FROM python:3.12-slim AS runtime
WORKDIR /app
COPY requirements-contracts.txt requirements-app.txt ./
RUN pip install --no-cache-dir -r requirements-app.txt
COPY app app
COPY contracts contracts
RUN useradd --uid 10001 --create-home app && mkdir -p /data /app/.scratch && chown app:app /data /app/.scratch
ENV RENOVATION_DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]

FROM runtime AS test
USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN python -m playwright install --with-deps chromium
RUN mkdir -p /app/.scratch/filipp-mvp/runtime && ln -s /opt/browsers /app/.scratch/filipp-mvp/runtime/browsers
COPY scripts scripts
COPY evals evals
COPY tests tests
CMD ["python", "-m", "pytest", "-q"]

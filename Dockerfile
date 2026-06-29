# feiyangyang web service — single-worker FastAPI on Fargate.
FROM python:3.12-slim

# Non-root runtime user.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# App source. data_loader writes the cache under /app/data/ohlc at warmup.
COPY src/ /app/src/
RUN mkdir -p /app/data/ohlc && chown -R app:app /app
USER app

WORKDIR /app/src
EXPOSE 8080
# SINGLE worker — uvicorn defaults to one in-process worker; do NOT pass --workers
# (even =1 spawns a supervisor) so the ~1GB warm cache + job registry stay single-process.
CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]

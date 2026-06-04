# UAT Defect Triage Agent — container image for Cloud Run.
#
# Build:    docker build -t uat-defect-agent .
# Run:      docker run --rm -p 8080:8080 --env-file .env uat-defect-agent
# Deploy:   gcloud run deploy uat-defect-agent --source . --region europe-west2

FROM python:3.11-slim

# Don't write .pyc, keep stdout unbuffered for Cloud Logging.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the application code.
COPY . .

# Cloud Run passes the listen port as $PORT (default 8080).
EXPOSE 8080

# Single-worker uvicorn is fine for our throughput (low). If we ever need
# more concurrency, bump --workers but check that BackgroundTasks behave
# correctly under a multi-process worker model (they don't share state, but
# we don't share state anyway).
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1

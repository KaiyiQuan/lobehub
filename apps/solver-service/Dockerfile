# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY scripts ./scripts
COPY data ./data

# TravelPlanner reference data: sha256-verified. Files already present in the
# build context (local dev) are reused; on a clean checkout (Railway) they are
# downloaded from the pinned Hugging Face revision. See README "Reference data".
RUN python scripts/fetch_data.py --out /app/data

RUN useradd --system --uid 10001 solver
USER 10001

ENV PYTHONPATH=/app/src \
    SOLVER_TP_DATA_DIR=/app/data

EXPOSE 8000

# python -m solver_service validates config in the master process before
# forking uvicorn workers (missing API key => immediate exit 1). Railway
# injects $PORT; SOLVER_WORKERS controls worker processes (see README "Scaling").
CMD ["python", "-m", "solver_service"]

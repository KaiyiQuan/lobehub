# In-region load generator (see loadtest/loadgen.py). Deployed as a temporary
# Railway service next to the target; `railway up` is run from this directory.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN pip install --no-cache-dir aiohttp==3.12.15
COPY loadgen.py mix.json ./

EXPOSE 8080
CMD ["python", "loadgen.py"]

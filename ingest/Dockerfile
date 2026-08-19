# Built from the REPO ROOT: the gateway authenticates against `control/`.
FROM python:3.11-slim

WORKDIR /app
COPY ingest/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY ingest /app/ingest/
COPY control /app/control/

ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "ingest.app:app", "--host", "0.0.0.0", "--port", "4319"]

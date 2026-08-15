FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/query/
RUN touch /app/query/__init__.py

ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "query.app:app", "--host", "0.0.0.0", "--port", "8080"]

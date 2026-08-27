FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements-kafka.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/requirements-kafka.txt

COPY . /app/normalizer/
RUN touch /app/normalizer/__init__.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "normalizer.consumer"]

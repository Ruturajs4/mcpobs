FROM python:3.11-slim

WORKDIR /app
COPY archiver/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY archiver /app/archiver/
RUN touch /app/archiver/__init__.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "archiver.archiver"]

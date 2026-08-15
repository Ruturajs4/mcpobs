# Built from the REPO ROOT (see docker-compose.yml), not from query/, because
# the service needs `control/` too -- authentication and the tables it queries
# are different databases but one deployable. A Dockerfile cannot COPY above its
# context, and the alternative (publishing `control` as a package) is ceremony
# for two services in one repo.
FROM python:3.11-slim

WORKDIR /app
COPY query/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY query /app/query/
COPY control /app/control/
RUN touch /app/query/__init__.py

ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "query.app:app", "--host", "0.0.0.0", "--port", "8080"]

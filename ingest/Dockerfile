# Built from the REPO ROOT: the gateway authenticates against `control/`.
# Not wired into any compose file in THIS repo (see ingest/Dockerfile.lite
# for that) -- it exists as the base recipe for multi-tenant deployments,
# which is the private ECC repo's job.
FROM python:3.11-slim

WORKDIR /app
COPY ingest/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY ingest /app/ingest/
COPY control /app/control/

# No mcpobs_control_plane entry point is registered here -- with nothing
# registered, control/resolve.py falls back to control/local.py's
# single-tenant LocalAuthenticator (see docs/decisions.md D180-D181). A
# deployment that wants real multi-tenancy runs the private ECC repo's own
# image, which builds FROM this one (or copies this same recipe) and adds
# its own `pip install` step registering the real ControlPlane.

ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "ingest.app:app", "--host", "0.0.0.0", "--port", "4319"]

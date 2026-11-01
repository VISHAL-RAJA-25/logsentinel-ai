# Image used to test patches. Built once WITH network access; patches are later
# tested in containers that have NO network access.
FROM python:3.13-slim
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" sqlalchemy pytest httpx ruff
WORKDIR /work

FROM python:3.12-slim

WORKDIR /workspace
COPY pyproject.toml /workspace/pyproject.toml
COPY src /workspace/src
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["python", "-m", "mi_agent.main"]

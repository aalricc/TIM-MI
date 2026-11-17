FROM python:3.12-slim

WORKDIR /target/app
COPY target/app /target/app
RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.32,<1" "pydantic>=2.10,<3"

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.12-slim
WORKDIR /target/last_resort
COPY target/last_resort /target/last_resort
RUN pip install --no-cache-dir fastapi uvicorn
EXPOSE 8070
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8070"]

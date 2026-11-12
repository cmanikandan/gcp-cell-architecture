FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir google-cloud-spanner==3.49.1

COPY app.py /app/app.py
COPY static /app/static
RUN useradd --create-home --uid 10001 celluser
USER celluser

ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["python3", "/app/app.py"]

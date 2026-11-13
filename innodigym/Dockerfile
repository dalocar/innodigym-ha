# Home Assistant builds this on the device from the add-on folder (no prebuilt image needed).
# To try it without Home Assistant, from the repository root: docker compose up --build
FROM python:3.12-alpine
WORKDIR /app
COPY app/ ./
# /data is the add-on's persistent volume (included in Home Assistant backups).
ENV INNODIGYM_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8787
CMD ["python", "server.py"]

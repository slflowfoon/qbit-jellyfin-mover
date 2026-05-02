FROM python:3.12-alpine

RUN apk add --no-cache rsync

WORKDIR /app
COPY src/ /app/src/

ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "qbit_jellyfin_mover"]


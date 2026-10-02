FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} app && useradd -u ${UID} -g ${GID} -m -s /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tgbackup ./tgbackup
COPY login.py initial_sync.py ./
RUN mkdir -p /app/data && chown app:app /app/data

USER app
VOLUME ["/app/data"]
CMD ["python", "-m", "tgbackup"]

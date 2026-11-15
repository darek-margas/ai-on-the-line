FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY phone_agent ./phone_agent
# The licence and the attribution term travel with every copy of the image.
COPY LICENSE NOTICE ./

# Run as an ordinary user. /data is where calls and recordings are kept.
RUN useradd --system --uid 10001 --home /app agent && mkdir -p /data && chown agent /data
USER agent

VOLUME /data
EXPOSE 8090 9092
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s     CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8090), 3)"
CMD ["python", "-m", "phone_agent"]

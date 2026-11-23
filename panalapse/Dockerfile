ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache ffmpeg tzdata

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY app /opt/panalapse/app
WORKDIR /opt/panalapse
ENV DATA_DIR=/data PORT=8099 PYTHONUNBUFFERED=1

CMD ["python3", "-m", "app.main"]

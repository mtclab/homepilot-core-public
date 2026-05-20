FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache openssh-client

COPY relay.py .

RUN pip install --no-cache-dir asyncssh

EXPOSE 50051

ENTRYPOINT ["python", "relay.py"]
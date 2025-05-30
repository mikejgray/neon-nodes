FROM python:3.9-slim

LABEL vendor=neon.ai \
    ai.neon.name="neon-node-websocket"

ENV OVOS_CONFIG_BASE_FOLDER=neon
ENV OVOS_CONFIG_FILENAME=neon.yaml
ENV OVOS_DEFAULT_CONFIG=/opt/neon/neon.yaml
ENV XDG_CONFIG_HOME=/config

RUN apt update && apt install -y swig gcc libpulse-dev portaudio19-dev wget pulseaudio
RUN mkdir /opt/neon && \
    wget https://github.com/OpenVoiceOS/precise-lite-models/raw/master/wakewords/en/hey_mycroft.tflite -O /opt/neon/hey_mycroft.tflite

COPY docker_overlay/ /

WORKDIR /app
COPY . /app

RUN pip install /app[voice-client,websocket-client]

CMD ["python3", "/app/neon_nodes/websocket_client.py"]
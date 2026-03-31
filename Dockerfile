FROM debian:trixie-slim
RUN apt update && apt install python3 python3-venv python3-pip -y

ADD https://github.com/ich777/miniassistant.git /app/miniassistant
RUN cd /app/miniassistant && export PIP_NO_CACHE_DIR=1 && ./install.sh && rm -rf /var/cache/apt/archives /var/lib/apt/lists/*

RUN ln -s /app/miniassistant/venv/bin/miniassistant /usr/local/bin/miniassistant

COPY --chmod=770 docker/entrypoint.sh /opt/scripts/start.sh
RUN chmod +x /opt/scripts/start.sh
ENTRYPOINT ["/opt/scripts/start.sh"]

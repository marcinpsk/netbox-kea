ARG FROM
FROM ${FROM}

ARG WHL_FILE
COPY ${WHL_FILE} /opt/netbox/dist/
RUN --mount=type=secret,id=host_ca,required=false \
    if [ -f "/usr/local/bin/uv" ]; then \
        if [ -f "/run/secrets/host_ca" ]; then export SSL_CERT_FILE=/run/secrets/host_ca; fi; \
        /usr/local/bin/uv pip install --native-tls /opt/netbox/dist/${WHL_FILE}; \
    else \
        /opt/netbox/venv/bin/pip install /opt/netbox/dist/${WHL_FILE}; \
    fi

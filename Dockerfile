# Relay / broker server only (it never sees screen contents: traffic is end-to-end encrypted).
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY teamlooker ./teamlooker
RUN pip install --no-cache-dir cryptography \
    && pip install --no-cache-dir --no-deps .
EXPOSE 7575
USER nobody
ENTRYPOINT ["teamlooker", "relay"]
CMD ["--bind", "0.0.0.0", "--port", "7575"]

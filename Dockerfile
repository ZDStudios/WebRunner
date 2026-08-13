# WebRunner — self-hosted job scheduler with a web dashboard.
#
#   docker build -t webrunner .
#   docker run -d --name webrunner --restart unless-stopped \
#       -p 8770:8770 -v webrunner-data:/data webrunner
#
# The container runs as root on purpose: its whole job is to execute commands
# and manage services on the host you point it at. Treat access to the
# dashboard as equivalent to a root shell and keep it off the public internet.

FROM python:3.12-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive

# Tools people actually reach for from a scheduled job. Keep this list tight —
# anything else is one `pip install` or `apt-get install` away in your own layer.
RUN apt-get update && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        wget \
        git \
        openssh-client \
        rsync \
        jq \
        unzip \
        tzdata \
        procps \
        iputils-ping \
        postgresql-client \
        default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI (no daemon) so the Docker block and Services page work when you
# mount the host socket with -v /var/run/docker.sock:/var/run/docker.sock
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64)  docker_arch='x86_64'  ;; \
      arm64)  docker_arch='aarch64' ;; \
      armhf)  docker_arch='armhf'   ;; \
      *)      docker_arch=''        ;; \
    esac; \
    if [ -n "$docker_arch" ]; then \
      curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-27.3.1.tgz" \
        -o /tmp/docker.tgz && \
      tar -xzf /tmp/docker.tgz -C /tmp && \
      mv /tmp/docker/docker /usr/local/bin/docker && \
      rm -rf /tmp/docker /tmp/docker.tgz; \
    else \
      echo "no static Docker CLI for $arch — the Docker block will be unavailable"; \
    fi

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY webrunner ./webrunner
RUN pip install --no-cache-dir --no-deps .

ENV WEBRUNNER_DATA_DIR=/data \
    WEBRUNNER_HOST=0.0.0.0 \
    WEBRUNNER_PORT=8770 \
    WEBRUNNER_SHELL=/bin/bash \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME ["/data"]
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8770/healthz || exit 1

ENTRYPOINT ["webrunner"]
CMD ["serve"]

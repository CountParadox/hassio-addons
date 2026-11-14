# The add-on is the official Bitfocus Companion image with a small start script.
# COMPANION_VERSION is bumped automatically by .github/workflows/update-companion.yml.
ARG COMPANION_VERSION=v5.1.3
FROM ghcr.io/bitfocus/companion/companion:${COMPANION_VERSION}

# Home Assistant mounts the add-on's config folder as root.
USER root

# nginx serves the Home Assistant ingress port (see ingress.conf).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx-light \
    && rm -rf /var/lib/apt/lists/*

COPY ingress.conf /etc/nginx/companion-ingress.conf.template
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Keep everything Companion stores in the add-on config folder (included in backups).
ENV COMPANION_CONFIG_BASEDIR=/config

ENTRYPOINT ["/run.sh"]

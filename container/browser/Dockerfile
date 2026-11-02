# Remote browser image (docs/container-runtime.md section 4).
# Built only by the Hub's admin-initiated setup, from this shipped context.
# The Chromium sandbox stays enabled: the container runs as an unprivileged
# user and relies on user namespaces. If the NAS kernel or Container Station
# blocks them, setup fails with a clear message instead of weakening Chromium.
FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      chromium xvfb socat fonts-noto-cjk ca-certificates dumb-init util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 1000 -m -d /home/browser -s /usr/sbin/nologin browser

COPY entrypoint.sh /usr/local/bin/browser-entrypoint.sh
RUN chmod 0755 /usr/local/bin/browser-entrypoint.sh

ENV HOME=/tmp DISPLAY=:99 LANG=C.UTF-8 \
    XDG_CONFIG_HOME=/tmp/.config XDG_CACHE_HOME=/tmp/.cache
USER 1000:1000
ENTRYPOINT ["dumb-init", "--", "/usr/local/bin/browser-entrypoint.sh"]

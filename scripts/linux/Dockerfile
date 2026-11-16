## Pinned Linux images for `scripts/linux/run.sh`: `main` (check, e2e, desktop, package suites),
## `compile` (the Nuitka build suite) and `oldgit` (the old-git regression suite, Debian 12's Git
## 2.39.5). Every base image is pinned by digest; every direct download is checked against its
## SHA-256; Ubuntu packages come from a fixed archive snapshot, Debian packages from
## snapshot.debian.org at a fixed time (bookworm's apt, 2.6.1, ignores `APT::Snapshot`).

FROM ghcr.io/astral-sh/uv:0.11.11@sha256:798712e57f879c5393777cbda2bb309b29fcdeb0532129d4b1c3125c5385975a AS uv

FROM buildpack-deps:noble-curl@sha256:8b9180fe497ffa61c196f329966942e4a1c3ba7a726a9bcab794783de8b64d22 AS main
ENV DEBIAN_FRONTEND=noninteractive
RUN printf 'APT::Snapshot "20261020T000000Z";\n' > /etc/apt/apt.conf.d/50snapshot \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      git unzip xz-utils xvfb xauth fonts-liberation fonts-dejavu-core fonts-noto-core \
      fonts-noto-color-emoji libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 \
      libatspi2.0-0t64 libcairo2 libcups2t64 libcurl4t64 libdbus-1-3 libexpat1 libgbm1 \
      libglib2.0-0t64 libgtk-3-0t64 libnspr4 libnss3 libpango-1.0-0 libudev1 libvulkan1 \
      libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
      libxrandr2 xdg-utils \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSLo /tmp/node.tar.xz https://nodejs.org/dist/v24.15.0/node-v24.15.0-linux-x64.tar.xz \
 && echo '472655581fb851559730c48763e0c9d3bc25975c59d518003fc0849d3e4ba0f6  /tmp/node.tar.xz' | sha256sum -c \
 && tar --no-same-owner -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
 && rm /tmp/node.tar.xz \
 && node --version
RUN curl -fsSLo /tmp/chrome.zip https://storage.googleapis.com/chrome-for-testing-public/156.0.8078.47/linux64/chrome-linux64.zip \
 && echo 'e6c94490f0b6b337225aeb2d2a5034858516b7017356980ba94f5176d8774a50  /tmp/chrome.zip' | sha256sum -c \
 && unzip -q /tmp/chrome.zip -d /opt/google \
 && mv /opt/google/chrome-linux64 /opt/google/chrome \
 && rm -f /tmp/chrome.zip \
 && ! ldd /opt/google/chrome/chrome | grep "not found"
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_DOWNLOADS=never
# go-w: uv makes its install lock 666, and uid 1000's uv only reads the installs.
RUN UV_PYTHON_DOWNLOADS=automatic uv python install 3.14.4 \
 && chmod -R a+rX,go-w /opt/uv-python
# fontconfig's postinst makes /usr/local/share/fonts root:staff 2775.
RUN chown root:root /usr/local/share/fonts && chmod g-ws /usr/local/share/fonts
# run.sh runs its tests as uid 1000, and /usr/local/bin leads root's PATH: uid 1000 must not be
# able to change anything here. The build fails on an entry under /usr/local or /opt that is not
# root:root, or that is world-writable (symlinks aside: their mode is always 777).
RUN bad="$(find /usr/local /opt \( ! -user root -o ! -group root -o ! -type l -perm -0002 \) -print)" \
 && if [ -n "$bad" ]; then printf 'not root:root, or world-writable:\n%s\n' "$bad" >&2; exit 1; fi
COPY run.sh /runner/run.sh

# The compile suite runs as root throughout (this image has no uid 1000), so the ownership check
# above stays in main: the base's own /opt/rh holds world-writable tmp folders.
FROM quay.io/pypa/manylinux_2_28_x86_64@sha256:c2261579b9c2e5d45aa93312f73e2a302182e3e977b558581a1838d6fed3d8e6 AS compile
RUN curl -fsSLo /tmp/node.tar.xz https://nodejs.org/dist/v24.15.0/node-v24.15.0-linux-x64.tar.xz \
 && echo '472655581fb851559730c48763e0c9d3bc25975c59d518003fc0849d3e4ba0f6  /tmp/node.tar.xz' | sha256sum -c \
 && tar --no-same-owner -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
 && rm /tmp/node.tar.xz \
 && node --version
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_DOWNLOADS=never
RUN UV_PYTHON_DOWNLOADS=automatic uv python install 3.14.4 \
 && chmod -R a+rX /opt/uv-python
COPY run.sh /runner/run.sh

# bookworm's apt ignores APT::Snapshot (see the header above); pin through snapshot.debian.org
# instead. Unlike noble's buildpack-deps, this base has no uid 1000 of its own, hence useradd.
FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8 AS oldgit
ENV DEBIAN_FRONTEND=noninteractive
RUN rm -rf /etc/apt/sources.list* \
 && mkdir -p /etc/apt/sources.list.d \
 && printf '%s\n' \
      'Types: deb' \
      'URIs: http://snapshot.debian.org/archive/debian/20261020T000000Z' \
      'Suites: bookworm bookworm-updates' \
      'Components: main' \
      'Check-Valid-Until: no' \
      '' \
      'Types: deb' \
      'URIs: http://snapshot.debian.org/archive/debian-security/20261020T000000Z' \
      'Suites: bookworm-security' \
      'Components: main' \
      'Check-Valid-Until: no' \
      > /etc/apt/sources.list.d/snapshot.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -u 1000 -m -d /home/ubuntu ubuntu
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_DOWNLOADS=never
RUN UV_PYTHON_DOWNLOADS=automatic uv python install 3.14.4 \
 && chmod -R a+rX,go-w /opt/uv-python
RUN bad="$(find /usr/local /opt \( ! -user root -o ! -group root -o ! -type l -perm -0002 \) -print)" \
 && if [ -n "$bad" ]; then printf 'not root:root, or world-writable:\n%s\n' "$bad" >&2; exit 1; fi
COPY run.sh /runner/run.sh

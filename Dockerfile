FROM python:3.13-slim-bookworm AS app-builder

RUN apt-get update \
    && apt-get install --yes --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyinstaller pycountry rich

WORKDIR /build
COPY bluray_remux.py .

RUN pyinstaller \
        --onefile \
        --strip \
        --clean \
        --name bluray_remux \
        bluray_remux.py \
    && install -Dm755 dist/bluray_remux /out/bluray_remux


FROM debian:bookworm-slim AS ffprobe-builder

ARG FFMPEG_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        nasm \
        pkg-config \
        xz-utils \
        yasm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp

RUN set -eux; \
    version="${FFMPEG_VERSION}"; \
    if [ "${version}" = "latest" ]; then \
        version="$(curl -fsSL https://www.ffmpeg.org/download.html \
            | grep -oP 'releases/ffmpeg-\K[0-9]+(\.[0-9]+)*(?=\.tar\.xz)' \
            | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"; \
    fi; \
    curl -fsSLo ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${version}.tar.xz"; \
    tar -xJf ffmpeg.tar.xz; \
    mv "ffmpeg-${version}" /tmp/ffmpeg-src

WORKDIR /tmp/ffmpeg-src

RUN ./configure \
        --prefix=/opt/ffprobe \
        --disable-debug \
        --disable-doc \
        --disable-ffmpeg \
        --disable-ffplay \
        --enable-ffprobe \
        --disable-network \
        --disable-shared \
        --enable-static \
    && make -j"$(nproc)" ffprobe \
    && install -Dm755 ffprobe /opt/ffprobe/bin/ffprobe \
    && strip --strip-unneeded /opt/ffprobe/bin/ffprobe


FROM debian:bookworm-slim AS mkvmerge-builder

ARG MKVTOOLNIX_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        binutils \
        ca-certificates \
        curl \
        gpg \
    && rm -rf /var/lib/apt/lists/*

RUN install -d /etc/apt/keyrings \
    && curl -fsSL "https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg" \
        -o /etc/apt/keyrings/gpg-pub-moritzbunkus.gpg \
    && printf '%s\n' \
        "deb [signed-by=/etc/apt/keyrings/gpg-pub-moritzbunkus.gpg] https://mkvtoolnix.download/debian/ bookworm main" \
        > /etc/apt/sources.list.d/mkvtoolnix.list

RUN set -eux; \
    apt-get update; \
    version="${MKVTOOLNIX_VERSION}"; \
    if [ "${version}" = "latest" ]; then \
        deb_version="$(apt-cache madison mkvtoolnix | awk 'NR==1 {print $3}')"; \
    else \
        deb_version="$(apt-cache madison mkvtoolnix | awk -v version="${version}" 'index($3, version "-") == 1 {print $3; exit}')"; \
    fi; \
    test -n "${deb_version}"; \
    apt-get install --yes --no-install-recommends "mkvtoolnix=${deb_version}"; \
    rm -rf /var/lib/apt/lists/*

RUN install -d /opt/mkvmerge/bin /opt/mkvmerge/lib \
    && cp /usr/bin/mkvmerge /opt/mkvmerge/bin/mkvmerge \
    && ldd /usr/bin/mkvmerge \
        | awk '/=> \// {print $3} /^\// {print $1}' \
        | sort -u \
        | while read -r lib; do \
            base="$(basename "$lib")"; \
            case "$base" in \
                libc.so*|libm.so*|libdl.so*|libpthread.so*|librt.so*|libstdc++.so*|libgcc_s.so*|ld-linux*) continue ;; \
            esac; \
            real="$(realpath "$lib")"; \
            real_base="$(basename "$real")"; \
            cp -L "$real" "/opt/mkvmerge/lib/$real_base"; \
            if [ "$base" != "$real_base" ] && [ ! -e "/opt/mkvmerge/lib/$base" ]; then \
                ln -s "$real_base" "/opt/mkvmerge/lib/$base"; \
            fi; \
        done \
    && strip --strip-unneeded /opt/mkvmerge/bin/mkvmerge \
    && find /opt/mkvmerge/lib -type f -exec strip --strip-unneeded '{}' + || true


FROM debian:bookworm-slim

ENV LANG=C.UTF-8 \
    LD_LIBRARY_PATH=/opt/mkvmerge/lib

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=app-builder /out/bluray_remux /usr/local/bin/bluray_remux
COPY --from=ffprobe-builder /opt/ffprobe/bin/ffprobe /usr/local/bin/ffprobe
COPY --from=mkvmerge-builder /opt/mkvmerge/bin/mkvmerge /usr/local/bin/mkvmerge
COPY --from=mkvmerge-builder /opt/mkvmerge/lib/ /opt/mkvmerge/lib/

ENTRYPOINT ["bluray_remux"]

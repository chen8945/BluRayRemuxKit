FROM python:3.13-slim-bookworm AS app-builder

RUN apt-get update \
    && apt-get install --yes --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyinstaller

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bluray_remux.py .

RUN pyinstaller \
        --onefile \
        --strip \
        --clean \
        --name bluray_remux \
        bluray_remux.py \
    && install -Dm755 dist/bluray_remux /out/bluray_remux


FROM debian:bookworm-slim AS ffmpeg-source

ARG FFMPEG_VERSION=latest
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp

RUN set -eux; \
    version="${FFMPEG_VERSION}"; \
    if [ "${version}" = "latest" ]; then \
        version="$(curl -fsSL https://www.ffmpeg.org/download.html \
            | grep -oP 'releases/ffmpeg-\K[0-9]+(\.[0-9]+)*(?=\.tar\.xz)' \
            | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"; \
    fi; \
    test -n "${version}"; \
    curl -fsSLo ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${version}.tar.xz"; \
    mkdir -p /tmp/ffmpeg-src; \
    tar -xJf ffmpeg.tar.xz --strip-components=1 -C /tmp/ffmpeg-src


FROM debian:bookworm-slim AS ffprobe-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        nasm \
        pkg-config \
        yasm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg-source /tmp/ffmpeg-src /tmp/ffmpeg-src
WORKDIR /tmp/ffmpeg-src

RUN ./configure \
        --prefix=/opt/ffprobe \
        --disable-debug \
        --disable-doc \
        --disable-autodetect \
        --disable-network \
        --disable-programs \
        --enable-ffprobe \
        --disable-everything \
        --enable-protocol=file,pipe \
        --enable-demuxer=mpegts,mpegtsraw,mov,matroska \
        --enable-parser=hevc,h264,vc1,aac,ac3,dca,mlp,mpegvideo,mpeg4video \
        --enable-decoder=hevc,h264,vc1,aac,ac3,eac3,truehd,mlp,dca,flac,pcm_bluray,hdmv_pgs_subtitle \
        --disable-shared \
        --enable-static \
    && make -j"$(nproc)" ffprobe \
    && install -Dm755 ffprobe /opt/ffprobe/bin/ffprobe \
    && strip --strip-unneeded /opt/ffprobe/bin/ffprobe


FROM debian:bookworm-slim AS ffmpeg-static-builder

ARG FDK_AAC_VERSION=2.0.3
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        autoconf \
        automake \
        build-essential \
        ca-certificates \
        curl \
        libtool \
        make \
        nasm \
        pkg-config \
        yasm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg-source /tmp/ffmpeg-src /tmp/ffmpeg-src

RUN set -eux; \
    mkdir -p /tmp/fdk-aac-src; \
    curl -fsSL "https://github.com/mstorsjo/fdk-aac/archive/v${FDK_AAC_VERSION}.tar.gz" \
        | tar -xz --strip-components=1 -C /tmp/fdk-aac-src; \
    cd /tmp/fdk-aac-src; \
    autoreconf -fiv; \
    ./configure \
        --prefix=/opt/ffmpeg-static \
        --enable-static \
        --disable-shared \
        --with-pic; \
    make -j"$(nproc)"; \
    make install; \
    cd /tmp/ffmpeg-src; \
    PKG_CONFIG_PATH=/opt/ffmpeg-static/lib/pkgconfig ./configure \
        --prefix=/opt/ffmpeg-static \
        --pkg-config-flags=--static \
        --disable-doc \
        --disable-network \
        --disable-programs \
        --enable-pic \
        --enable-static \
        --disable-shared \
        --enable-libfdk-aac; \
    make -j"$(nproc)"; \
    make install


FROM debian:bookworm-slim AS mkvmerge-builder

ARG MKVTOOLNIX_VERSION=latest
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        binutils \
        ca-certificates \
        curl \
        gpg \
        patchelf \
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
    && interp_path="$(patchelf --print-interpreter /usr/bin/mkvmerge)" \
    && interp_base="$(basename "${interp_path}")" \
    && cp -L "${interp_path}" "/opt/mkvmerge/lib/${interp_base}" \
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
    && find /opt/mkvmerge/bin -maxdepth 1 -type f -executable \
        -exec patchelf --set-interpreter "/opt/mkvmerge/lib/${interp_base}" '{}' ';' \
        -exec patchelf --set-rpath '$ORIGIN/../lib' '{}' ';' \
    && find /opt/mkvmerge/lib -maxdepth 1 -type f -name 'lib*.so*' \
        -exec patchelf --set-rpath '$ORIGIN' '{}' ';' \
    && strip --strip-unneeded /opt/mkvmerge/bin/mkvmerge \
    && find /opt/mkvmerge/lib -type f -exec strip --strip-unneeded '{}' + || true


FROM debian:bookworm-slim AS makemkv-builder
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        file \
        libc6-dev \
        libexpat1-dev \
        libcurl4 \
        libssl-dev \
        make \
        patchelf \
        patch \
        pkg-config \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY docker/fetch_makemkv_beta_key.sh /usr/local/bin/fetch_makemkv_beta_key.sh
COPY docker/makemkv/default.mmcp.xml /src/docker/makemkv/default.mmcp.xml
COPY docker/makemkv/settings.conf /src/docker/makemkv/settings.conf
COPY docker/makemkv-oss-*.tar.gz /src/docker/
COPY docker/makemkv-bin-*.tar.gz /src/docker/
COPY --from=ffmpeg-static-builder /opt/ffmpeg-static /opt/ffmpeg-static
RUN chmod +x /usr/local/bin/fetch_makemkv_beta_key.sh

RUN set -eux; \
    version="$(find /src/docker -maxdepth 1 -type f -name 'makemkv-oss-*.tar.gz' \
        | sed -E 's|.*/makemkv-oss-([0-9]+(\.[0-9]+){2})\.tar\.gz$|\1|' \
        | sort -V \
        | tail -1)"; \
    if [ -z "$version" ]; then \
        version="$(curl -fsSL https://www.makemkv.com/download/ \
            | grep -Eo 'Setup_MakeMKV_v[0-9]+(\.[0-9]+){2}\.exe' \
            | head -n 1 \
            | sed -E 's/^Setup_MakeMKV_v([0-9]+(\.[0-9]+){2})\.exe$/\1/')"; \
    fi; \
    test -n "$version"; \
    mkdir -p /tmp/makemkv-src; \
    cd /tmp/makemkv-src; \
    if [ -f "/src/docker/makemkv-oss-${version}.tar.gz" ] && [ -f "/src/docker/makemkv-bin-${version}.tar.gz" ]; then \
        cp "/src/docker/makemkv-oss-${version}.tar.gz" makemkv-oss.tar.gz; \
        cp "/src/docker/makemkv-bin-${version}.tar.gz" makemkv-bin.tar.gz; \
    else \
        curl -fsSLo makemkv-oss.tar.gz "https://www.makemkv.com/download/makemkv-oss-${version}.tar.gz"; \
        curl -fsSLo makemkv-bin.tar.gz "https://www.makemkv.com/download/makemkv-bin-${version}.tar.gz"; \
    fi; \
    tar -xzf makemkv-oss.tar.gz; \
    tar -xzf makemkv-bin.tar.gz; \
    cd "/tmp/makemkv-src/makemkv-oss-${version}"; \
    PKG_CONFIG="pkg-config --static" \
    PKG_CONFIG_PATH=/opt/ffmpeg-static/lib/pkgconfig \
    OBJCOPY=objcopy \
    ./configure --prefix=/opt/makemkv --disable-gui; \
    make -j"$(nproc)"; \
    make install; \
    cd "/tmp/makemkv-src/makemkv-bin-${version}"; \
    mkdir -p tmp; \
    touch tmp/eula_accepted; \
    make -j"$(nproc)" PREFIX=/opt/makemkv; \
    make install PREFIX=/opt/makemkv; \
    install -d /opt/makemkv/conf /opt/makemkv/lib; \
    cp /src/docker/makemkv/default.mmcp.xml /opt/makemkv/default.mmcp.xml; \
    key="$(/usr/local/bin/fetch_makemkv_beta_key.sh)"; \
    sed "s|__MAKE_MKV_BETA_KEY__|$key|g" /src/docker/makemkv/settings.conf > /opt/makemkv/conf/settings.conf; \
    curl_lib="$(ldconfig -p | awk '/libcurl\.so\.4/{print $NF; exit}')"; \
    if [ -z "${curl_lib}" ]; then \
        echo "Unable to locate libcurl.so.4 on build system"; \
        exit 1; \
    fi; \
    base="$(basename "${curl_lib}")"; \
    real="$(realpath "${curl_lib}")"; \
    real_base="$(basename "${real}")"; \
    cp -L "${real}" "/opt/makemkv/lib/${real_base}"; \
    if [ "${base}" != "${real_base}" ] && [ ! -e "/opt/makemkv/lib/${base}" ]; then \
        ln -s "${real_base}" "/opt/makemkv/lib/${base}"; \
    fi; \
    : > /tmp/makemkv-ldd.txt; \
    { \
        find /opt/makemkv/bin -maxdepth 1 -type f -executable; \
        find /opt/makemkv/lib -maxdepth 1 -type f -name '*.so*'; \
    } | while read -r target; do \
        if ! file "$target" | grep -q 'ELF '; then \
            continue; \
        fi; \
        if ! LD_LIBRARY_PATH=/opt/makemkv/lib ldd "$target" >> /tmp/makemkv-ldd.txt 2>&1; then \
            echo "ldd failed for $target"; \
            file "$target"; \
            exit 1; \
        fi; \
    done; \
    if grep -q 'not found' /tmp/makemkv-ldd.txt; then \
        cat /tmp/makemkv-ldd.txt; \
        exit 1; \
    fi; \
    awk '/=> \// {print $3} /^\// {print $1}' /tmp/makemkv-ldd.txt \
        | sort -u \
        | while read -r lib; do \
            base="$(basename "$lib")"; \
            case "$base" in \
                libc.so*|libm.so*|libdl.so*|libpthread.so*|librt.so*|ld-linux*|ld-musl*) continue ;; \
            esac; \
            real="$(realpath "$lib")"; \
            case "$real" in \
                /opt/makemkv/*) continue ;; \
            esac; \
            real_base="$(basename "$real")"; \
            cp -L "$real" "/opt/makemkv/lib/$real_base"; \
            if [ "$base" != "$real_base" ] && [ ! -e "/opt/makemkv/lib/$base" ]; then \
                ln -s "$real_base" "/opt/makemkv/lib/$base"; \
            fi; \
        done; \
    interp_path="$(patchelf --print-interpreter /opt/makemkv/bin/makemkvcon)"; \
    interp_base="$(basename "${interp_path}")"; \
    cp -L "${interp_path}" "/opt/makemkv/lib/${interp_base}"; \
    find /opt/makemkv/bin -maxdepth 1 -type f -executable \
        -exec patchelf --set-interpreter "/opt/makemkv/lib/${interp_base}" '{}' ';' \
        -exec patchelf --set-rpath '$ORIGIN/../lib' '{}' ';'; \
    find /opt/makemkv/lib -maxdepth 1 -type f -name 'lib*.so*' \
        -exec patchelf --set-rpath '$ORIGIN' '{}' ';'; \
    rm -rf /opt/makemkv/include; \
    if [ -d /opt/makemkv/share ]; then \
        find /opt/makemkv/share -mindepth 1 -maxdepth 1 ! -name 'MakeMKV' -exec rm -rf '{}' +; \
    fi; \
    if [ -d /opt/makemkv/share/MakeMKV ]; then \
        find /opt/makemkv/share/MakeMKV -mindepth 1 -maxdepth 1 \
            ! -name 'appdata.tar' \
            ! -name 'blues.jar' \
            ! -name 'blues.policy' \
            -exec rm -rf '{}' +; \
    fi; \
    if [ "$(find /opt/makemkv/lib -maxdepth 1 -type f | wc -l)" -le 10 ]; then \
        echo "MakeMKV runtime libs appear incomplete:"; \
        ls -la /opt/makemkv/lib; \
        exit 1; \
    fi; \
    strip --strip-unneeded /opt/makemkv/bin/makemkvcon || true; \
    find /opt/makemkv/lib -type f -name '*.so*' -exec strip --strip-unneeded '{}' + || true; \
    rm -f /tmp/makemkv-ldd.txt; \
    rm -rf /tmp/makemkv-src


FROM debian:bookworm-slim

ENV LANG=C.UTF-8 \
    MAKEMKV_PROFILE=/opt/makemkv/default.mmcp.xml \
    LD_LIBRARY_PATH=/opt/mkvmerge/lib:/opt/makemkv/lib

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=app-builder /out/bluray_remux /usr/local/bin/bluray_remux
COPY --from=ffprobe-builder /opt/ffprobe/bin/ffprobe /usr/local/bin/ffprobe
COPY --from=mkvmerge-builder /opt/mkvmerge/bin/mkvmerge /usr/local/bin/mkvmerge
COPY --from=mkvmerge-builder /opt/mkvmerge/lib/ /opt/mkvmerge/lib/
COPY --from=makemkv-builder /opt/makemkv/ /opt/makemkv/
COPY docker/fetch_makemkv_beta_key.sh /usr/local/bin/fetch_makemkv_beta_key.sh
COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/fetch_makemkv_beta_key.sh /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /root/.MakeMKV \
    && ln -sf /opt/makemkv/bin/makemkvcon /usr/local/bin/makemkvcon \
    && ln -sf /opt/makemkv/conf/settings.conf /root/.MakeMKV/settings.conf \
    && ln -sf /opt/makemkv/default.mmcp.xml /usr/local/bin/default.mmcp.xml

ENTRYPOINT ["docker-entrypoint.sh"]

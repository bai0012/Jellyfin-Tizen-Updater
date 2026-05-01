FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TIZEN_STUDIO=/home/tizen/tizen-studio
ENV PATH=/home/tizen/tizen-studio/tools:/home/tizen/tizen-studio/tools/ide/bin:$PATH

RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    unzip \
    xz-utils \
    python3-full \
    python3-pip \
    openjdk-11-jre \
    libglib2.0-0 \
    libgtk-3-0 \
    libnss3 \
    libx11-6 \
    libxext6 \
    libxi6 \
    libxtst6 \
    file \
    findutils \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install requests --break-system-packages

RUN useradd -m -s /bin/bash tizen \
    && mkdir -p /app /data \
    && chown -R tizen:tizen /app /data

USER tizen
WORKDIR /home/tizen

RUN curl -L -o web-cli_Tizen_Studio_6.1_ubuntu-64.bin \
    https://download.tizen.org/sdk/Installer/tizen-studio_6.1/web-cli_Tizen_Studio_6.1_ubuntu-64.bin \
    && chmod +x web-cli_Tizen_Studio_6.1_ubuntu-64.bin \
    && ./web-cli_Tizen_Studio_6.1_ubuntu-64.bin --accept-license ${TIZEN_STUDIO} \
    && rm web-cli_Tizen_Studio_6.1_ubuntu-64.bin \
    && echo "=== Installed tree ===" \
    && find ${TIZEN_STUDIO} -maxdepth 3 -type f | head -80 \
    && test -x ${TIZEN_STUDIO}/package-manager/package-manager-cli.bin

RUN ${TIZEN_STUDIO}/package-manager/package-manager-cli.bin show-pkgs | tee /tmp/tizen-pkgs.txt \
    && grep -Ei "TOOLS|WebCLI|SDK tools|sdb|TV" /tmp/tizen-pkgs.txt || true \
    && ${TIZEN_STUDIO}/package-manager/package-manager-cli.bin install --accept-license --no-java-check TOOLS WebCLI \
    && test -x ${TIZEN_STUDIO}/tools/sdb \
    && test -x ${TIZEN_STUDIO}/tools/ide/bin/tizen

WORKDIR /app

COPY --chown=tizen:tizen jellyfin_tizen_auto_updater.py /app/jellyfin_tizen_auto_updater.py

CMD ["python3", "/app/jellyfin_tizen_auto_updater.py"]
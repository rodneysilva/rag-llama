# ═══ SANDBOX de execução — testa os códigos exibidos no chat ═══
# Container SEMPRE ATIVO (compose `sandbox`) com as principais linguagens:
# python, node, java (17), dotnet (8), rust, ruby, php, go e dart/flutter.
# Sem portas publicadas: só a rede interna do compose; agent.py (stdlib)
# executa comandos sob o usuário não-root `sandbox` em /tmp/work.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive HOME=/tmp/home

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-tk python3-pip python3-venv \
      ca-certificates curl git unzip xz-utils build-essential \
      openjdk-17-jdk-headless ruby-full php-cli golang-go nodejs npm \
      dotnet-sdk-8.0 xvfb \
    && rm -rf /var/lib/apt/lists/*

# ALIASES que o mundo usa (Ubuntu 24.04 só tem python3/pip3): scripts de
# teste gerados no chat chamam `python`/`pip` direto — sem os links, todo
# `bash x.sh` morria em "command not found". PEP 668 liberado p/ o teste.
RUN ln -sf /usr/bin/python3 /usr/local/bin/python \
    && ln -sf /usr/bin/pip3 /usr/local/bin/pip
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# .NET 10 (pedido do dono): SDK + runtime ASP.NET Core via instalador
# oficial em /usr/share/dotnet10 com symlink ANTES do apt no PATH. Os
# SHARED FRAMEWORKS do .NET 8 do apt são LIGADOS no MESMO root: um único
# DOTNET_ROOT resolve net8.0 E net10.0 (sem isso o app net8.0 compilava
# e morria em 'You must install .NET to run this application').
RUN curl -sSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && bash /tmp/dotnet-install.sh --channel 10.0 --install-dir /usr/share/dotnet10 \
    && bash /tmp/dotnet-install.sh --channel 10.0 --runtime aspnetcore --install-dir /usr/share/dotnet10 \
    && for fw in /usr/lib/dotnet/shared/Microsoft.NETCore.App/* /usr/lib/dotnet/shared/Microsoft.AspNetCore.App/*; do \
           [ -d "$fw" ] || continue; \
           mkdir -p "/usr/share/dotnet10/shared/$(basename "$(dirname "$fw")")"; \
           ln -sfn "$fw" "/usr/share/dotnet10/shared/$(basename "$(dirname "$fw")")/$(basename "$fw")"; \
       done \
    && ln -sf /usr/share/dotnet10/dotnet /usr/local/bin/dotnet \
    && rm /tmp/dotnet-install.sh \
    && chmod -R a+rX /usr/share/dotnet10
ENV DOTNET_ROOT=/usr/share/dotnet10

# Rust compartilhado em /opt (read-only p/ o usuário de execução; o cargo
# do usuário cacheia registro em $CARGO_HOME=/tmp/home/.cargo)
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path \
    && chmod -R a+rX /opt/rustup /opt/cargo

# Dart/Flutter (dart run p/ scripts; flutter para projetos).
# git safe.directory no gitconfig de SISTEMA (vale para o usuário sandbox —
# o tarball do flutter é git e o dart CLI o consulta).
RUN curl -fsSL https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.24.5-stable.tar.xz \
        -o /tmp/fl.tar.xz \
    && mkdir -p /opt && tar -xJf /tmp/fl.tar.xz -C /opt && rm /tmp/fl.tar.xz \
    && git config --system --add safe.directory '*' || true

ENV PATH="/opt/cargo/bin:/opt/flutter/bin:${PATH}"

RUN useradd -m -u 2000 sandbox \
    && mkdir -p /tmp/home /tmp/work \
    && chown -R sandbox:sandbox /tmp/home /tmp/work
USER sandbox
WORKDIR /tmp/work
COPY agent.py /agent.py
EXPOSE 8020
CMD ["python3", "/agent.py"]

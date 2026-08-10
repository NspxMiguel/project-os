#!/usr/bin/env bash
#
# project-os installer.
#
#   curl -fsSL https://raw.githubusercontent.com/NspxMiguel/project-os/main/install.sh | bash
#
# Installs project-os natively: a virtualenv under /opt/project-os and a systemd
# unit. Not a container. project-os is the layer that runs your things -- it does
# not itself live inside one.
#
# The script is written to be run twice. Every step checks before it acts, so a
# re-run upgrades instead of exploding, and a run that fails halfway can simply
# be run again.

set -euo pipefail

REPO="${PROJECT_OS_REPO:-https://github.com/NspxMiguel/project-os.git}"
BRANCH="${PROJECT_OS_BRANCH:-main}"
PREFIX="${PROJECT_OS_PREFIX:-/opt/project-os}"
STATE="${PROJECT_OS_HOME:-/var/lib/project-os}"
SERVICE_USER="${PROJECT_OS_USER:-project-os}"
PORT="${PROJECT_OS_PORT:-8099}"
# Empty means "decide by how much RAM this board has" -- see choose_extras.
EXTRAS="${PROJECT_OS_EXTRAS:-}"

UNIT=/etc/systemd/system/project-os.service

# Progress goes to stderr so that a function can both narrate and return a value
# on stdout -- check_memory does exactly that.
say()  { printf '\n\033[1m==> %s\033[0m\n' "$*" >&2; }
note() { printf '    %s\n' "$*" >&2; }
die()  { printf '\n\033[31merro: %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- checks

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        # sudo rather than a bare failure: most people paste this into a normal
        # shell and the only thing missing is four characters.
        die "rode como root:  curl -fsSL <url> | sudo bash"
    fi
}

check_os() {
    [ "$(uname -s)" = "Linux" ] || die "o instalador é para Linux (Raspberry Pi OS, Debian, Ubuntu)"
    command -v systemctl >/dev/null 2>&1 || die "sem systemd -- instale à mão, veja docs/INSTALL.md"
}

# The user's rule: every Pi except the tiny ones. A 512 MB board boots and then
# dies the first time something touches the network, which is worse than a clear
# refusal now.
check_memory() {
    local kb mb
    kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
    mb=$(( kb / 1024 ))
    if [ "$mb" -lt 700 ]; then
        die "$mb MB de RAM. project-os pede pelo menos 1 GB -- para placas menores veja docs/SIMPLE-PROJECT-OS.md"
    fi
    note "memória: ${mb} MB"
    echo "$mb"
}

check_python() {
    local version
    command -v python3 >/dev/null 2>&1 || die "python3 não encontrado"
    version=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    python3 - <<'PY' || die "project-os precisa de Python 3.9 ou mais novo"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
    note "python: $version"
}

# --------------------------------------------------------------------------- install

install_packages() {
    say "Pacotes do sistema"
    export DEBIAN_FRONTEND=noninteractive
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq python3 python3-venv python3-dev git build-essential
        # A Pi sem HDMI só é encontrada pelo nome: sem o Avahi, "raspberrypi.local"
        # não resolve e o único jeito de achar o aparelho é caçar IP no roteador.
        # Numa instalação headless isso não é opcional.
        apt-get install -y -qq avahi-daemon avahi-utils || \
            note "avahi não instalou -- o acesso por .local pode não funcionar"
        systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
    else
        note "sem apt -- garanta python3-venv, git e um compilador C por conta própria"
    fi
}

# Which optional extras to install. The heavy ones (pyatv, PyChromecast) pull in
# cryptography and protobuf, which on a 1 GB Pi 3B is a twenty-minute compile and
# a real chunk of RAM at runtime. Everything here is optional by design: without
# them project-os still boots and each screen says what is missing.
choose_extras() {
    local mb="$1"
    if [ -n "$EXTRAS" ]; then
        echo "$EXTRAS"
    elif [ "$mb" -ge 1800 ]; then
        echo "all"
    else
        echo "system"
    fi
}

make_user() {
    say "Usuário do serviço"
    if id "$SERVICE_USER" >/dev/null 2>&1; then
        note "$SERVICE_USER já existe"
    else
        useradd --system --home-dir "$STATE" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
        note "criado $SERVICE_USER"
    fi
    # video: temperature and fan control on a Pi go through /dev/vchiq and the
    # hwmon nodes, and neither is world-writable.
    for group in video gpio i2c spi; do
        getent group "$group" >/dev/null 2>&1 && usermod -aG "$group" "$SERVICE_USER" || true
    done
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE"
}

fetch_source() {
    say "Código"
    if [ -d "$PREFIX/.git" ]; then
        note "atualizando $PREFIX"
        git -C "$PREFIX" fetch --quiet origin "$BRANCH"
        git -C "$PREFIX" reset --quiet --hard "origin/$BRANCH"
    elif [ -f "$(dirname "$0")/pyproject.toml" ]; then
        # Running from a clone the person already made. Copying beats cloning
        # again: whatever they edited is what they wanted installed.
        note "copiando de $(cd "$(dirname "$0")" && pwd)"
        mkdir -p "$PREFIX"
        cp -a "$(dirname "$0")/." "$PREFIX/"
    else
        note "clonando $REPO"
        git clone --quiet --depth 1 --branch "$BRANCH" "$REPO" "$PREFIX"
    fi
}

make_venv() {
    local extras="$1"
    say "Ambiente Python (extras: $extras)"
    [ -x "$PREFIX/.venv/bin/python3" ] || python3 -m venv "$PREFIX/.venv"
    "$PREFIX/.venv/bin/pip" install --quiet --upgrade pip wheel
    if [ "$extras" = "none" ]; then
        "$PREFIX/.venv/bin/pip" install --quiet "$PREFIX"
    else
        note "isto demora na primeira vez -- há coisa a compilar"
        "$PREFIX/.venv/bin/pip" install --quiet "$PREFIX[$extras]"
    fi
    # Not an extra and not optional: FastAPI refuses to build the upload route
    # without it, so the file manager would fail at import time.
    "$PREFIX/.venv/bin/pip" install --quiet python-multipart
    chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"
}

write_unit() {
    say "Serviço do systemd"
    cat > "$UNIT" <<UNITFILE
[Unit]
Description=project-os
Documentation=https://github.com/NspxMiguel/project-os
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
Environment=PROJECT_OS_HOME=$STATE
WorkingDirectory=$PREFIX
ExecStart=$PREFIX/bin/project-os --port $PORT
Restart=on-failure
RestartSec=5

# It is a Pi. A leak in an app should cost the app, not the SD card's swap.
MemoryMax=70%

# Hardening, kept deliberately loose in one place: project-os manages the machine
# it runs on, so ProtectSystem=strict would break the thing it exists to do.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=read-only
ReadWritePaths=$STATE

[Install]
WantedBy=multi-user.target
UNITFILE
    systemctl daemon-reload
    systemctl enable --quiet project-os.service
    systemctl restart project-os.service
}

wait_for_boot() {
    say "Subindo"
    local url="http://127.0.0.1:$PORT/api/system/health"
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
            note "no ar"
            return 0
        fi
        sleep 2
    done
    printf '\n'
    note "não respondeu em 60s. O que aconteceu:"
    journalctl -u project-os -n 30 --no-pager || true
    return 1
}

finish() {
    local host mdns
    host=$(hostname)
    if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
        mdns="ativo"
    else
        mdns="INATIVO -- use o IP abaixo"
    fi
    cat <<DONE

  project-os instalado.

    http://$host.local:$PORT   (mDNS: $mdns)
    http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT

  Abra no navegador e crie a primeira conta -- até lá o sistema não
  responde a mais nada, de propósito.

  Ele vem vazio. Os aplicativos ficam na loja, dentro da própria tela.

    systemctl status project-os
    journalctl -u project-os -f
    $PREFIX/bin/project-os --help

DONE
}

main() {
    need_root
    check_os
    check_python
    local mb extras
    mb=$(check_memory)
    extras=$(choose_extras "$mb")

    install_packages
    make_user
    fetch_source
    make_venv "$extras"
    write_unit
    wait_for_boot || die "o serviço não subiu -- o log acima diz por quê"
    finish
}

main "$@"

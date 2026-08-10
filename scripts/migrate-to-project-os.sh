#!/usr/bin/env bash
#
# Move an old ProjectOS install (0.1.x) onto the project-os layout, in place.
#
#   sudo bash migrate-to-project-os.sh
#
# Why this exists, said plainly: the 0.1.0 unit ran `python -m projectos` from
# /opt/projectos, and an update replaces the tree but cannot replace the unit.
# The rename therefore could not be delivered as an update -- only as a new card.
#
#   "nao quero ter q ficar fleshando nao, tem q ter alguma forma de atualizar
#    direto de la"
#
# Right. So this runs once, over SSH, and afterwards nothing like it is needed
# again: the new unit points at /opt/project-os/bin/project-os, a script that
# lives *inside* the tree, so every future change to how it starts arrives as an
# ordinary update.
#
# It moves rather than reinstalls: the database, the config, the media and the
# downloads are the parts nobody wants to lose.

set -euo pipefail

OLD_PREFIX=${OLD_PREFIX:-/opt/projectos}
NEW_PREFIX=${NEW_PREFIX:-/opt/project-os}
OLD_STATE=${OLD_STATE:-/var/lib/projectos}
NEW_STATE=${NEW_STATE:-/var/lib/project-os}
OLD_UNIT=/etc/systemd/system/projectos.service
NEW_UNIT=/etc/systemd/system/project-os.service
OLD_USER=${OLD_USER:-projectos}
NEW_USER=${NEW_USER:-project-os}
PORT=${PORT:-80}

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31merro: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "rode com sudo"

if [ -d "$NEW_PREFIX" ] && [ ! -d "$OLD_PREFIX" ]; then
    note "já está no layout novo, nada a fazer"
    exit 0
fi
[ -d "$OLD_PREFIX" ] || die "não achei $OLD_PREFIX"

say "Parando o serviço"
systemctl stop projectos.service 2>/dev/null || true
systemctl disable projectos.service 2>/dev/null || true

say "Movendo código e dados"
[ -d "$NEW_PREFIX" ] || mv "$OLD_PREFIX" "$NEW_PREFIX"
if [ -d "$OLD_STATE" ] && [ ! -d "$NEW_STATE" ]; then
    mv "$OLD_STATE" "$NEW_STATE"
fi
note "$NEW_PREFIX e $NEW_STATE"

say "Usuário"
if id "$NEW_USER" >/dev/null 2>&1; then
    note "$NEW_USER já existe"
elif id "$OLD_USER" >/dev/null 2>&1; then
    # Renaming keeps the uid, so every file already on the card stays owned by
    # the right account without a recursive chown of the media directory.
    usermod -l "$NEW_USER" "$OLD_USER"
    groupmod -n "$NEW_USER" "$OLD_USER" 2>/dev/null || true
    usermod -d "$NEW_STATE" "$NEW_USER" 2>/dev/null || true
    note "$OLD_USER renomeado para $NEW_USER"
else
    adduser --system --group --home "$NEW_STATE" --shell /usr/sbin/nologin "$NEW_USER"
fi
chown -R "$NEW_USER:$NEW_USER" "$NEW_PREFIX" "$NEW_STATE"

say "Ponto de entrada estável"
# The tree being moved may predate bin/project-os, so write it if missing. From
# here on the unit never names anything inside the tree again.
if [ ! -x "$NEW_PREFIX/bin/project-os" ]; then
    install -d -o "$NEW_USER" -g "$NEW_USER" "$NEW_PREFIX/bin"
    cat > "$NEW_PREFIX/bin/project-os" <<'WRAPPER'
#!/bin/sh
set -e
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -x "$here/.venv/bin/python" ]; then
    python="$here/.venv/bin/python"
else
    python=$(command -v python3 || command -v python)
fi
module=project_os
if [ ! -d "$here/$module" ]; then
    for candidate in project_os projectos; do
        if [ -d "$here/$candidate" ]; then module=$candidate; break; fi
    done
fi
cd "$here"
exec "$python" -m "$module" "$@"
WRAPPER
    chmod +x "$NEW_PREFIX/bin/project-os"
    chown "$NEW_USER:$NEW_USER" "$NEW_PREFIX/bin/project-os"
fi

say "Serviço"
cat > "$NEW_UNIT" <<UNITFILE
[Unit]
Description=project-os
Documentation=https://github.com/NspxMiguel/project-os
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$NEW_USER
Group=$NEW_USER
Environment=PROJECT_OS_HOME=$NEW_STATE
WorkingDirectory=$NEW_PREFIX
ExecStart=$NEW_PREFIX/bin/project-os --host 0.0.0.0 --port $PORT
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNITFILE
rm -f "$OLD_UNIT"

# The sudo rule and the hostname carry the old name too.
if [ -f /etc/sudoers.d/010_projectos ]; then
    sed 's/^projectos /project-os /' /etc/sudoers.d/010_projectos > /etc/sudoers.d/010_project-os
    chmod 440 /etc/sudoers.d/010_project-os
    rm -f /etc/sudoers.d/010_projectos
fi
if [ "$(hostname)" = "projectos" ]; then
    hostnamectl set-hostname project-os 2>/dev/null || true
    sed -i 's/\bprojectos\b/project-os/g' /etc/hosts 2>/dev/null || true
    note "hostname agora é project-os (o endereço vira http://project-os.local)"
fi

systemctl daemon-reload
systemctl enable --quiet project-os.service
systemctl restart project-os.service

say "Pronto"
note "abra http://project-os.local  (ou o IP de sempre)"
note "daqui pra frente, atualizar é pela própria tela: Updates"

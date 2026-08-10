#!/bin/bash -e
# Put project-os into the image: the source at /opt/project-os, its virtualenv,
# the service, and the two files that make a headless box findable.
#
# Runs inside pi-gen's chroot, so everything here happens to the image, not to
# the machine doing the building.

install -m 755 files/usr/local/sbin/project-os-firstboot "${ROOTFS_DIR}/usr/local/sbin/project-os-firstboot"
install -m 644 files/etc/systemd/system/project-os.service "${ROOTFS_DIR}/etc/systemd/system/project-os.service"
install -m 644 files/etc/systemd/system/project-os-firstboot.service "${ROOTFS_DIR}/etc/systemd/system/project-os-firstboot.service"

# The source tree, minus everything that is not the product. The build runs from
# the repository root, so PROJECT_OS_SRC points at it.
SRC="${PROJECT_OS_SRC:-$(pwd)}"
install -d -m 755 "${ROOTFS_DIR}/opt/project-os"
for item in project_os bin web docs requirements.txt pyproject.toml README.md LICENSE install.sh; do
    if [ -e "${SRC}/${item}" ]; then
        cp -a "${SRC}/${item}" "${ROOTFS_DIR}/opt/project-os/"
    fi
done

on_chroot << 'EOF'
set -e

# pi-gen already created "project-os" as the login user (SSH is the only way in
# when something goes wrong and there is no screen). The service runs as that
# same user rather than as root: the web interface installs packages and runs
# apps, and none of that needs to own the machine.
#
# The password is NOT expired on purpose, and that is a correction of a real
# mistake: expiring it looked like good hygiene, but on a box whose only door is
# SSH it slams that door. An expired password makes sshd refuse every
# non-interactive command, and the interactive change then failed too -- the
# machine became unreachable by the one route it has. A warning that cannot lock
# anyone out beats a policy that can.
if ! id -u project-os >/dev/null 2>&1; then
    adduser --system --group --home /var/lib/project-os --shell /usr/sbin/nologin project-os
fi
install -d -m 755 -o project-os -g project-os /var/lib/project-os
chown -R project-os:project-os /opt/project-os
chmod +x /opt/project-os/bin/project-os

python3 -m venv /opt/project-os/.venv
/opt/project-os/.venv/bin/pip install --no-cache-dir --upgrade pip wheel
/opt/project-os/.venv/bin/pip install --no-cache-dir -r /opt/project-os/requirements.txt

# The optional extras, installed here because on a fresh Pi they are a long
# compile and this is the machine with time to spare. Each one is still optional
# at runtime: if a wheel is unavailable for this architecture the image is built
# without it and the matching screen says what is missing.
/opt/project-os/.venv/bin/pip install --no-cache-dir \
    psutil zeroconf httpx pyatv PyChromecast casttube yt-dlp || \
    echo "project-os: some optional extras did not install; the system still boots"

chown -R project-os:project-os /opt/project-os

# sudo without a password, for the package manager in Advanced mode -- "o botao
# advanced eu falei q é um linux normal". Scoped to apt/flatpak, not to a shell.
cat > /etc/sudoers.d/010_project-os <<'SUDO'
project-os ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt-mark, /usr/bin/flatpak, /usr/bin/systemctl
SUDO
chmod 440 /etc/sudoers.d/010_project-os

systemctl enable project-os.service
systemctl enable project-os-firstboot.service
systemctl enable avahi-daemon.service
systemctl enable ssh.service
EOF

# The card, opened on a laptop, should explain itself.
BOOT_DIR="${ROOTFS_DIR}/boot/firmware"
[ -d "${BOOT_DIR}" ] || BOOT_DIR="${ROOTFS_DIR}/boot"
cat > "${BOOT_DIR}/project-os-wifi.txt.exemplo" << 'EOF'
# Renomeie este arquivo para project-os-wifi.txt e preencha, se a Pi for entrar
# por Wi-Fi. No cabo de rede nao precisa de nada disso.
#
# O arquivo e lido a cada boot e apagado assim que a rede conecta -- ele guarda
# a senha em texto puro.

ssid=NomeDaSuaRede
password=SenhaDaSuaRede
country=BR
EOF

cat > "${BOOT_DIR}/LEIA-ME.txt" << 'EOF'
project-os

1. Se for usar Wi-Fi: renomeie project-os-wifi.txt.exemplo para
   project-os-wifi.txt e escreva o nome da rede e a senha dentro dele.
   No cabo de rede nao precisa fazer nada.

2. Ponha o cartao na Raspberry Pi e ligue.

3. Abra no navegador:  http://project-os.local
   Se o .local nao funcionar na sua rede, use o IP que aparece no roteador.
   A primeira tela pede para criar a sua conta.

Sem monitor em nenhum momento. Se precisar do terminal:
   ssh project-os@project-os.local   (senha inicial: project-os)

   Troque a senha assim que entrar, com:  passwd
   Esta imagem e publica, entao a senha inicial tambem e.
EOF

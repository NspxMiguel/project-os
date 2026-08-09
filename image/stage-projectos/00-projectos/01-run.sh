#!/bin/bash -e
# Put ProjectOS into the image: the source at /opt/projectos, its virtualenv,
# the service, and the two files that make a headless box findable.
#
# Runs inside pi-gen's chroot, so everything here happens to the image, not to
# the machine doing the building.

install -m 755 files/usr/local/sbin/projectos-firstboot "${ROOTFS_DIR}/usr/local/sbin/projectos-firstboot"
install -m 644 files/etc/systemd/system/projectos.service "${ROOTFS_DIR}/etc/systemd/system/projectos.service"
install -m 644 files/etc/systemd/system/projectos-firstboot.service "${ROOTFS_DIR}/etc/systemd/system/projectos-firstboot.service"

# The source tree, minus everything that is not the product. The build runs from
# the repository root, so PROJECTOS_SRC points at it.
SRC="${PROJECTOS_SRC:-$(pwd)}"
install -d -m 755 "${ROOTFS_DIR}/opt/projectos"
for item in projectos web docs requirements.txt pyproject.toml README.md LICENSE install.sh; do
    if [ -e "${SRC}/${item}" ]; then
        cp -a "${SRC}/${item}" "${ROOTFS_DIR}/opt/projectos/"
    fi
done

on_chroot << 'EOF'
set -e

# pi-gen already created "projectos" as the login user (SSH is the only way in
# when something goes wrong and there is no screen). The service runs as that
# same user rather than as root: the web interface installs packages and runs
# apps, and none of that needs to own the machine.
#
# The image is public, so its password is public too. Expiring it means the
# first SSH login has to set a new one before it gives you a shell.
if ! id -u projectos >/dev/null 2>&1; then
    adduser --system --group --home /var/lib/projectos --shell /usr/sbin/nologin projectos
fi
chage -d 0 projectos 2>/dev/null || true
install -d -m 755 -o projectos -g projectos /var/lib/projectos
chown -R projectos:projectos /opt/projectos

python3 -m venv /opt/projectos/.venv
/opt/projectos/.venv/bin/pip install --no-cache-dir --upgrade pip wheel
/opt/projectos/.venv/bin/pip install --no-cache-dir -r /opt/projectos/requirements.txt

# The optional extras, installed here because on a fresh Pi they are a long
# compile and this is the machine with time to spare. Each one is still optional
# at runtime: if a wheel is unavailable for this architecture the image is built
# without it and the matching screen says what is missing.
/opt/projectos/.venv/bin/pip install --no-cache-dir \
    psutil zeroconf httpx pyatv PyChromecast casttube yt-dlp || \
    echo "ProjectOS: some optional extras did not install; the system still boots"

chown -R projectos:projectos /opt/projectos

# sudo without a password, for the package manager in Advanced mode -- "o botao
# advanced eu falei q é um linux normal". Scoped to apt/flatpak, not to a shell.
cat > /etc/sudoers.d/010_projectos <<'SUDO'
projectos ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt-mark, /usr/bin/flatpak, /usr/bin/systemctl
SUDO
chmod 440 /etc/sudoers.d/010_projectos

systemctl enable projectos.service
systemctl enable projectos-firstboot.service
systemctl enable avahi-daemon.service
systemctl enable ssh.service
EOF

# The card, opened on a laptop, should explain itself.
BOOT_DIR="${ROOTFS_DIR}/boot/firmware"
[ -d "${BOOT_DIR}" ] || BOOT_DIR="${ROOTFS_DIR}/boot"
cat > "${BOOT_DIR}/projectos-wifi.txt.exemplo" << 'EOF'
# Renomeie este arquivo para projectos-wifi.txt e preencha, se a Pi for entrar
# por Wi-Fi. No cabo de rede nao precisa de nada disso.
#
# O arquivo e lido a cada boot e apagado assim que a rede conecta -- ele guarda
# a senha em texto puro.

ssid=NomeDaSuaRede
password=SenhaDaSuaRede
country=BR
EOF

cat > "${BOOT_DIR}/LEIA-ME.txt" << 'EOF'
ProjectOS

1. Se for usar Wi-Fi: renomeie projectos-wifi.txt.exemplo para
   projectos-wifi.txt e escreva o nome da rede e a senha dentro dele.
   No cabo de rede nao precisa fazer nada.

2. Ponha o cartao na Raspberry Pi e ligue.

3. Abra no navegador:  http://projectos.local
   Se o .local nao funcionar na sua rede, use o IP que aparece no roteador.
   A primeira tela pede para criar a sua conta.

Sem monitor em nenhum momento. Se precisar do terminal:
   ssh projectos@projectos.local   (senha inicial: projectos)
   No primeiro login ele obriga a trocar a senha -- esta imagem e publica,
   entao a senha inicial tambem e.
EOF

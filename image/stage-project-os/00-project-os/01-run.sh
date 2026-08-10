#!/bin/bash -e
# Put project-os into the image: the source at /opt/project-os, its virtualenv,
# the service, and the two files that make a headless box findable.
#
# Runs inside pi-gen's chroot, so everything here happens to the image, not to
# the machine doing the building.

install -m 755 files/usr/local/sbin/project-os-firstboot "${ROOTFS_DIR}/usr/local/sbin/project-os-firstboot"
install -m 755 files/usr/local/sbin/project-os-set-password "${ROOTFS_DIR}/usr/local/sbin/project-os-set-password"
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
# No password at all, on purpose -- the Home Assistant idea:
#
#   "n da pra criar a passwd na porra do setup??? no site igual ha"
#
# A password printed in a public README is not a password. Shipping one and then
# expiring it was worse: an expired password makes sshd refuse non-interactive
# commands, which is the only way to administer a box with no screen, and it
# locked the machine out entirely. So the account ships locked, and the first-run
# screen in the browser is what gives it a password -- one that exists nowhere
# except on this card.
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
project-os ALL=(root) NOPASSWD: /usr/local/sbin/project-os-set-password
SUDO
chmod 440 /etc/sudoers.d/010_project-os

# Locked: no password, so no login, until the first-run screen sets one.
passwd --lock project-os >/dev/null 2>&1 || true

# pi-gen marks the first user's password as needing an immediate change, which
# makes sense for an image that ships a known password. This one ships none, and
# the flag survives into the password the browser sets later -- sshd then demands
# a change it cannot complete and hangs up, on a box with no screen. Turn it off
# here as well as in the helper, so a card that never went through the helper is
# not booby-trapped either.
chage -d "$(date -u +%Y-%m-%d)" -m 0 -M -1 -I -1 -E -1 project-os >/dev/null 2>&1 || true

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

Sem monitor em nenhum momento.

O SSH vem sem senha nenhuma -- esta imagem e publica, e senha que vem escrita
num arquivo publico nao e senha. Voce define a sua na propria tela do passo 3,
e a partir dai entra com:

   ssh project-os@project-os.local
EOF

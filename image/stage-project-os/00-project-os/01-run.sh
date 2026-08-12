#!/bin/bash -e
# Put project-os into the image: the source at /opt/project-os, its virtualenv,
# the service, and the two files that make a headless box findable.
#
# Runs inside pi-gen's chroot, so everything here happens to the image, not to
# the machine doing the building.

install -m 755 files/usr/local/sbin/project-os-firstboot "${ROOTFS_DIR}/usr/local/sbin/project-os-firstboot"
install -m 755 files/usr/local/sbin/project-os-set-password "${ROOTFS_DIR}/usr/local/sbin/project-os-set-password"
install -m 755 files/usr/local/sbin/project-os-system-update "${ROOTFS_DIR}/usr/local/sbin/project-os-system-update"
install -m 755 files/usr/local/sbin/project-os-slot-state "${ROOTFS_DIR}/usr/local/sbin/project-os-slot-state"

# O esquema de dois sistemas (docs/RECOVERY.md): o decisor de slot, o
# reparticionamento do primeiro boot, e os dois ganchos que põem tudo isso
# dentro do initramfs.
install -d -m 755 "${ROOTFS_DIR}/usr/share/project-os"
install -m 755 files/usr/share/project-os/slot-decide.sh "${ROOTFS_DIR}/usr/share/project-os/slot-decide.sh"
install -m 755 files/usr/share/project-os/layout.sh "${ROOTFS_DIR}/usr/share/project-os/layout.sh"
install -m 755 files/usr/share/project-os/fstab-slot.sh "${ROOTFS_DIR}/usr/share/project-os/fstab-slot.sh"
install -d -m 755 "${ROOTFS_DIR}/etc/initramfs-tools/scripts/local-top"
install -d -m 755 "${ROOTFS_DIR}/etc/initramfs-tools/hooks"
install -m 755 files/etc/initramfs-tools/scripts/local-top/project-os-layout \
    "${ROOTFS_DIR}/etc/initramfs-tools/scripts/local-top/project-os-layout"
install -m 755 files/etc/initramfs-tools/scripts/local-top/project-os-slot \
    "${ROOTFS_DIR}/etc/initramfs-tools/scripts/local-top/project-os-slot"
install -m 755 files/etc/initramfs-tools/hooks/project-os-slot \
    "${ROOTFS_DIR}/etc/initramfs-tools/hooks/project-os-slot"
install -m 644 files/etc/systemd/system/project-os-clone-slot.service \
    "${ROOTFS_DIR}/etc/systemd/system/project-os-clone-slot.service"
install -m 755 files/usr/local/sbin/project-os-clone-slot \
    "${ROOTFS_DIR}/usr/local/sbin/project-os-clone-slot"
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
project-os ALL=(root) NOPASSWD: /usr/local/sbin/project-os-system-update
# A FAT é montada como root (uid=0, umask 0022 -- o padrão do vfat) e este
# serviço não é root: sem esta linha ele não consegue gravar qual slot deve
# subir, e uma atualização escreveria o slot B inteiro sem conseguir apontar o
# boot para ele.
project-os ALL=(root) NOPASSWD: /usr/local/sbin/project-os-slot-state
SUDO
chmod 440 /etc/sudoers.d/010_project-os

# Os dados do usuário moram numa partição própria (p4), fora dos dois sistemas.
#
# Sem esta linha o esquema de dois sistemas apaga tudo do dono na primeira
# atualização: o Pi reinicia no slot B, encontra um /var/lib/project-os vazio, e
# mostra a tela de criar conta como se fosse um aparelho novo. Banco, conta,
# playlists e as músicas baixadas ficariam no slot A, invisíveis.
#
# nofail: antes do primeiro reparticionamento a partição ainda não existe, e um
# fstab que exige uma partição inexistente segura o boot num prompt de emergência
# -- numa caixa sem tela isso é o mesmo que não subir.
grep -q "pos-data" /etc/fstab || cat >> /etc/fstab <<'FSTAB'
# Dados do project-os: banco, configuração, música. Fora dos dois sistemas de
# propósito -- atualizar troca sistema, nunca os seus dados. docs/RECOVERY.md
LABEL=pos-data  /var/lib/project-os  ext4  defaults,noatime,nofail,x-systemd.device-timeout=15  0  2
FSTAB

# O carimbo de "este slot está inteiro".
#
# O slot A sai de fábrica completo, e precisa dizer isso: é o carimbo que impede
# o project-os-clone-slot de copiar por cima de um slot que já tem sistema. Sem
# ele aqui, o dia em que o Pi estiver rodando o slot B e o clone olhar para o
# slot A, o A não teria carimbo nenhum -- e o clone passaria por cima do sistema
# antigo, que é justamente o caminho de volta.
date -u +"%Y-%m-%dT%H:%M:%SZ" > /etc/project-os-slot-completo

# Locked: no password, so no login, until the first-run screen sets one.
passwd --lock project-os >/dev/null 2>&1 || true

# pi-gen marks the first user's password as needing an immediate change, which
# makes sense for an image that ships a known password. This one ships none, and
# the flag survives into the password the browser sets later -- sshd then demands
# a change it cannot complete and hangs up, on a box with no screen. Turn it off
# here as well as in the helper, so a card that never went through the helper is
# not booby-trapped either.
chage -d "$(date -u +%Y-%m-%d)" -m 0 -M -1 -I -1 -E -1 project-os >/dev/null 2>&1 || true

# initramfs-tools: é ele quem carrega o decisor de slot antes de existir
# sistema nenhum. Sem isto instalado, o cartão sobe pelo caminho normal e o
# esquema de dois sistemas simplesmente não acontece.
# rsync é o que clona o sistema para o slot reserva; sem ele o slot B fica vazio
# para sempre e a primeira atualização não teria de onde voltar.
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    initramfs-tools busybox parted rsync >/dev/null

# O initramfs, um por kernel. Esta parte já quebrou uma imagem e o motivo vale
# escrito: o cartão traz quatro kernels (kernel.img, kernel7.img, kernel7l.img,
# kernel8.img) e o Pi 3B sobe o kernel7. Um initramfs carrega dentro os módulos
# do kernel para o qual foi gerado, então gerar um só -- ainda por cima
# escolhendo "o último da lista", que é o v8 de 64 bits -- e apontar o config.txt
# para ele deixa o Pi 3B esperando para sempre um cartão SD cujos drivers não
# existem naquele initramfs. Trava antes da rede, antes de tudo.
#
# A imagem já vem com auto_initramfs=1, que é o mecanismo certo: o firmware
# carrega o initramfs que combina com o kernel que ele decidiu subir. Então não
# se acrescenta linha nenhuma ao config.txt -- só se regeneram TODOS os
# initramfs, agora que os nossos hooks estão instalados.
# E tem que ser "-c", não "-u". O pi-gen DESLIGA o update-initramfs no começo da
# build -- stage0/02-firmware/02-run.sh põe update_initramfs=no -- e só religa no
# export-image, que roda depois deste stage. Com a chave desligada, a função
# update() do update-initramfs sai logo no começo, dizendo "Not updating
# initramfs", e sai com **sucesso**: um "-u" aqui não regenera nada e ainda
# engana quem confia no código de saída (um `|| ...` depois dele nunca roda).
#
# O modo "create" não passa por essa chave. E, ao contrário do que parece, ele
# não se recusa a sobrescrever: generate_initramfs() faz
# `mkinitramfs -o "$arquivo.new" && mv -f "$arquivo.new" "$arquivo"`.
#
# A chave é religada aqui também, para o caso de uma versão futura do pi-gen
# passar a bloquear o create -- e porque um sistema instalado que não regenera o
# initramfs ao atualizar o kernel é um tijolo esperando a hora.
if [ -f /etc/initramfs-tools/update-initramfs.conf ]; then
    sed -i 's/^update_initramfs=.*/update_initramfs=yes/' /etc/initramfs-tools/update-initramfs.conf
fi
update-initramfs -c -k all

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot

# auto_initramfs=1 tem que estar ligado, e a linha "initramfs <arquivo>" não
# pode existir: ela atropela o automático e força um arquivo só para todos os
# kernels, que é exatamente o defeito descrito acima.
sed -i '/^initramfs /d' "$BOOT/config.txt"
grep -q "^auto_initramfs=1" "$BOOT/config.txt" || echo "auto_initramfs=1" >> "$BOOT/config.txt"

# Fora o primeiro boot do Raspberry Pi OS.
#
# O cmdline.txt vem com init=/usr/lib/raspberrypi-sys-mods/firstboot, que troca
# o init do sistema no primeiríssimo boot. Esse script sorteia um identificador
# de disco novo, reescreve a tabela de partições com fdisk para gravá-lo,
# conserta o fstab e o cmdline.txt para o novo número e reinicia.
#
# Nada disso é errado -- é errado *junto com o nosso*. No único boot que precisa
# dar certo passariam a existir dois donos da tabela de partições: o nosso
# initramfs, que corta o cartão em quatro, e o firstboot, que reescreve o MBR
# logo depois. Duas coisas mexendo no mesmo setor no mesmo boot é como se perde
# um cartão, e é justamente o boot em que ele não pode ter que gravar de novo.
#
# O que o firstboot faz e ainda queremos continua acontecendo:
#   - chaves de host do SSH: regenerate_ssh_host_keys.service está habilitado
#     por conta própria, roda Before=ssh.service, e a imagem não traz chave
#     nenhuma pronta (conferido dentro da imagem);
#   - custom.toml: não usamos;
#   - identificador de disco sorteado: não queremos. Um número fixo, o mesmo que
#     está no cmdline.txt e no fstab desde a build, é o que faz o PARTUUID
#     continuar valendo depois do reparticionamento (ver layout.sh).
sed -i 's| init=/usr/lib/raspberrypi-sys-mods/firstboot||' "$BOOT/cmdline.txt"
if grep -q "raspberrypi-sys-mods/firstboot" "$BOOT/cmdline.txt"; then
    echo "project-os: não consegui tirar o firstboot do cmdline.txt" >&2
    exit 1
fi

# Cada kernel tem o seu, e cada um precisa ter os nossos scripts E os módulos do
# kernel certo. Conferir os dois é o que teria evitado a imagem quebrada.
falta() { echo "project-os: $*" >&2; exit 1; }
for PAR in "initramfs:v6" "initramfs7:v7" "initramfs7l:v7l" "initramfs8:v8"; do
    ARQUIVO="${PAR%%:*}"
    SUFIXO="${PAR##*:}"
    CAMINHO="$BOOT/$ARQUIVO"
    [ -f "$CAMINHO" ] || falta "faltou $ARQUIVO na partição de boot"
    LISTA=$(lsinitramfs "$CAMINHO" 2>/dev/null || true)
    [ -n "$LISTA" ] || falta "não consegui ler $ARQUIVO"
    for PECA in slot-decide.sh layout.sh project-os-slot project-os-layout \
                sfdisk mkfs.ext4 resize2fs e2fsck blkid; do
        echo "$LISTA" | grep -q "$PECA" || falta "$ARQUIVO saiu sem $PECA"
    done
    # E os módulos: um initramfs com os módulos do kernel errado é a falha que
    # trancou o Pi dele, e ela não aparece em nenhuma outra checagem.
    echo "$LISTA" | grep -q "lib/modules/[^/]*-rpi-${SUFIXO}/" || \
        falta "$ARQUIVO não tem os módulos do kernel $SUFIXO"
done

systemctl enable project-os.service
systemctl enable project-os-firstboot.service
systemctl enable project-os-clone-slot.service
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

#!/bin/bash
# Confere a imagem pronta antes de alguém gravar um cartão com ela.
#
# A build já falha se o initramfs sair sem o decisor dentro, mas isso é o
# construtor conferindo a si mesmo. Isto aqui abre o arquivo que vai para o
# cartão e olha o que tem lá -- e, mais importante, **simula o primeiro boot**:
# estica a imagem para o tamanho do cartão dele e roda o reparticionamento de
# verdade, com o layout.sh que está dentro da própria imagem.
#
# Se isto passa, o que sobra de risco é o Pi ligar. O resto foi verificado.
#
# Uso: verify-image-docker.sh /repo/scratch/project-os-0.4.0.img

set -euo pipefail

IMG="${1:?uso: verify-image-docker.sh <imagem.img>}"
CARTAO_MB="${2:-62000}"
FALHAS=0

apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq fdisk dosfstools e2fsprogs parted util-linux cpio xz-utils file \
    initramfs-tools-core zstd >/dev/null 2>&1

ok()    { echo "  ok    $*"; }
falha() { echo "  FALHA $*"; FALHAS=$((FALHAS + 1)); }

nodes() {
    local base; base=$(basename "$1")
    for entrada in /sys/block/"$base"/"$base"*/dev; do
        [ -r "$entrada" ] || continue
        local nome maj min atual esperado
        nome=$(basename "$(dirname "$entrada")")
        maj=$(cut -d: -f1 "$entrada"); min=$(cut -d: -f2 "$entrada")
        if [ -b "/dev/$nome" ]; then
            atual=$(stat -c '%t:%T' "/dev/$nome" 2>/dev/null || echo "")
            esperado=$(printf '%x:%x' "$maj" "$min")
            [ "$atual" = "$esperado" ] && continue
            rm -f "/dev/$nome"
        fi
        mknod "/dev/$nome" b "$maj" "$min" 2>/dev/null || true
    done
}

echo "== o cartão como ele sai da gravação =="
# Um cartão de 62 GB gravado com esta imagem tem a imagem no começo e espaço
# livre no resto. É exatamente isso que o primeiro boot encontra.
truncate -s "${CARTAO_MB}M" "$IMG"
LOOP=$(losetup --show -fP "$IMG")
partprobe "$LOOP" >/dev/null 2>&1 || true
nodes "$LOOP"

PARTES=$(sfdisk -d "$LOOP" | grep -c "^${LOOP}p[0-9]")
[ "$PARTES" = "2" ] && ok "imagem com duas partições, como esperado" \
                    || falha "a imagem veio com $PARTES partições"

mkdir -p /mnt/boot /mnt/raiz
mount "${LOOP}p1" /mnt/boot

grep -q "^initramfs initramfs-project-os followkernel" /mnt/boot/config.txt \
    && ok "config.txt manda o firmware carregar o nosso initramfs" \
    || falha "config.txt não tem a linha do initramfs"

if [ -f /mnt/boot/initramfs-project-os ]; then
    ok "initramfs presente ($(du -h /mnt/boot/initramfs-project-os | cut -f1))"
    # O que importa não é o arquivo existir, é o que tem dentro dele.
    # Um initramfs do Debian é um ou mais cpio concatenados, cada um com sua
    # compressão. Abrir isso "na mão" com zcat|cpio funciona por acaso e falha
    # em silêncio -- foi o que aconteceu na primeira versão deste teste, e o
    # resultado foi um alarme falso dizendo que a imagem estava quebrada.
    # lsinitramfs é a ferramenta que entende o formato.
    LISTA=$(mktemp)
    lsinitramfs /mnt/boot/initramfs-project-os > "$LISTA" 2>/dev/null || true
    if [ ! -s "$LISTA" ]; then
        rm -rf /tmp/desmonta && mkdir -p /tmp/desmonta
        unmkinitramfs /mnt/boot/initramfs-project-os /tmp/desmonta >/dev/null 2>&1 || true
        find /tmp/desmonta -type f 2>/dev/null > "$LISTA" || true
    fi
    [ -s "$LISTA" ] || falha "não consegui abrir o initramfs para conferir o conteúdo"
    for peca in slot-decide.sh project-os-slot project-os-layout layout.sh; do
        grep -q "$peca" "$LISTA" && ok "o initramfs leva $peca" \
                                 || falha "o initramfs NÃO tem $peca"
    done
else
    falha "não tem initramfs na partição de boot"
fi
umount /mnt/boot

echo "== o sistema dentro da imagem =="
mount "${LOOP}p2" /mnt/raiz
for arquivo in \
    /usr/share/project-os/layout.sh \
    /usr/share/project-os/slot-decide.sh \
    /usr/local/sbin/project-os-system-update \
    /usr/local/sbin/project-os-clone-slot \
    /etc/systemd/system/project-os-clone-slot.service \
    /opt/project-os/project_os/core/slots.py \
    /opt/project-os/project_os/core/sysupdate.py
do
    [ -e "/mnt/raiz$arquivo" ] && ok "$arquivo" || falha "faltou $arquivo"
done

grep -q "project-os-system-update" /mnt/raiz/etc/sudoers.d/010_project-os \
    && ok "o sudoers libera o ajudante de sistema" \
    || falha "o sudoers não libera o ajudante de sistema"

# Diretiva ativa, não a palavra: o arquivo tem um comentário longo explicando
# justamente por que ela não está lá, e a primeira versão deste teste acusou o
# comentário.
grep -qE "^[[:space:]]*NoNewPrivileges[[:space:]]*=" /mnt/raiz/etc/systemd/system/project-os.service \
    && falha "o NoNewPrivileges voltou (quebra sudo, apt e a senha do SSH)" \
    || ok "sem NoNewPrivileges no serviço"

grep -qE "^[[:space:]]*CapabilityBoundingSet[[:space:]]*=" /mnt/raiz/etc/systemd/system/project-os.service \
    && falha "o CapabilityBoundingSet voltou (quebra o dpkg)" \
    || ok "sem CapabilityBoundingSet no serviço"

[ -L /mnt/raiz/etc/systemd/system/multi-user.target.wants/project-os-clone-slot.service ] \
    && ok "o clone do slot B está habilitado" \
    || falha "o clone do slot B não está habilitado"

VERSAO=$(grep -oE '"[0-9]+\.[0-9]+\.[0-9]+"' /mnt/raiz/opt/project-os/project_os/__init__.py | head -1 | tr -d '"')
[ -n "$VERSAO" ] && ok "versão dentro da imagem: $VERSAO" || falha "não achei a versão"
umount /mnt/raiz

echo "== primeiro boot: o reparticionamento de verdade =="
# O mesmo layout.sh que está dentro da imagem, rodando sobre a imagem.
mount "${LOOP}p2" /mnt/raiz
cp /mnt/raiz/usr/share/project-os/layout.sh /tmp/layout.sh
umount /mnt/raiz

bash /tmp/layout.sh "$LOOP" || falha "o reparticionamento falhou"
partprobe "$LOOP" >/dev/null 2>&1 || true
nodes "$LOOP"

PARTES=$(sfdisk -d "$LOOP" | grep -c "^${LOOP}p[0-9]")
[ "$PARTES" = "4" ] && ok "quatro partições depois do primeiro boot" \
                    || falha "esperava 4 partições, achei $PARTES"

mount "${LOOP}p1" /mnt/boot
grep -q "^initramfs initramfs-project-os" /mnt/boot/config.txt \
    && ok "o boot continua intacto depois de reparticionar" \
    || falha "o boot se perdeu no reparticionamento"
umount /mnt/boot

mount "${LOOP}p2" /mnt/raiz
[ -f /mnt/raiz/opt/project-os/project_os/core/slots.py ] \
    && ok "o sistema sobreviveu ao resize" \
    || falha "o sistema se perdeu no resize"
TAM=$(df -m --output=size /mnt/raiz | tail -1 | tr -d ' ')
[ "$TAM" -gt 6000 ] && ok "o sistema A ficou com ${TAM} MB" || falha "sistema A ficou com ${TAM} MB"
umount /mnt/raiz

[ "$(blkid -o value -s LABEL "${LOOP}p3")" = "rootB" ] && ok "sistema B pronto para receber" \
    || falha "sistema B não foi formatado"
[ "$(blkid -o value -s LABEL "${LOOP}p4")" = "data" ] && ok "partição de dados pronta" \
    || falha "partição de dados não foi formatada"

losetup -d "$LOOP"

echo
if [ "$FALHAS" -eq 0 ]; then
    echo "TUDO OK"
    exit 0
fi
echo "$FALHAS FALHA(S)"
exit 1

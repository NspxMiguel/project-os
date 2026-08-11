#!/bin/bash
# Testa o reparticionamento num cartão de mentira, dentro de um container Linux.
#
# Este é o script que mexe na tabela de partições do cartão dele. Não existe
# "quase certo" aqui: ou ele preserva o boot e a raiz, ou o Pi vira um tijolo e
# a promessa de nunca mais tirar o cartão morre junto. Então antes de chegar
# perto de hardware ele roda contra um disco construído igual ao que o pi-gen
# gera, com arquivos dentro, e conferimos que os arquivos continuam lá.
#
# Rodado por tests/test_layout.py via docker; dá para chamar à mão também.

set -euo pipefail

LAYOUT=/repo/image/stage-project-os/00-project-os/files/usr/share/project-os/layout.sh
TRABALHO=$(mktemp -d)
FALHAS=0

apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq fdisk dosfstools e2fsprogs parted util-linux >/dev/null 2>&1

# Em container não há udev criando /dev/loopNpM: fazemos como o layout.sh faz.
nodes() {
    local base
    base=$(basename "$1")
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

ok()    { echo "  ok    $*"; }
falha() { echo "  FALHA $*"; FALHAS=$((FALHAS + 1)); }

# Monta um cartão igual ao que sai do pi-gen: p1 FAT de 512M, p2 ext4 pequena.
criar_cartao() {
    local imagem="$1" tamanho_mb="$2" raiz_mb="${3:-2560}"
    dd if=/dev/zero of="$imagem" bs=1M count=0 seek="$tamanho_mb" status=none
    sfdisk --quiet "$imagem" >/dev/null <<TAB
label: dos
unit: sectors
${imagem}1 : start=8192, size=1048576, type=c
${imagem}2 : start=1056768, size=$((raiz_mb * 2048)), type=83
TAB
    local loop
    loop=$(losetup --show -fP "$imagem")
    partprobe "$loop" >/dev/null 2>&1 || true
    nodes "$loop"
    mkfs.vfat -n bootfs "${loop}p1" >/dev/null 2>&1
    mkfs.ext4 -q -F -L rootfs "${loop}p2" >/dev/null 2>&1
    local montagem="$TRABALHO/mnt"
    mkdir -p "$montagem"
    mount "${loop}p1" "$montagem"
    echo "console=serial0,115200 root=/dev/mmcblk0p2" > "$montagem/cmdline.txt"
    umount "$montagem"
    mount "${loop}p2" "$montagem"
    mkdir -p "$montagem/opt/project-os"
    echo "o sistema inteiro do miguel" > "$montagem/opt/project-os/prova.txt"
    dd if=/dev/urandom of="$montagem/opt/project-os/grande.bin" bs=1M count=64 status=none
    sha256sum "$montagem/opt/project-os/grande.bin" | cut -d' ' -f1 > "$TRABALHO/sha-antes"
    umount "$montagem"
    echo "$loop"
}

echo "== cartão de 62 GB, como o dele =="
IMG="$TRABALHO/cartao.img"
LOOP=$(criar_cartao "$IMG" 62000)

bash "$LAYOUT" "$LOOP" >/dev/null 2>&1 || falha "layout.sh saiu com erro"
partprobe "$LOOP" >/dev/null 2>&1 || true
nodes "$LOOP"
sleep 1

PARTES=$(sfdisk -d "$LOOP" | grep -c "^${LOOP}p[0-9]")
[ "$PARTES" = "4" ] && ok "quatro partições" || falha "esperava 4 partições, achei $PARTES"

P1_INICIO=$(sfdisk -d "$LOOP" | sed -n "s|^${LOOP}p1 .*start=[[:space:]]*\([0-9]*\).*|\1|p")
P1_TAM=$(sfdisk -d "$LOOP" | sed -n "s|^${LOOP}p1 .*size=[[:space:]]*\([0-9]*\).*|\1|p")
[ "$P1_INICIO" = "8192" ] && [ "$P1_TAM" = "1048576" ] \
    && ok "a partição de boot não foi tocada" \
    || falha "a partição de boot mudou: início=$P1_INICIO tamanho=$P1_TAM"

[ "$(blkid -o value -s TYPE "${LOOP}p1")" = "vfat" ] && ok "boot continua FAT" || falha "boot deixou de ser FAT"

mkdir -p "$TRABALHO/mnt"
mount "${LOOP}p1" "$TRABALHO/mnt"
grep -q "root=/dev/mmcblk0p2" "$TRABALHO/mnt/cmdline.txt" \
    && ok "cmdline.txt intacto" || falha "cmdline.txt sumiu ou mudou"
umount "$TRABALHO/mnt"

# O que mais importa: a raiz cresceu e os arquivos continuam lá, byte por byte.
mount "${LOOP}p2" "$TRABALHO/mnt"
if [ -f "$TRABALHO/mnt/opt/project-os/prova.txt" ]; then
    ok "os arquivos da raiz sobreviveram"
else
    falha "a raiz perdeu os arquivos"
fi
DEPOIS=$(sha256sum "$TRABALHO/mnt/opt/project-os/grande.bin" 2>/dev/null | cut -d' ' -f1 || echo vazio)
[ "$DEPOIS" = "$(cat "$TRABALHO/sha-antes")" ] \
    && ok "64 MB de dados conferem byte a byte depois do resize" \
    || falha "os dados da raiz mudaram"
TAMANHO_RAIZ=$(df -m --output=size "$TRABALHO/mnt" | tail -1 | tr -d ' ')
[ "$TAMANHO_RAIZ" -gt 6000 ] \
    && ok "a raiz cresceu para ${TAMANHO_RAIZ} MB" \
    || falha "a raiz não cresceu (${TAMANHO_RAIZ} MB)"
umount "$TRABALHO/mnt"

[ "$(blkid -o value -s LABEL "${LOOP}p3")" = "rootB" ] && ok "slot B formatado" || falha "slot B não formatado"
[ "$(blkid -o value -s LABEL "${LOOP}p4")" = "data" ] && ok "dados formatados" || falha "dados não formatados"

P3_INICIO=$(sfdisk -d "$LOOP" | sed -n "s|^${LOOP}p3 .*start=[[:space:]]*\([0-9]*\).*|\1|p")
[ $((P3_INICIO % 8192)) -eq 0 ] && ok "partições alinhadas em 4 MiB" || falha "p3 desalinhada ($P3_INICIO)"

echo "== rodar de novo não faz nada (idempotente) =="
SAIDA=$(bash "$LAYOUT" "$LOOP" 2>&1 || true)
echo "$SAIDA" | grep -q "já tem" && ok "reconhece que já está pronto" || falha "não é idempotente: $SAIDA"
PARTES=$(sfdisk -d "$LOOP" | grep -c "^${LOOP}p[0-9]")
[ "$PARTES" = "4" ] && ok "continua com quatro partições" || falha "mexeu de novo na tabela"
losetup -d "$LOOP"

echo "== cartão pequeno demais: não mexe em nada =="
IMG2="$TRABALHO/pequeno.img"
LOOP2=$(criar_cartao "$IMG2" 4000 2560)
ANTES=$(sfdisk -d "$LOOP2")
bash "$LAYOUT" "$LOOP2" >/dev/null 2>&1 && falha "aceitou um cartão pequeno demais" || ok "recusou o cartão pequeno"
[ "$(sfdisk -d "$LOOP2")" = "$ANTES" ] && ok "a tabela não foi tocada" || falha "mexeu na tabela mesmo recusando"
losetup -d "$LOOP2"

echo "== disco que não é o nosso: não mexe em nada =="
IMG3="$TRABALHO/estranho.img"
dd if=/dev/zero of="$IMG3" bs=1M count=0 seek=20000 status=none
sfdisk --quiet "$IMG3" >/dev/null <<TAB
label: dos
unit: sectors
${IMG3}1 : start=8192, size=2097152, type=83
${IMG3}2 : start=2105344, size=2097152, type=83
TAB
LOOP3=$(losetup --show -fP "$IMG3")
partprobe "$LOOP3" >/dev/null 2>&1 || true
nodes "$LOOP3"
mkfs.ext4 -q -F "${LOOP3}p1" >/dev/null 2>&1
ANTES3=$(sfdisk -d "$LOOP3")
bash "$LAYOUT" "$LOOP3" >/dev/null 2>&1 && falha "aceitou um disco sem FAT de boot" || ok "recusou o disco estranho"
[ "$(sfdisk -d "$LOOP3")" = "$ANTES3" ] && ok "a tabela do disco estranho está intacta" || falha "mexeu num disco que não é nosso"
losetup -d "$LOOP3"

rm -rf "$TRABALHO"
echo
if [ "$FALHAS" -eq 0 ]; then
    echo "TUDO OK"
    exit 0
fi
echo "$FALHAS FALHA(S)"
exit 1

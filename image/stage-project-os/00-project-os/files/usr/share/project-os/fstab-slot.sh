#!/bin/sh
# Ajusta o /etc/fstab de um slot para o slot que ele realmente é.
#
# Um sistema chega ao slot B de dois jeitos -- copiado do slot A (clone-slot) ou
# desempacotado de um tarball (system-update) -- e nos dois o /etc/fstab que
# chega junto diz que a raiz é a p2, porque era verdade na origem. Escrito assim
# na p3, o slot B afirma ser a partição do vizinho.
#
# A tentação é simplesmente apagar a linha da raiz e deixar o initramfs decidir
# sozinho. Não dá: o cmdline.txt não tem "rw", então o kernel monta a raiz
# **somente leitura**, e quem a remonta para escrita é o systemd-remount-fs
# lendo exatamente essa linha. Sem ela o slot sobe, responde, e não grava nada
# -- o pior tipo de defeito, porque parece que funcionou.
#
# Então a linha continua existindo e passa a apontar para o lugar certo. Os
# PARTUUID vêm do disco de verdade, não de conta de cabeça, o que também
# conserta o caso de o tarball ter vindo de um cartão com outro identificador.
#
# Uso:  fstab-slot.sh <caminho do fstab> <partição raiz> [partição de boot]

set -u

FSTAB="${1:-}"
PART_RAIZ="${2:-}"
PART_BOOT="${3:-}"

aviso() { echo "project-os-fstab: $*" >&2; }

[ -n "$FSTAB" ] && [ -n "$PART_RAIZ" ] || { aviso "uso: fstab-slot.sh <fstab> <raiz> [boot]"; exit 2; }
[ -f "$FSTAB" ] || { aviso "$FSTAB não existe"; exit 2; }

PU_RAIZ=$(blkid -o value -s PARTUUID "$PART_RAIZ" 2>/dev/null || true)
[ -n "$PU_RAIZ" ] || { aviso "não descobri o PARTUUID de $PART_RAIZ; fstab intocado"; exit 3; }
LINHA_RAIZ="PARTUUID=$PU_RAIZ  /  ext4  defaults,noatime  0  1"

LINHA_BOOT=""
if [ -n "$PART_BOOT" ]; then
    PU_BOOT=$(blkid -o value -s PARTUUID "$PART_BOOT" 2>/dev/null || true)
    [ -n "$PU_BOOT" ] && LINHA_BOOT="PARTUUID=$PU_BOOT  /boot/firmware  vfat  defaults  0  2"
fi

NOVO=$(mktemp) || { aviso "sem lugar para escrever"; exit 3; }

awk -v raiz="$LINHA_RAIZ" -v boot="$LINHA_BOOT" '
    /^[[:space:]]*#/       { print; next }
    NF < 2                 { print; next }
    $2 == "/"              { print raiz; tem_raiz = 1; next }
    $2 == "/boot/firmware" { if (boot != "") { print boot; tem_boot = 1 } next }
                           { print }
    END {
        if (!tem_raiz) print raiz
        if (boot != "" && !tem_boot) print boot
    }
' "$FSTAB" > "$NOVO"

# Só substitui se sobrou um fstab com a raiz dentro. Um awk que falhasse no meio
# deixaria o slot sem linha de raiz nenhuma, que é pior que o arquivo original.
if awk '$1 !~ /^#/ && $2 == "/" { achou = 1 } END { exit !achou }' "$NOVO"; then
    cat "$NOVO" > "$FSTAB"
    rm -f "$NOVO"
    exit 0
fi

aviso "a reescrita saiu sem a linha da raiz; deixando o fstab como estava"
rm -f "$NOVO"
exit 3

#!/bin/bash
# Testa a escolha do slot do jeito que ela acontece de verdade no boot.
#
# O detalhe que este teste existe para prender: os scripts de local-top NÃO são
# carregados dentro do shell do init. O ORDER roda cada um como processo filho e
# depois faz source de /conf/param.conf. Um "export ROOT" no filho não chega em
# ninguém -- e o sintoma disso é invisível: o Pi sobe normal, sempre pelo slot A,
# e a troca de sistema simplesmente nunca acontece.
#
# Então aqui o script é executado como filho, igualzinho, com blkid e mount
# falsos, e o que se confere é o que sobrou em /conf/param.conf.
#
# Rodado por tests/test_slot_boot.py via docker; dá para chamar à mão também.

set -uo pipefail

BASE=/repo/image/stage-project-os/00-project-os/files
TRABALHO=$(mktemp -d)
FALHAS=0

ok()    { echo "  ok    $*"; }
falha() { echo "  FALHA $*"; FALHAS=$((FALHAS + 1)); }

# --- o mundo de mentira -------------------------------------------------------
# blkid acha a FAT; mount entrega um diretório em vez de uma partição; o resto
# não faz nada. É exatamente o que o script enxerga dentro do initramfs.
SHIMS="$TRABALHO/shims"
FAT="$TRABALHO/fat"
mkdir -p "$SHIMS" "$FAT"

cat > "$SHIMS/blkid" <<'SH'
#!/bin/sh
for arg in "$@"; do
    case "$arg" in
        bootfs|LABEL=bootfs) echo "/dev/mmcblk0p1"; exit 0 ;;
    esac
done
exit 2
SH

cat > "$SHIMS/mount" <<SH
#!/bin/sh
# O último argumento é o ponto de montagem. Em vez de montar, aponta para a
# pasta que faz o papel da FAT.
for ultimo in "\$@"; do :; done
mkdir -p "\$ultimo"
cp -a "$FAT/." "\$ultimo/" 2>/dev/null
echo "\$ultimo" > "$TRABALHO/montado"
exit 0
SH

cat > "$SHIMS/umount" <<SH
#!/bin/sh
# Devolve para a "FAT" o que o script escreveu, como um umount de verdade faria.
if [ -f "$TRABALHO/montado" ]; then
    cp -a "\$(cat "$TRABALHO/montado")/." "$FAT/" 2>/dev/null
fi
exit 0
SH

printf '#!/bin/sh\nexit 0\n' > "$SHIMS/sync"
chmod +x "$SHIMS"/*

mkdir -p /usr/share/project-os
cp "$BASE/usr/share/project-os/slot-decide.sh" /usr/share/project-os/

# --- o que o ORDER faz --------------------------------------------------------
# Uma linha para o script, uma linha para o source. Igual ao arquivo gerado pelo
# initramfs-tools, que foi lido de dentro da imagem para escrever isto.
bootar() {
    rm -f /conf/param.conf
    mkdir -p /conf
    ROOT="PARTUUID=5671f673-02"   # o que o cmdline.txt manda, antes de tudo
    PATH="$SHIMS:$PATH" "$BASE/etc/initramfs-tools/scripts/local-top/project-os-slot" >/dev/null 2>&1
    # shellcheck disable=SC1091
    [ -e /conf/param.conf ] && . /conf/param.conf
    echo "$ROOT"
}

estado() {
    cat > "$FAT/project-os-slot.conf" <<CONF
slot=$1
good=$2
tries=$3
recovery=${4:-0}
CONF
}

echo "== estado normal: slot A =="
estado A A 0
RESULTADO=$(bootar)
[ "$RESULTADO" = "/dev/mmcblk0p2" ] \
    && ok "sobe a p2 (slot A)" \
    || falha "esperava /dev/mmcblk0p2, veio '$RESULTADO'"

echo "== depois de uma atualização: slot B =="
estado B A 0
RESULTADO=$(bootar)
[ "$RESULTADO" = "/dev/mmcblk0p3" ] \
    && ok "sobe a p3 (slot B) -- a troca de sistema chega no init" \
    || falha "a escolha do slot B não chegou no init: ROOT ficou '$RESULTADO'"

echo "== a tentativa é contada ANTES do boot =="
estado B A 0
bootar >/dev/null
TRIES=$(sed -n 's/^tries=//p' "$FAT/project-os-slot.conf")
[ "$TRIES" = "1" ] \
    && ok "tries=1 gravado antes de entregar o boot" \
    || falha "tries ficou '$TRIES'; um kernel que trava não volta para contar"

echo "== slot B falhou três vezes: volta para o que funciona =="
estado B A 3
RESULTADO=$(bootar)
[ "$RESULTADO" = "/dev/mmcblk0p2" ] \
    && ok "desiste do slot B e volta para o A sozinho" \
    || falha "não voltou para o slot bom: ROOT ficou '$RESULTADO'"

echo "== sem arquivo de estado (cartão recém-gravado) =="
rm -f "$FAT/project-os-slot.conf"
RESULTADO=$(bootar)
[ "$RESULTADO" = "/dev/mmcblk0p2" ] \
    && ok "sem estado nenhum, sobe o slot A" \
    || falha "sem estado o boot foi para '$RESULTADO'"

echo "== o param.conf é o canal usado, não o export =="
estado B A 0
rm -f /conf/param.conf
PATH="$SHIMS:$PATH" "$BASE/etc/initramfs-tools/scripts/local-top/project-os-slot" >/dev/null 2>&1
grep -q "^ROOT=/dev/mmcblk0p3$" /conf/param.conf 2>/dev/null \
    && ok "ROOT escrito em /conf/param.conf" \
    || falha "o script não escreveu ROOT em /conf/param.conf (o export sozinho não chega em ninguém)"

rm -rf "$TRABALHO"
echo
if [ "$FALHAS" -eq 0 ]; then
    echo "TUDO OK"
    exit 0
fi
echo "$FALHAS FALHA(S)"
exit 1

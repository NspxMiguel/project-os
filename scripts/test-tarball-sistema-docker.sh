#!/bin/bash
# O tarball de sistema que o CI publica, desempacotado num slot de verdade.
#
# É o último elo que nunca tinha sido testado. O "nunca mais gravar o cartão"
# depende inteiro dele: quando ele apertar Atualizar, é este arquivo que vira o
# sistema do slot B, e se ele tiver a forma errada -- uma pasta a mais em volta,
# faltando /etc, com o /boot no lugar errado -- o slot B não sobe. Três boots
# depois o Pi volta atrás sozinho (é para isso que o esquema existe), mas aí
# nenhuma atualização pela rede funcionaria nunca, e a saída seria o cartão no PC.
#
# Este teste roda o ajudante de verdade contra o tarball de verdade, num cartão
# de quatro partições, e depois abre o slot para ver se ficou um sistema.
#
# Uso: test-tarball-sistema-docker.sh /s/project-os-rootfs-0.4.4.tar.gz

set -uo pipefail

TARBALL="${1:?uso: test-tarball-sistema-docker.sh <rootfs.tar.gz>}"
BASE=/repo/image/stage-project-os/00-project-os/files
TRAB=$(mktemp -d)
FALHAS=0

ok()    { echo "  ok    $*"; }
falha() { echo "  FALHA $*"; FALHAS=$((FALHAS + 1)); }

apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq fdisk dosfstools e2fsprogs util-linux >/dev/null 2>&1

install -d -m 755 /usr/share/project-os
for s in layout.sh slot-decide.sh fstab-slot.sh; do
    install -m 755 "$BASE/usr/share/project-os/$s" "/usr/share/project-os/$s"
done
install -m 755 "$BASE/usr/local/sbin/project-os-system-update" /usr/local/sbin/

nodes() {
    local base; base=$(basename "$1")
    for entrada in /sys/block/"$base"/"$base"*/dev; do
        [ -r "$entrada" ] || continue
        local nome maj min
        nome=$(basename "$(dirname "$entrada")")
        maj=$(cut -d: -f1 "$entrada"); min=$(cut -d: -f2 "$entrada")
        [ -b "/dev/$nome" ] || mknod "/dev/$nome" b "$maj" "$min" 2>/dev/null || true
    done
}

echo "== o que o CI publicou =="
TAM=$(stat -c %s "$TARBALL")
echo "  tarball: $((TAM / 1024 / 1024)) MB"

# A forma do tarball importa: um "./" na frente é o normal; uma pasta a mais em
# volta ("project-os-rootfs/usr/...") faria o slot receber um sistema dentro de
# uma pasta, e nada disso subiria.
# A listagem inteira, uma vez só: olhar só o começo do arquivo mede a ordem em
# que o tar gravou, não o que tem dentro.
LISTA="$TRAB/lista.txt"
tar -tzf "$TARBALL" 2>/dev/null | sed 's|^\./||' | cut -d/ -f1 | sort -u | grep -v '^$' > "$LISTA"
echo "  nomes de topo: $(tr '\n' ' ' < "$LISTA")"

FALTOU=""
for exigido in etc usr bin lib sbin var opt boot; do
    grep -qx "$exigido" "$LISTA" || FALTOU="$FALTOU /$exigido"
done
[ -z "$FALTOU" ] && ok "o sistema está na raiz do tarball, inteiro" \
                 || falha "o tarball não tem:$FALTOU"

echo
echo "== desempacotando no slot B, com o ajudante de verdade =="
IMG="$TRAB/cartao.img"
dd if=/dev/zero of="$IMG" bs=1M count=0 seek=20000 status=none
sfdisk --quiet "$IMG" >/dev/null <<'TAB'
label: dos
label-id: 0x5671f673
unit: sectors
1 : start=8192, size=1048576, type=c
2 : start=1056768, size=16777216, type=83
3 : start=17833984, size=16777216, type=83
4 : start=34611200, size=6291456, type=83
TAB
LOOP=$(losetup --show -fP "$IMG")
partprobe "$LOOP" >/dev/null 2>&1 || true
nodes "$LOOP"
mkfs.vfat -n bootfs "${LOOP}p1" >/dev/null 2>&1
mkfs.ext4 -q -F -L pos-rootA "${LOOP}p2" >/dev/null 2>&1
mkfs.ext4 -q -F -L pos-rootB "${LOOP}p3" >/dev/null 2>&1

# A identidade desta caixa, que a atualização tem que levar junto.
install -d -m 700 /etc/NetworkManager/system-connections
echo "psk=a-senha-do-wifi" > /etc/NetworkManager/system-connections/casa.nmconnection
install -d -m 755 /etc/ssh && echo "chave" > /etc/ssh/ssh_host_ed25519_key
grep -q "^project-os:" /etc/shadow 2>/dev/null || \
    echo 'project-os:$6$x$a-senha-dele:20000:0:99999:7:::' >> /etc/shadow

SHIMS="$TRAB/shims"; mkdir -p "$SHIMS"
cat > "$SHIMS/findmnt" <<SH
#!/bin/sh
alvo=""
for a in "\$@"; do alvo="\$a"; done
if [ "\$alvo" = "/" ]; then echo "${LOOP}p2"; exit 0; fi
grep -q "^\$alvo " /proc/mounts || exit 1
awk -v d="\$alvo" '\$1 == d { print \$2; a=1 } END { exit !a }' /proc/mounts
SH
chmod +x "$SHIMS/findmnt"

# O ajudante recebe um .tar, não um .tar.gz -- é o project-os que descompacta
# antes de chamar. Aqui se faz o mesmo, e se mede quanto tempo leva no caminho.
gunzip -c "$TARBALL" > "$TRAB/sistema.tar" 2>/dev/null || falha "não consegui descompactar o tarball"

SAIDA=$(PATH="$SHIMS:$PATH" /usr/local/sbin/project-os-system-update "$TRAB/sistema.tar" B 2>&1)
RC=$?
[ "$RC" = "0" ] && ok "o ajudante gravou o slot B sem erro" \
                || falha "o ajudante saiu com $RC: $(echo "$SAIDA" | tail -3)"

echo
echo "== o slot B, aberto por dentro =="
MNT="$TRAB/mnt"; mkdir -p "$MNT"
mount "${LOOP}p3" "$MNT"

# Um sistema que sobe precisa destes. Faltando qualquer um, o kernel monta a
# raiz e para -- e numa caixa sem tela isso é indistinguível de "não ligou".
# -e segue o link; -L pega o link em si. Aqui a diferença é tudo: /sbin/init é
# um link para /lib/systemd/systemd, e visto de fora do slot esse caminho é
# resolvido contra a raiz deste container, onde ele não existe. Perguntar só com
# -e faria o teste acusar de faltar um init que está lá.
existe() { [ -e "$1" ] || [ -L "$1" ]; }

for caminho in sbin/init etc/fstab etc/passwd usr/bin bin/sh lib; do
    existe "$MNT/$caminho" && ok "/$caminho" || falha "o slot B ficou sem /$caminho"
done

[ -x "$MNT/opt/project-os/bin/project-os" ] \
    && ok "o project-os está lá e é executável" || falha "o slot B ficou sem o project-os"
# O virtualenv: sem ele o serviço não sobe, o slot novo nunca confirma, e toda
# atualização terminaria voltando atrás. O python dele é um link para o python do
# sistema -- de novo, só -L enxerga.
if [ -f "$MNT/opt/project-os/.venv/pyvenv.cfg" ] && existe "$MNT/opt/project-os/.venv/bin/python"; then
    ok "o virtualenv veio junto ($(ls "$MNT/opt/project-os/.venv/lib" 2>/dev/null | head -1))"
else
    falha "o slot B ficou sem o virtualenv (o serviço não subiria)"
fi
[ -x "$MNT/opt/project-os/.venv/bin/uvicorn" ] \
    && ok "o uvicorn está instalado no slot novo" || falha "o slot novo não tem uvicorn"
[ -f "$MNT/etc/systemd/system/project-os.service" ] \
    && ok "o serviço veio junto" || falha "o slot B ficou sem o arquivo de serviço"
[ -f "$MNT/usr/share/project-os/layout.sh" ] && [ -f "$MNT/usr/local/sbin/project-os-system-update" ] \
    && ok "o slot B sabe se atualizar sozinho (não é beco sem saída)" \
    || falha "o slot B veio sem os scripts do esquema de dois sistemas"

grep -q "^PARTUUID=5671f673-03[[:space:]]*/[[:space:]]" "$MNT/etc/fstab" \
    && ok "o fstab aponta para o próprio slot B" \
    || falha "o fstab do slot B: $(grep '[[:space:]]/[[:space:]]' "$MNT/etc/fstab" | head -1)"

[ -f "$MNT/etc/NetworkManager/system-connections/casa.nmconnection" ] \
    && ok "a senha do Wi-Fi atravessou" || falha "perdeu o Wi-Fi (o slot novo subiria sem rede)"
grep -q "a-senha-dele" "$MNT/etc/shadow" \
    && ok "a senha dele atravessou" || falha "perdeu a senha dele"
[ -f "$MNT/etc/project-os-slot-completo" ] \
    && ok "o slot ficou carimbado de completo" || falha "o slot não foi carimbado"

USADO=$(df -m --output=used "$MNT" | tail -1 | tr -d ' ')
echo "  (o sistema ocupa ${USADO} MB no slot)"
[ "$USADO" -lt 7500 ] && ok "cabe folgado num slot de 8 GB" \
                      || falha "o sistema ocupa ${USADO} MB -- perto demais do tamanho do slot"

umount "$MNT"
losetup -d "$LOOP"
rm -rf "$TRAB"

echo
if [ "$FALHAS" -eq 0 ]; then
    echo "TUDO OK"
    exit 0
fi
echo "$FALHAS FALHA(S)"
exit 1

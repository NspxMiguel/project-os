#!/bin/bash
# A vida inteira do cartão, em sequência, num disco de verdade.
#
# Cada peça deste projeto já tinha teste sozinha -- o reparticionamento, a
# escolha do slot, o fstab, o ajudante de estado. O que nunca tinha acontecido é
# elas rodarem **em sequência no mesmo cartão**, que é a única forma que existe
# no mundo real. E o clone-slot nunca tinha rodado nenhuma vez.
#
# É onde mora o defeito que sobra depois de todos os outros: a peça A passa, a
# peça B passa, e a saída de A não é bem o que B esperava. Nenhum teste de
# unidade vê isso.
#
# Então este script faz o cartão viver:
#
#   1. gravação  -- duas partições, como sai do pi-gen
#   2. 1º boot   -- reparticiona e escolhe o slot (initramfs de verdade)
#   3. clone     -- o slot B recebe uma cópia do sistema que está rodando
#   4. update    -- um sistema novo é gravado por cima do slot B
#   5. boot no B -- e a escolha tem que chegar no init
#   6. volta     -- o slot B não confirma três vezes e o cartão volta sozinho
#   7. alterna   -- o slot "bom" para de subir e ele tenta o outro
#
# Rodado por tests/test_ciclo_completo.py via docker.

set -uo pipefail

# Soltar todo loop e desmontar tudo na saída, inclusive em erro. Um loop que
# fica preso não morre com o contêiner: sobra na VM do Docker apontando para um
# arquivo apagado, e o próximo teste que procurar partição por PARTUUID acha a
# do lixo -- foi assim que este projeto acusou um defeito que não existia.
soltar_tudo() {
    for ponto in /mnt/raiz /mnt/boot /mnt/slot /boot/firmware; do
        umount "$ponto" 2>/dev/null || true
    done
    losetup -D 2>/dev/null || true
}
trap soltar_tudo EXIT


BASE=/repo/image/stage-project-os/00-project-os/files
# A área de trabalho fica onde o clone não olha. A imagem do cartão de teste
# não faz parte do sistema de arquivos de um Pi -- deixá-la em /tmp faria o
# clone tentar copiar um arquivo de 20 GB para dentro de um slot de 8 GB, e o
# teste mediria o meu erro em vez do produto. /var/lib/project-os é a partição
# de dados no cartão de verdade, e o clone a exclui por isso mesmo.
install -d -m 755 /var/lib/project-os
TRAB=$(mktemp -d -p /var/lib/project-os)
FALHAS=0

ok()    { echo "  ok    $*"; }
falha() { echo "  FALHA $*"; FALHAS=$((FALHAS + 1)); }
etapa() { echo; echo "== $* =="; }

apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq fdisk dosfstools e2fsprogs parted util-linux rsync >/dev/null 2>&1

# Em container não há udev criando /dev/loopNpM.
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

# Os scripts se chamam por caminho absoluto, como no cartão.
install -d -m 755 /usr/share/project-os
for s in layout.sh slot-decide.sh fstab-slot.sh; do
    install -m 755 "$BASE/usr/share/project-os/$s" "/usr/share/project-os/$s"
done
for s in project-os-system-update project-os-clone-slot project-os-slot-state; do
    install -m 755 "$BASE/usr/local/sbin/$s" "/usr/local/sbin/$s"
done

# --- o sistema que está rodando ------------------------------------------------
# O container faz o papel do slot A ligado: é de "/" que o clone copia e é de
# "/" que a atualização tira a identidade da caixa. Então a identidade mora aqui.
install -d -m 700 /etc/NetworkManager/system-connections
cat > /etc/NetworkManager/system-connections/casa.nmconnection <<'WIFI'
[connection]
id=casa
[wifi-security]
psk=a-senha-do-wifi-dele
WIFI
chmod 600 /etc/NetworkManager/system-connections/casa.nmconnection
install -d -m 755 /etc/ssh
echo "chave-de-host-desta-caixa" > /etc/ssh/ssh_host_ed25519_key
echo "project-os-de-teste" > /etc/hostname
echo "0123456789abcdef0123456789abcdef" > /etc/machine-id
grep -q "^project-os:" /etc/shadow 2>/dev/null || \
    echo 'project-os:$6$aaaa$a-senha-que-ele-criou-na-tela:20000:0:99999:7:::' >> /etc/shadow

# O fstab do sistema que está rodando. Um Pi tem; um container não -- e sem ele
# o clone não teria o que ajustar, o que faria o teste medir a coisa errada.
cat > /etc/fstab <<'FSTA'
proc                  /proc           proc  defaults          0  0
PARTUUID=5671f673-01  /boot/firmware  vfat  defaults          0  2
PARTUUID=5671f673-02  /               ext4  defaults,noatime  0  1
LABEL=pos-data        /var/lib/project-os  ext4  defaults,noatime,nofail  0  2
FSTA

# --- 1. o cartão como sai da gravação -----------------------------------------
etapa "1. gravação: o cartão como sai do pi-gen"
IMG="$TRAB/cartao.img"
dd if=/dev/zero of="$IMG" bs=1M count=0 seek=20000 status=none
sfdisk --quiet "$IMG" >/dev/null <<'TAB'
label: dos
label-id: 0x5671f673
unit: sectors
1 : start=8192, size=1048576, type=c
2 : start=1056768, size=5242880, type=83
TAB
LOOP=$(losetup --show -fP "$IMG")
partprobe "$LOOP" >/dev/null 2>&1 || true
nodes "$LOOP"
mkfs.vfat -n bootfs "${LOOP}p1" >/dev/null 2>&1
mkfs.ext4 -q -F -L rootfs "${LOOP}p2" >/dev/null 2>&1

MNT="$TRAB/mnt"
mkdir -p "$MNT"
mount "${LOOP}p1" "$MNT"
cat > "$MNT/config.txt" <<'CFG'
auto_initramfs=1
CFG
echo "console=tty1 root=PARTUUID=5671f673-02 rootfstype=ext4 fsck.repair=yes rootwait quiet" \
    > "$MNT/cmdline.txt"
umount "$MNT"

mount "${LOOP}p2" "$MNT"
mkdir -p "$MNT/opt/project-os" "$MNT/etc"
echo "o sistema do miguel" > "$MNT/opt/project-os/prova.txt"
cat > "$MNT/etc/fstab" <<'FST'
proc                  /proc           proc  defaults          0  0
PARTUUID=5671f673-01  /boot/firmware  vfat  defaults          0  2
PARTUUID=5671f673-02  /               ext4  defaults,noatime  0  1
LABEL=pos-data        /var/lib/project-os  ext4  defaults,noatime,nofail  0  2
FST
umount "$MNT"
ok "cartão de 20 GB gravado com duas partições"

# --- como o initramfs roda de verdade -----------------------------------------
# O ORDER chama cada script como processo filho e depois faz source do
# param.conf. Reproduzir isso é o ponto: um "export ROOT" não chegaria em ninguém.
bootar() {
    mkdir -p /conf
    rm -f /conf/param.conf
    ROOT="PARTUUID=5671f673-02"
    "$BASE/etc/initramfs-tools/scripts/local-top/project-os-layout" >/dev/null 2>&1
    # shellcheck disable=SC1091
    [ -e /conf/param.conf ] && . /conf/param.conf
    "$BASE/etc/initramfs-tools/scripts/local-top/project-os-slot" >/dev/null 2>&1
    # shellcheck disable=SC1091
    [ -e /conf/param.conf ] && . /conf/param.conf
    echo "$ROOT"
}

estado() {
    mount "${LOOP}p1" "$MNT"
    cat "$MNT/project-os-slot.conf" 2>/dev/null
    umount "$MNT"
}

escrever_estado() {
    # Pelo ajudante de verdade, que é quem o project-os usa.
    mkdir -p /boot/firmware
    mount "${LOOP}p1" /boot/firmware
    /usr/local/sbin/project-os-slot-state "$@" >/dev/null 2>&1
    local rc=$?
    umount /boot/firmware
    return $rc
}

# --- 2. primeiro boot ---------------------------------------------------------
etapa "2. primeiro boot: reparticiona e escolhe o slot"
RAIZ=$(bootar)
partprobe "$LOOP" >/dev/null 2>&1 || true
nodes "$LOOP"

PARTES=$(sfdisk -d "$LOOP" | grep -c "^${LOOP}p[0-9]")
[ "$PARTES" = "4" ] && ok "o cartão virou quatro partições" \
                    || falha "esperava 4 partições, achei $PARTES"

[ "$RAIZ" = "${LOOP}p2" ] && ok "o init recebeu a p2 (slot A)" \
                          || falha "o init recebeu '$RAIZ'"

[ "$(sfdisk --disk-id "$LOOP")" = "0x5671f673" ] \
    && ok "o identificador do disco sobreviveu" \
    || falha "o identificador do disco mudou"

echo "$(estado)" | grep -q "slot=A" && ok "o estado inicial ficou gravado na FAT" \
                                    || falha "não gravou o estado inicial"

mount "${LOOP}p2" "$MNT"
[ -f "$MNT/opt/project-os/prova.txt" ] && ok "os arquivos da raiz sobreviveram" \
                                       || falha "a raiz perdeu os arquivos"
umount "$MNT"

[ "$(blkid -o value -s LABEL "${LOOP}p3")" = "pos-rootB" ] \
    && ok "slot B formatado e rotulado" || falha "slot B não ficou pronto"
[ "$(blkid -o value -s LABEL "${LOOP}p4")" = "pos-data" ] \
    && ok "partição de dados formatada" || falha "dados não ficaram prontos"

# --- 3. o clone para o slot B -------------------------------------------------
etapa "3. clone: o slot B recebe uma cópia do que está rodando"
# findmnt de mentira: a raiz é a p2 deste cartão. É a única coisa fingida --
# o rsync, o mkfs e o fstab são de verdade.
SHIMS="$TRAB/shims"
mkdir -p "$SHIMS"
cat > "$SHIMS/findmnt" <<SH
#!/bin/sh
alvo=""
for a in "\$@"; do alvo="\$a"; done
if [ "\$alvo" = "/" ]; then echo "${LOOP}p2"; exit 0; fi
grep -q "^\$alvo " /proc/mounts || exit 1
awk -v d="\$alvo" '\$1 == d { print \$2; achou=1 } END { exit !achou }' /proc/mounts
SH
chmod +x "$SHIMS/findmnt"

rm -f /var/lib/project-os/.slot-clonado
PATH="$SHIMS:$PATH" /usr/local/sbin/project-os-clone-slot >/dev/null 2>&1
RC=$?
[ "$RC" = "0" ] && ok "o clone rodou sem erro" || falha "o clone saiu com código $RC"

mount "${LOOP}p3" "$MNT"
[ -d "$MNT/usr" ] && [ -d "$MNT/etc" ] \
    && ok "o slot B tem um sistema dentro" || falha "o slot B continuou vazio"
[ -f "$MNT/etc/project-os-slot-completo" ] \
    && ok "o slot B ficou carimbado de completo" \
    || falha "o clone terminou sem carimbar o slot B"
grep -q "^PARTUUID=5671f673-03[[:space:]]*/[[:space:]]" "$MNT/etc/fstab" \
    && ok "o fstab do slot B aponta para o próprio slot B" \
    || falha "o fstab do slot B ficou: $(grep '[[:space:]]/[[:space:]]' "$MNT/etc/fstab" | head -1)"
grep -q "pos-data" "$MNT/etc/fstab" \
    && ok "o slot B continua montando a partição de dados" \
    || falha "o clone perdeu a linha da partição de dados"
umount "$MNT"

[ -f /var/lib/project-os/.slot-clonado ] \
    && ok "a marca de 'já clonei' ficou gravada" || falha "não gravou a marca do clone"

# Roda de novo: não pode copiar por cima -- este slot é o caminho de volta.
rm -f /var/lib/project-os/.slot-clonado
SAIDA=$(PATH="$SHIMS:$PATH" /usr/local/sbin/project-os-clone-slot 2>&1)
echo "$SAIDA" | grep -qi "completo" \
    && ok "rodar de novo reconhece o slot pronto e não mexe nele" \
    || falha "rodar de novo não reconheceu o slot pronto: $SAIDA"

# --- 3b. o defeito que este teste existe para pegar --------------------------
# Uma cópia interrompida no meio deixa /usr e /etc no lugar. Se a pergunta for
# "tem /usr e /etc?", este meio sistema passa por sistema inteiro: o clone marca
# "já clonei", nunca mais tenta, e a caixa fica contando com um caminho de volta
# que não sobe. No dia em que o slot de hoje parar, o initramfs alterna para
# esse lixo, ele também não sobe, e aí não há mais de onde voltar.
etapa "3b. um slot com meio sistema não pode passar por pronto"
mount "${LOOP}p3" "$MNT"
rm -f "$MNT/etc/project-os-slot-completo"      # o que uma cópia interrompida deixa
rm -rf "$MNT/opt"
umount "$MNT"
rm -f /var/lib/project-os/.slot-clonado

SAIDA=$(PATH="$SHIMS:$PATH" /usr/local/sbin/project-os-clone-slot 2>&1)
echo "$SAIDA" | grep -qi "clonando" \
    && ok "reconhece o meio sistema e copia de novo" \
    || falha "deu por pronto um slot pela metade: $SAIDA"

mount "${LOOP}p3" "$MNT"
[ -f "$MNT/etc/project-os-slot-completo" ] \
    && ok "e desta vez terminou e carimbou" || falha "a segunda cópia também não carimbou"
umount "$MNT"

# --- 4. a atualização ---------------------------------------------------------
etapa "4. atualização: um sistema novo por cima do slot B"
FABRICA="$TRAB/fabrica"
mkdir -p "$FABRICA/etc" "$FABRICA/usr/bin" "$FABRICA/opt/project-os"
cat > "$FABRICA/etc/fstab" <<'FST'
proc                  /proc           proc  defaults          0  0
PARTUUID=5671f673-01  /boot/firmware  vfat  defaults          0  2
PARTUUID=5671f673-02  /               ext4  defaults,noatime  0  1
LABEL=pos-data        /var/lib/project-os  ext4  defaults,noatime,nofail  0  2
FST
# O shadow de fábrica: tem a conta, mas não tem a senha dele.
echo 'project-os:!:20000:0:99999:7:::' > "$FABRICA/etc/shadow"
echo "versao-nova" > "$FABRICA/opt/project-os/prova.txt"
TARBALL="$TRAB/sistema-novo.tar"
tar -C "$FABRICA" -cf "$TARBALL" .

PATH="$SHIMS:$PATH" /usr/local/sbin/project-os-system-update "$TARBALL" B >/dev/null 2>&1
RC=$?
[ "$RC" = "0" ] && ok "a atualização rodou sem erro" || falha "a atualização saiu com código $RC"

mount "${LOOP}p3" "$MNT"
[ "$(cat "$MNT/opt/project-os/prova.txt" 2>/dev/null)" = "versao-nova" ] \
    && ok "o sistema novo está no slot B" || falha "o slot B não recebeu o sistema novo"
grep -q "^PARTUUID=5671f673-03[[:space:]]*/[[:space:]]" "$MNT/etc/fstab" \
    && ok "o fstab do sistema novo aponta para o slot B" \
    || falha "o fstab do sistema novo não foi ajustado"

# A identidade da caixa: sem isto, um Pi no Wi-Fi sobe sem rede e volta atrás.
[ -f "$MNT/etc/NetworkManager/system-connections/casa.nmconnection" ] \
    && ok "a senha do Wi-Fi atravessou a atualização" || falha "perdeu a senha do Wi-Fi"
[ -f "$MNT/etc/ssh/ssh_host_ed25519_key" ] \
    && ok "a chave de host do SSH atravessou" || falha "perdeu a chave de host do SSH"
grep -q "a-senha-que-ele-criou-na-tela" "$MNT/etc/shadow" \
    && ok "a senha que ele criou na tela atravessou" || falha "perdeu a senha dele"
grep -c "^project-os:" "$MNT/etc/shadow" | grep -qx "1" \
    && ok "a conta dele não ficou duplicada no shadow" \
    || falha "o shadow ficou com $(grep -c '^project-os:' "$MNT/etc/shadow") linhas project-os"
[ "$(cat "$MNT/etc/machine-id" 2>/dev/null)" = "0123456789abcdef0123456789abcdef" ] \
    && ok "a identidade da máquina atravessou" || falha "perdeu o machine-id"
[ -f "$MNT/etc/project-os-slot-completo" ] \
    && ok "a atualização carimbou o slot B de completo" \
    || falha "a atualização não carimbou o slot; o clone passaria por cima dele"
umount "$MNT"

# O slot que está rodando não pode ser tocado nunca.
PATH="$SHIMS:$PATH" /usr/local/sbin/project-os-system-update "$TARBALL" A >/dev/null 2>&1 \
    && falha "aceitou formatar o slot que está rodando" \
    || ok "recusou formatar o slot que está rodando"
mount "${LOOP}p2" "$MNT"
[ "$(cat "$MNT/opt/project-os/prova.txt" 2>/dev/null)" = "o sistema do miguel" ] \
    && ok "o slot A ficou intacto" || falha "o slot A foi mexido"
umount "$MNT"

# --- 5. reiniciar no slot B ---------------------------------------------------
etapa "5. o boot vai para o slot B"
escrever_estado slot=B tries=0 && ok "o ajudante gravou slot=B na FAT" \
                               || falha "o ajudante não conseguiu gravar na FAT"
RAIZ=$(bootar)
[ "$RAIZ" = "${LOOP}p3" ] \
    && ok "o init recebeu a p3 -- a atualização chegou no boot" \
    || falha "o init recebeu '$RAIZ'; a troca de sistema não aconteceu"

echo "$(estado)" | grep -q "tries=1" \
    && ok "a tentativa foi contada antes de entregar o boot" \
    || falha "tries não foi contado: $(estado | tr '\n' ' ')"

# --- 6. o slot B não confirma: volta sozinho ----------------------------------
etapa "6. o slot B não confirma três vezes"
bootar >/dev/null   # 2ª
bootar >/dev/null   # 3ª
RAIZ=$(bootar)
[ "$RAIZ" = "${LOOP}p2" ] \
    && ok "o cartão voltou sozinho para o slot que funciona" \
    || falha "não voltou atrás: o init recebeu '$RAIZ'"

# --- 7. o slot "bom" também para de subir -------------------------------------
etapa "7. o slot bom também para de subir"
escrever_estado slot=A good=A tries=3
RAIZ=$(bootar)
[ "$RAIZ" = "${LOOP}p3" ] \
    && ok "tenta o outro slot em vez de insistir no mesmo" \
    || falha "ficou preso no mesmo slot ('$RAIZ'): laço infinito com um sistema bootável ao lado"

# --- 8. confirmar o slot novo -------------------------------------------------
etapa "8. o sistema novo se apresenta"
escrever_estado slot=B good=B tries=0
RAIZ=$(bootar)
[ "$RAIZ" = "${LOOP}p3" ] && ok "confirmado, o slot B é o sistema da casa agora" \
                          || falha "o slot confirmado não subiu"

umount "$MNT" 2>/dev/null
losetup -d "$LOOP"
rm -rf "$TRAB"

echo
if [ "$FALHAS" -eq 0 ]; then
    echo "TUDO OK"
    exit 0
fi
echo "$FALHAS FALHA(S)"
exit 1

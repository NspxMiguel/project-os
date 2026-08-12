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

# O firmware do Pi escolhe o kernel (o 3B sobe o kernel7.img) e, com
# auto_initramfs=1, carrega o initramfs que combina com ele. Uma linha
# "initramfs <arquivo>" no config.txt atropela isso e força um arquivo só para
# todos os kernels -- foi assim que a v0.4.1 travou: o arquivo forçado tinha os
# módulos do kernel de 64 bits e o Pi 3B ficou esperando para sempre um cartão
# SD cujo driver não estava lá dentro.
grep -qE "^[[:space:]]*initramfs[[:space:]]+[^=]" /mnt/boot/config.txt \
    && falha "config.txt força um initramfs só (atropela o auto_initramfs)" \
    || ok "config.txt não força initramfs nenhum"

grep -q "^auto_initramfs=1" /mnt/boot/config.txt \
    && ok "auto_initramfs=1 ligado" \
    || falha "auto_initramfs=1 não está no config.txt"

# O primeiro boot do Raspberry Pi OS sorteia um identificador de disco novo e
# reescreve o MBR. Junto com o nosso reparticionamento, no mesmo boot, é como se
# perde um cartão.
grep -q "raspberrypi-sys-mods/firstboot" /mnt/boot/cmdline.txt \
    && falha "o cmdline.txt ainda chama o firstboot do Raspberry Pi OS" \
    || ok "o firstboot do Raspberry Pi OS foi removido do cmdline.txt"

# Um initramfs por kernel, e cada um tem que levar os nossos scripts E os
# módulos do seu próprio kernel.
for PAR in "initramfs:v6" "initramfs7:v7" "initramfs7l:v7l" "initramfs8:v8"; do
    ARQ="${PAR%%:*}"; SUF="${PAR##*:}"
    if [ ! -f "/mnt/boot/$ARQ" ]; then
        falha "não tem $ARQ na partição de boot"
        continue
    fi
    LISTA=$(mktemp)
    lsinitramfs "/mnt/boot/$ARQ" > "$LISTA" 2>/dev/null || true
    if [ ! -s "$LISTA" ]; then
        falha "não consegui abrir $ARQ para conferir o conteúdo"
        rm -f "$LISTA"
        continue
    fi
    FALTOU=""
    for peca in slot-decide.sh layout.sh project-os-slot project-os-layout \
                sfdisk mkfs.ext4 e2fsck resize2fs e2label blkid; do
        grep -q "$peca" "$LISTA" || FALTOU="$FALTOU $peca"
    done
    grep -q "lib/modules/[^/]*-rpi-${SUF}/" "$LISTA" || FALTOU="$FALTOU módulos-$SUF"
    [ -z "$FALTOU" ] && ok "$ARQ completo ($(du -h "/mnt/boot/$ARQ" | cut -f1))" \
                     || falha "$ARQ saiu sem:$FALTOU"
    rm -f "$LISTA"
done

# O kernel7 é o que o Pi 3B dele carrega. Se só um puder estar certo, é este.
[ -f /mnt/boot/kernel7.img ] && ok "kernel7.img presente (é o que o Pi 3B sobe)" \
                            || falha "não tem kernel7.img na imagem"

# O initramfs do kernel7, aberto por dentro: é o que vai rodar no Pi dele.
#
# O ORDER chama cada script de local-top como processo filho e depois faz source
# de /conf/param.conf. Um "export ROOT" morreria com o filho e o Pi subiria
# sempre a p2 -- toda atualização gravaria o slot B e todo boot subiria o slot A,
# em silêncio. Então se confere o canal, no arquivo que vai no cartão.
rm -rf /tmp/dentro && mkdir -p /tmp/dentro
if unmkinitramfs /mnt/boot/initramfs7 /tmp/dentro >/dev/null 2>&1; then
    DENTRO=$(dirname "$(find /tmp/dentro -maxdepth 3 -type f -name init | head -1)")
    ESCOLHA="$DENTRO/scripts/local-top/project-os-slot"
    if [ -f "$ESCOLHA" ]; then
        grep -q 'ROOT=$ROOT" >> /conf/param.conf' "$ESCOLHA" \
            && ok "a escolha do slot chega no init pelo /conf/param.conf" \
            || falha "o script de slot não escreve ROOT em /conf/param.conf (a troca de sistema não aconteceria)"
    else
        falha "não achei o script de slot dentro do initramfs7"
    fi

    ORDEM="$DENTRO/scripts/local-top/ORDER"
    if [ -f "$ORDEM" ]; then
        grep -q "project-os-layout" "$ORDEM" && grep -q "project-os-slot" "$ORDEM" \
            && ok "os dois scripts estão na ordem de execução do initramfs" \
            || falha "o ORDER do initramfs não chama os nossos scripts"
        # O reparticionamento tem que vir antes da escolha: no primeiro boot não
        # existe slot B para escolher até ele ser criado.
        awk '/project-os-layout/ { l = NR } /project-os-slot/ { s = NR } END { exit !(l && s && l < s) }' "$ORDEM" \
            && ok "o reparticionamento roda antes da escolha do slot" \
            || falha "a ordem dos scripts no initramfs está trocada"
    else
        falha "não achei o ORDER dentro do initramfs7"
    fi
else
    falha "não consegui abrir o initramfs7 para conferir os scripts por dentro"
fi

umount /mnt/boot

echo "== o sistema dentro da imagem =="
mount "${LOOP}p2" /mnt/raiz
for arquivo in \
    /usr/share/project-os/layout.sh \
    /usr/share/project-os/slot-decide.sh \
    /usr/share/project-os/fstab-slot.sh \
    /usr/local/sbin/project-os-system-update \
    /usr/local/sbin/project-os-slot-state \
    /usr/local/sbin/project-os-clone-slot \
    /etc/systemd/system/project-os-clone-slot.service \
    /opt/project-os/project_os/core/slots.py \
    /opt/project-os/project_os/core/sysupdate.py
do
    [ -e "/mnt/raiz$arquivo" ] && ok "$arquivo" || falha "faltou $arquivo"
done

# A partição de dados tem que estar no fstab, senão ela é criada, formatada e
# nunca montada -- e uma troca de slot cairia numa tela de primeira configuração
# com os dados dele órfãos na p4.
grep -q "pos-data" /mnt/raiz/etc/fstab \
    && ok "o fstab monta a partição de dados" \
    || falha "o fstab não monta a partição de dados (pos-data)"

# A raiz precisa continuar no fstab: o cmdline.txt não tem "rw", então quem
# remonta a raiz para escrita é o systemd-remount-fs lendo esta linha.
awk '$1 !~ /^#/ && $2 == "/" { achou = 1 } END { exit !achou }' /mnt/raiz/etc/fstab \
    && ok "o fstab tem a linha da raiz (sem ela o sistema sobe somente leitura)" \
    || falha "o fstab não tem linha para a raiz"

# A identidade da caixa tem que atravessar a atualização: sem o Wi-Fi, o slot
# novo sobe sem rede, nunca confirma, e três boots depois volta atrás -- toda
# atualização por Wi-Fi terminaria em rollback.
# -F: procura o texto, não uma expressão regular. Sem isso o "^" de
# "^project-os:" vira âncora de início de linha e a busca só acha o que estiver
# na primeira coluna -- no script essa string mora no meio de uma linha, dentro
# de aspas. O resultado era acusar a imagem de não levar a senha dele quando ela
# leva. É o mesmo alarme falso que este verificador já deu ao procurar
# "NoNewPrivileges" e achar o comentário que explica por que ele não está lá:
# aqui se confere conteúdo, então a busca é literal.
FALTOU_ID=""
for peca in "NetworkManager/system-connections" "ssh_host_ed25519_key" "^project-os:"; do
    grep -qF -- "$peca" /mnt/raiz/usr/local/sbin/project-os-system-update || FALTOU_ID="$FALTOU_ID $peca"
done
[ -z "$FALTOU_ID" ] \
    && ok "a atualização leva Wi-Fi, senha e chaves de SSH para o slot novo" \
    || falha "o ajudante de atualização não leva:$FALTOU_ID"

# O carimbo de "este slot está inteiro".
#
# O slot A tem que sair de fábrica carimbado. Sem isso, no dia em que o Pi
# estiver rodando o slot B, o clone olharia para o slot A, não veria carimbo
# nenhum, e copiaria por cima do sistema antigo -- que é o caminho de volta.
[ -f /mnt/raiz/etc/project-os-slot-completo ] \
    && ok "o slot A sai de fábrica carimbado de completo" \
    || falha "falta o carimbo de slot completo (o clone passaria por cima do caminho de volta)"

# E os dois que escrevem um slot têm que carimbar no fim.
grep -qF "project-os-slot-completo" /mnt/raiz/usr/local/sbin/project-os-clone-slot \
    && ok "o clone carimba o slot no fim da cópia" \
    || falha "o clone não carimba o slot"
grep -qF "project-os-slot-completo" /mnt/raiz/usr/local/sbin/project-os-system-update \
    && ok "a atualização carimba o slot no fim" \
    || falha "a atualização não carimba o slot"

# Um rsync que sai 24 ("sumiu arquivo durante a cópia") copiou tudo que importa:
# num sistema ligado isso é o normal, e tratar como falha abortaria a cópia.
grep -qF '"$CODIGO" -ne 24' /mnt/raiz/usr/local/sbin/project-os-clone-slot \
    && ok "o clone aceita o código 24 do rsync" \
    || falha "o clone trata 'sumiu arquivo' como falha (abortaria quase toda cópia)"

# O arquivo de Wi-Fi do cartão é lido por um script que precisa aguentar o
# editor que ele usar. Um BOM invisível no começo colava-se na primeira chave e
# fazia o arquivo inteiro ser ignorado: o Pi liga, não entra na rede, e não tem
# como avisar ninguém.
grep -qF 'key=${key#$' /mnt/raiz/usr/local/sbin/project-os-firstboot \
    && ok "o Wi-Fi do cartão aguenta arquivo com BOM" \
    || falha "o script de Wi-Fi não tira o BOM (um arquivo salvo no editor errado some com o Pi da rede)"

# Um slot recém-instalado só pode se dar por bom depois de aparecer na rede.
grep -qF "def slot_por_provar" /mnt/raiz/opt/project-os/project_os/core/slots.py \
    && ok "um slot novo só confirma depois de aparecer na rede" \
    || falha "falta a trava de rede: uma atualização que derrube a rede se daria por boa"

# rsync é quem clona o sistema para o slot reserva.
[ -x /mnt/raiz/usr/bin/rsync ] && ok "rsync instalado (clona o slot B)" \
                              || falha "rsync não está na imagem; o slot B ficaria vazio"

grep -q "project-os-system-update" /mnt/raiz/etc/sudoers.d/010_project-os \
    && ok "o sudoers libera o ajudante de sistema" \
    || falha "o sudoers não libera o ajudante de sistema"

# Sem esta, o project-os não consegue gravar qual slot sobe: a FAT é do root.
grep -q "project-os-slot-state" /mnt/raiz/etc/sudoers.d/010_project-os \
    && ok "o sudoers libera o ajudante de estado dos slots" \
    || falha "o sudoers não libera o ajudante de estado (a troca de slot não seria gravada)"

# A pasta do código é do serviço, mas a troca de versão acontece na pasta de
# cima: /opt/.project_os-update-xxxx, depois duas renomeações. Com /opt no padrão
# do Debian (root:root 755) a atualização do app morre com "Permission denied"
# depois de baixar o pacote -- e a caixa só se conserta baixando um rootfs
# inteiro.
GRUPO_OPT=$(stat -c '%G' /mnt/raiz/opt)
MODO_OPT=$(stat -c '%a' /mnt/raiz/opt)
# 0$MODO_OPT: a mode é octal, e o "0" na frente é o que faz o shell ler como tal.
GRUPO_ESCREVE=$(( ((0$MODO_OPT / 8) % 8) & 2 ))
if [ "$GRUPO_OPT" = "project-os" ] && [ "$GRUPO_ESCREVE" -ne 0 ]; then
    ok "/opt deixa o grupo project-os escrever ($GRUPO_OPT $MODO_OPT)"
else
    falha "/opt não deixa o serviço trocar a pasta do código ($GRUPO_OPT $MODO_OPT)"
fi

[ "$(stat -c '%U' /mnt/raiz/opt/project-os)" = "project-os" ] \
    && ok "a pasta do código é do serviço" \
    || falha "/opt/project-os não é do usuário project-os"

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

ID_ANTES=$(sfdisk --disk-id "$LOOP")
bash /tmp/layout.sh "$LOOP" || falha "o reparticionamento falhou"
partprobe "$LOOP" >/dev/null 2>&1 || true
nodes "$LOOP"

PARTES=$(sfdisk -d "$LOOP" | grep -c "^${LOOP}p[0-9]")
[ "$PARTES" = "4" ] && ok "quatro partições depois do primeiro boot" \
                    || falha "esperava 4 partições, achei $PARTES"

mount "${LOOP}p1" /mnt/boot
[ -f /mnt/boot/kernel7.img ] && [ -f /mnt/boot/initramfs7 ] && [ -f /mnt/boot/cmdline.txt ] \
    && ok "o boot continua intacto depois de reparticionar" \
    || falha "o boot se perdeu no reparticionamento"
umount /mnt/boot

# Os PARTUUID do cmdline.txt e do fstab derivam do identificador do disco. Se o
# reparticionamento o trocar, as duas referências apontam para o nada.
[ "$(sfdisk --disk-id "$LOOP")" = "$ID_ANTES" ] \
    && ok "o identificador do disco sobreviveu ($ID_ANTES)" \
    || falha "o identificador do disco mudou: era $ID_ANTES, virou $(sfdisk --disk-id "$LOOP")"

ESPERADO="${ID_ANTES#0x}"
[ "$(blkid -o value -s PARTUUID "${LOOP}p2")" = "${ESPERADO}-02" ] \
    && ok "o PARTUUID da raiz continua o mesmo do cmdline.txt" \
    || falha "o PARTUUID da raiz mudou (o cmdline.txt aponta para o nada)"
[ "$(blkid -o value -s PARTUUID "${LOOP}p1")" = "${ESPERADO}-01" ] \
    && ok "o PARTUUID do boot continua o mesmo do fstab" \
    || falha "o PARTUUID do boot mudou (o /boot/firmware não montaria)"

mount "${LOOP}p2" /mnt/raiz
[ -f /mnt/raiz/opt/project-os/project_os/core/slots.py ] \
    && ok "o sistema sobreviveu ao resize" \
    || falha "o sistema se perdeu no resize"
TAM=$(df -m --output=size /mnt/raiz | tail -1 | tr -d ' ')
[ "$TAM" -gt 6000 ] && ok "o sistema A ficou com ${TAM} MB" || falha "sistema A ficou com ${TAM} MB"
umount /mnt/raiz

[ "$(blkid -o value -s LABEL "${LOOP}p3")" = "pos-rootB" ] && ok "sistema B pronto para receber" \
    || falha "sistema B não foi formatado"
[ "$(blkid -o value -s LABEL "${LOOP}p4")" = "pos-data" ] && ok "partição de dados pronta" \
    || falha "partição de dados não foi formatada"

losetup -d "$LOOP"

echo
if [ "$FALHAS" -eq 0 ]; then
    echo "TUDO OK"
    exit 0
fi
echo "$FALHAS FALHA(S)"
exit 1

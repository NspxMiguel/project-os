#!/bin/sh
# Transforma o cartão recém-gravado (duas partições) no cartão de dois sistemas.
#
# Roda uma única vez, no primeiro boot, **de dentro do initramfs** -- que é o
# único momento em que a raiz ainda não está montada. Reparticionar um disco
# montado por baixo do sistema que está rodando nele não é algo que se faça com
# cuidado suficiente; aqui não existe esse problema.
#
# De:                          Para:
#   p1  boot   FAT               p1  boot   FAT     (intocada)
#   p2  root   ext4  ~2,5G       p2  rootA  ext4    (cresce, mesmos dados)
#                                p3  rootB  ext4    (vazia, clonada depois)
#                                p4  data   ext4    (o resto)
#
# Regras que este script segue, e o motivo de cada uma:
#
# 1. **Nada é reduzido.** p2 só cresce, e cresce para dentro de espaço que não
#    era de ninguém. Encolher sistema de arquivos é a operação que perde dados.
# 2. **p1 não é tocada.** É de onde o Pi sobe. Mexer nela é a única forma de
#    transformar isto num tijolo.
# 3. **Na dúvida, não faz.** Qualquer coisa fora do esperado -- tabela diferente,
#    disco pequeno demais, ferramenta faltando -- sai sem alterar nada. O cartão
#    continua funcionando como sempre funcionou, com um sistema só.
#
# Uso:  layout.sh <disco>        ex.: layout.sh /dev/mmcblk0
#       layout.sh <disco> --dry  imprime o que faria e não faz nada

set -u

SLOT_MB_ALVO=8192      # tamanho de cada sistema, quando o cartão permite
SLOT_MB_MINIMO=3072    # abaixo disso não vale a pena: um sistema não cabe
DATA_MB_MINIMO=1024    # e ainda tem que sobrar espaço para os dados

DISCO="${1:-}"
DRY="${2:-}"

aviso() { echo "project-os-layout: $*" >&2; }

[ -n "$DISCO" ] || { aviso "uso: layout.sh <disco>"; exit 2; }
[ -b "$DISCO" ] || [ -f "$DISCO" ] || { aviso "$DISCO não é um disco"; exit 2; }

for ferramenta in sfdisk mkfs.ext4 e2fsck resize2fs blkid; do
    command -v "$ferramenta" >/dev/null 2>&1 || {
        aviso "falta $ferramenta; deixando o cartão como está"
        exit 3
    }
done

# O separador do número da partição: mmcblk0p2, mas sda2.
case "$DISCO" in
    *[0-9]) SEP=p ;;
    *)      SEP=  ;;
esac
parte() { echo "${DISCO}${SEP}${1}"; }

SETOR=512

partes_existentes=$(sfdisk -d "$DISCO" 2>/dev/null | grep -c "^${DISCO}${SEP}[0-9]")
if [ "$partes_existentes" -ge 4 ]; then
    aviso "o cartão já tem $partes_existentes partições; nada a fazer"
    exit 0
fi
if [ "$partes_existentes" -ne 2 ]; then
    aviso "esperava 2 partições, achei $partes_existentes; deixando como está"
    exit 3
fi

linha_de() { sfdisk -d "$DISCO" 2>/dev/null | grep "^$(parte "$1") "; }
campo() { echo "$1" | sed -n "s/.*$2=[[:space:]]*\([0-9A-Za-z]*\).*/\1/p"; }

L1=$(linha_de 1); L2=$(linha_de 2)
P1_INICIO=$(campo "$L1" start); P1_TAM=$(campo "$L1" size); P1_TIPO=$(campo "$L1" type)
P2_INICIO=$(campo "$L2" start); P2_TAM=$(campo "$L2" size)

for valor in "$P1_INICIO" "$P1_TAM" "$P2_INICIO" "$P2_TAM"; do
    case "$valor" in
        ''|*[!0-9]*) aviso "não entendi a tabela de partições; deixando como está"; exit 3 ;;
    esac
done

# p1 tem que ser a FAT de boot e p2 a raiz. Se não for, este não é o nosso
# cartão e não há nada que possamos fazer com segurança.
if [ "$(blkid -o value -s TYPE "$(parte 1)" 2>/dev/null)" != "vfat" ]; then
    aviso "a primeira partição não é FAT; deixando como está"
    exit 3
fi

TOTAL_SETORES=$(blockdev --getsz "$DISCO" 2>/dev/null || echo 0)
if [ "$TOTAL_SETORES" -eq 0 ]; then
    TAMANHO_BYTES=$(sfdisk -s "$DISCO" 2>/dev/null || echo 0)   # em KiB
    TOTAL_SETORES=$((TAMANHO_BYTES * 2))
fi
[ "$TOTAL_SETORES" -gt 0 ] || { aviso "não sei o tamanho do disco"; exit 3; }

mb_para_setores() { echo $(($1 * 1024 * 1024 / SETOR)); }
setores_para_mb() { echo $(($1 * SETOR / 1024 / 1024)); }

DISPONIVEL=$((TOTAL_SETORES - P2_INICIO))
SLOT_SETORES=$(mb_para_setores "$SLOT_MB_ALVO")
DATA_MINIMO_SETORES=$(mb_para_setores "$DATA_MB_MINIMO")

# Cabe o alvo? Se não, divide o que existe: dois slots iguais e o resto de dados.
if [ $((SLOT_SETORES * 2 + DATA_MINIMO_SETORES)) -gt "$DISPONIVEL" ]; then
    SLOT_SETORES=$(((DISPONIVEL - DATA_MINIMO_SETORES) / 2))
fi
if [ "$(setores_para_mb "$SLOT_SETORES")" -lt "$SLOT_MB_MINIMO" ]; then
    aviso "cartão pequeno demais para dois sistemas; deixando com um só"
    exit 3
fi
# Um slot nunca pode ficar menor que o que já está em uso na raiz de hoje.
if [ "$SLOT_SETORES" -lt "$P2_TAM" ]; then
    SLOT_SETORES="$P2_TAM"
fi

# Alinhamento de 4 MiB: cartão SD escreve em blocos grandes, e uma partição
# desalinhada custa desempenho para sempre.
ALINHAMENTO=$(mb_para_setores 4)
alinhar() { echo $(( ($1 / ALINHAMENTO) * ALINHAMENTO )); }

SLOT_SETORES=$(alinhar "$SLOT_SETORES")
P3_INICIO=$(alinhar $((P2_INICIO + SLOT_SETORES)))
P4_INICIO=$(alinhar $((P3_INICIO + SLOT_SETORES)))
P4_TAM=$(alinhar $((TOTAL_SETORES - P4_INICIO)))

if [ "$P4_TAM" -lt "$DATA_MINIMO_SETORES" ]; then
    aviso "não sobra espaço para os dados; deixando como está"
    exit 3
fi

# O identificador do disco (label-id) TEM que ser preservado.
#
# O sfdisk sorteia um novo quando a tabela não diz qual é -- e o PARTUUID de
# cada partição é derivado dele. O cmdline.txt manda o kernel montar
# root=PARTUUID=<id>-02 e o fstab monta /boot/firmware por PARTUUID=<id>-01;
# trocar o id apaga as duas referências de uma vez. O Pi passaria a depender
# exclusivamente do nosso initramfs para achar a raiz, e a partição de boot
# simplesmente não montaria. Foi medido: sem esta linha o id mudava de
# 5671f673 para outro a cada reparticionamento.
LABEL_ID=$(sfdisk -d "$DISCO" 2>/dev/null | sed -n 's/^label-id:[[:space:]]*//p' | head -n 1)
[ -n "$LABEL_ID" ] && LINHA_ID="label-id: $LABEL_ID" || LINHA_ID=""

TABELA=$(cat <<TAB
label: dos
${LINHA_ID}
unit: sectors
$(parte 1) : start=$P1_INICIO, size=$P1_TAM, type=$P1_TIPO
$(parte 2) : start=$P2_INICIO, size=$SLOT_SETORES, type=83
$(parte 3) : start=$P3_INICIO, size=$SLOT_SETORES, type=83
$(parte 4) : start=$P4_INICIO, size=$P4_TAM, type=83
TAB
)

if [ "$DRY" = "--dry" ]; then
    echo "$TABELA"
    exit 0
fi

aviso "reparticionando: dois sistemas de $(setores_para_mb "$SLOT_SETORES") MB e $(setores_para_mb "$P4_TAM") MB de dados"

# A partir daqui a tabela muda. O começo de p1 e de p2 é o mesmo de antes, então
# os dados de ambas continuam onde estavam -- é só a tabela que passa a dizer
# que p2 é maior e que existem p3 e p4.
echo "$TABELA" | sfdisk --no-reread --force "$DISCO" >/dev/null 2>&1 || {
    aviso "sfdisk falhou; o cartão continua como estava"
    exit 4
}
sync
partprobe "$DISCO" >/dev/null 2>&1 || partx -u "$DISCO" >/dev/null 2>&1 || true

# O kernel já sabe das partições novas; /dev pode não saber ainda. Quem costuma
# criar esses arquivos é o udev, e aqui dentro do initramfs ele pode não estar
# ouvindo -- então criamos nós mesmos, a partir do que o kernel diz em /sys.
# Sem isso o mkfs do slot B falha com "não existe esse arquivo" logo depois de a
# partição ter sido criada com sucesso, que é o tipo de erro que faz perder uma
# tarde.
garantir_nodes() {
    _base=$(basename "$DISCO")
    for _entrada in /sys/block/"$_base"/"$_base"*/dev; do
        [ -r "$_entrada" ] || continue
        _nome=$(basename "$(dirname "$_entrada")")
        _maj=$(cut -d: -f1 "$_entrada")
        _min=$(cut -d: -f2 "$_entrada")
        if [ -b "/dev/$_nome" ]; then
            # Um nó que sobrou da tabela antiga aponta para um lugar que não
            # existe mais, e o erro que ele dá ("não é um dispositivo válido")
            # não parece o que é. Confere o número; se não bate, refaz.
            _atual=$(stat -c '%t:%T' "/dev/$_nome" 2>/dev/null || echo "")
            _esperado=$(printf '%x:%x' "$_maj" "$_min" 2>/dev/null || echo "")
            [ "$_atual" = "$_esperado" ] && continue
            rm -f "/dev/$_nome"
        fi
        mknod "/dev/$_nome" b "$_maj" "$_min" 2>/dev/null || true
    done
}

_espera=0
while [ "$_espera" -lt 10 ]; do
    garantir_nodes
    [ -b "$(parte 3)" ] && [ -b "$(parte 4)" ] && break
    _espera=$((_espera + 1))
    sleep 1
done
if [ ! -b "$(parte 3)" ] || [ ! -b "$(parte 4)" ]; then
    aviso "as partições novas não apareceram em /dev; o cartão segue bootável, sem slot B"
    exit 4
fi

# Agora o sistema de arquivos da raiz pode ocupar a partição maior.
e2fsck -fp "$(parte 2)" >/dev/null 2>&1 || true
resize2fs "$(parte 2)" >/dev/null 2>&1 || aviso "resize2fs não cresceu a raiz (segue funcionando no tamanho antigo)"

mkfs.ext4 -q -F -L pos-rootB "$(parte 3)" >/dev/null 2>&1 || { aviso "não consegui formatar o slot B"; exit 5; }
mkfs.ext4 -q -F -L pos-data "$(parte 4)" >/dev/null 2>&1 || { aviso "não consegui formatar os dados"; exit 5; }
sync

# A raiz de hoje passa a se chamar pos-rootA: o fstab e o ajudante de
# atualização procuram por rótulo, e "rootfs" é o nome que o pi-gen deixou.
e2label "$(parte 2)" pos-rootA >/dev/null 2>&1 || true

aviso "pronto: p2=pos-rootA p3=pos-rootB p4=pos-data"
exit 0

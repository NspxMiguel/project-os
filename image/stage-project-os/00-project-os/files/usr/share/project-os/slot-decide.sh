#!/bin/sh
# Qual sistema sobe. Roda dentro do initramfs, antes de existir sistema nenhum.
#
# É o pedaço de código mais importante do cartão: enquanto ele funcionar, um
# sistema quebrado custa dois minutos de boot em falso, e não uma viagem até o
# PC com o cartão na mão -- que é o que este esquema inteiro existe para evitar.
#
#     "n quero nunca mais ter q pegar esse sdcard e plugar no meu pc dnv"
#
# Por isso ele é POSIX puro, sem depender de nada além do busybox, e por isso a
# decisão mora aqui separada do resto: assim dá para testar cada caso sem um Pi.
#
# Uso:  decidir_slot <caminho-do-conf>
# Imprime uma linha:  <SLOT> <TRIES-A-GRAVAR>
#            ou:      RECOVERY 0
#
# Quem chama grava o resultado de volta no arquivo *antes* de entregar o boot,
# porque um sistema que trava no meio do boot não volta para gravar nada.

MAX_TRIES=3

# Lê uma chave do arquivo sem invocar shell nenhum sobre o conteúdo: o arquivo
# fica numa partição FAT que qualquer um pode escrever, e dar `eval` nele seria
# entregar o boot para quem editasse o cartão.
ler_chave() {
    _arquivo="$1"
    _chave="$2"
    _padrao="$3"
    [ -f "$_arquivo" ] || { printf '%s' "$_padrao"; return; }
    _valor=$(sed -n "s/^[[:space:]]*$_chave[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p" \
             "$_arquivo" 2>/dev/null | tail -n 1)
    [ -n "$_valor" ] || _valor="$_padrao"
    printf '%s' "$_valor"
}

e_slot() {
    case "$1" in
        A|B) return 0 ;;
        *)   return 1 ;;
    esac
}

e_numero() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *)           return 0 ;;
    esac
}

decidir_slot() {
    _conf="$1"

    _slot=$(ler_chave "$_conf" slot A)
    _good=$(ler_chave "$_conf" good A)
    _tries=$(ler_chave "$_conf" tries 0)
    _recovery=$(ler_chave "$_conf" recovery 0)

    # Qualquer coisa ilegível vira o padrão. No caminho do boot, "não entendi,
    # então subo o A" é infinitamente melhor do que não subir nada.
    e_slot "$_slot" || _slot=A
    e_slot "$_good" || _good=A
    e_numero "$_tries" || _tries=0

    if [ "$_recovery" = "1" ]; then
        echo "RECOVERY 0"
        return
    fi

    if [ "$_tries" -ge "$MAX_TRIES" ]; then
        if [ "$_slot" != "$_good" ]; then
            # O slot novo não sobe. Volta para o que sabidamente sobe, e passa a
            # ser ele o pedido: um sistema quebrado não fica sendo tentado para
            # sempre a cada reinício.
            echo "$_good 1"
        else
            # O único slot que já funcionou também parou de funcionar. Não há
            # terceiro para tentar; quem resolve isso é o recovery.
            echo "RECOVERY 0"
        fi
        return
    fi

    echo "$_slot $((_tries + 1))"
}

# Chamado como programa (e não com `.`), decide e imprime.
case "${0##*/}" in
    slot-decide.sh|slot-decide)
        [ -n "$1" ] && decidir_slot "$1"
        ;;
esac

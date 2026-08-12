#!/bin/bash
# O sudoers e os arquivos de serviço, conferidos pelas ferramentas do sistema.
#
# Os dois quebram de um jeito que não aparece em teste de Python e não aparece
# na build: o arquivo é escrito, a imagem sai, e o defeito só existe quando o Pi
# liga.
#
# Um sudoers inválido não desabilita uma linha -- ele derruba o **sudo inteiro**.
# Numa caixa em que o modo Advanced é "um linux normal", e em que a senha do SSH
# e a troca de slot passam por ajudantes com sudo, isso é a caixa parar de poder
# se consertar. E é uma vírgula fora do lugar.
#
# Um arquivo de serviço com diretiva errada faz o systemd recusar a unidade. Sem
# tela, isso é indistinguível de não ter ligado.
#
# Rodado por tests/test_unidades.py via docker.

set -uo pipefail

BASE=/repo/image/stage-project-os/00-project-os/files
STAGE=/repo/image/stage-project-os/00-project-os/01-run.sh
FALHAS=0

ok()    { echo "  ok    $*"; }
falha() { echo "  FALHA $*"; FALHAS=$((FALHAS + 1)); }

apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq sudo systemd >/dev/null 2>&1

echo "== o sudoers que a imagem escreve =="
# O bloco vem do próprio 01-run.sh, e não de uma cópia: uma cópia envelheceria
# em silêncio, que é o modo de falhar que este teste existe para impedir.
awk '/^cat > \/etc\/sudoers.d\/010_project-os/,/^SUDO$/' "$STAGE" | sed '1d;$d' > /tmp/sudoers-teste
if [ ! -s /tmp/sudoers-teste ]; then
    falha "não achei o bloco do sudoers dentro do 01-run.sh"
else
    chmod 440 /tmp/sudoers-teste
    if visudo -c -f /tmp/sudoers-teste >/dev/null 2>&1; then
        ok "o sudoers é válido ($(grep -c '^project-os' /tmp/sudoers-teste) regras)"
    else
        falha "sudoers inválido -- derrubaria o sudo inteiro: $(visudo -c -f /tmp/sudoers-teste 2>&1 | head -2)"
    fi

    # Cada ajudante liberado tem que existir na imagem. Uma linha que aponta para
    # um caminho que não existe é uma permissão que nunca funciona -- e o sintoma
    # aparece só quando ele aperta o botão.
    for caminho in $(grep -oE '/usr/local/sbin/[a-z-]+' /tmp/sudoers-teste | sort -u); do
        nome=$(basename "$caminho")
        [ -f "$BASE/usr/local/sbin/$nome" ] \
            && ok "o ajudante $nome existe" \
            || falha "o sudoers libera $caminho, que não está na imagem"
    done
fi

echo
echo "== os arquivos de serviço =="
# Os executáveis também são postos no lugar: sem eles o systemd reclama de
# "comando não existe", que aqui seria ruído e esconderia o erro de verdade.
install -d /usr/local/sbin /opt/project-os/bin
for s in "$BASE"/usr/local/sbin/*; do install -m 755 "$s" /usr/local/sbin/; done
printf '#!/bin/sh\n' > /opt/project-os/bin/project-os
chmod 755 /opt/project-os/bin/project-os
install -d /usr/bin && printf '#!/bin/sh\n' > /usr/bin/install-teste

for U in "$BASE"/etc/systemd/system/*.service; do
    N=$(basename "$U")
    install -m 644 "$U" "/etc/systemd/system/$N"
    SAIDA=$(systemd-analyze verify "/etc/systemd/system/$N" 2>&1 \
        | grep -vE "Unknown key name 'Documentation'|^$" | head -3)
    [ -z "$SAIDA" ] && ok "$N" || falha "$N: $SAIDA"
done

echo
if [ "$FALHAS" -eq 0 ]; then
    echo "TUDO OK"
    exit 0
fi
echo "$FALHAS FALHA(S)"
exit 1

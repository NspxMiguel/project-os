#!/bin/bash
# O arquivo de Wi-Fi do cartão, lido do jeito que o Pi lê.
#
# Este script decide se ele **enxerga** a caixa. Numa Raspberry sem tela e sem
# cabo, não entrar na rede é indistinguível de não ligar -- e o conserto é o
# cartão de volta no PC, que é o que este projeto existe para nunca precisar.
#
# O que se testa aqui não é o feliz: é o arquivo salvo no editor errado.
# Um project-os-wifi.txt escrito no Windows termina cada linha com CR, e um
# "MinhaRede\r" não é o nome de rede nenhuma. Um editor que põe BOM no começo
# faz a primeira chave virar "\xef\xbb\xbfssid", que não casa com nada. Nos dois
# casos o Pi liga, não entra na rede, e não tem como contar isso para ninguém.
#
# Rodado por tests/test_wifi_cartao.py via docker.

set -uo pipefail

ORIGEM=/repo/image/stage-project-os/00-project-os/files/usr/local/sbin/project-os-firstboot
TRAB=$(mktemp -d)
FALHAS=0

ok()    { echo "  ok    $*"; }
falha() { echo "  FALHA $*"; FALHAS=$((FALHAS + 1)); }

# --- o mundo de mentira -------------------------------------------------------
# nmcli anota o que recebeu num arquivo, e diz se está conectado conforme o
# roteiro do teste. O resto não faz nada.
SHIMS="$TRAB/shims"
mkdir -p "$SHIMS" /boot/firmware

cat > "$SHIMS/nmcli" <<SH
#!/bin/sh
if [ "\$1" = "-t" ]; then
    # "nmcli -t -f STATE general status"
    cat "$TRAB/estado" 2>/dev/null || echo "disconnected"
    exit 0
fi
# "nmcli device wifi connect <ssid> password <senha>"
printf '%s\n' "\$*" > "$TRAB/tentativa"
exit 0
SH
printf '#!/bin/sh\nexit 0\n' > "$SHIMS/logger"
printf '#!/bin/sh\nexit 0\n' > "$SHIMS/rfkill"
printf '#!/bin/sh\nexit 0\n' > "$SHIMS/raspi-config"
printf '#!/bin/sh\nexit 0\n' > "$SHIMS/sleep"
chmod +x "$SHIMS"/*

rodar() {
    rm -f "$TRAB/tentativa"
    printf '%s' "${2:-connected}" > "$TRAB/estado"
    printf '%s' "$1" > /boot/firmware/project-os-wifi.txt
    PATH="$SHIMS:$PATH" bash "$ORIGEM" >/dev/null 2>&1
}

tentativa() { cat "$TRAB/tentativa" 2>/dev/null || echo "(nao tentou)"; }

echo "== o arquivo simples =="
rodar 'ssid=MinhaRede
password=minhasenha
country=BR
'
case "$(tentativa)" in
    *"MinhaRede"*"minhasenha"*) ok "entrou na rede com o nome e a senha certos" ;;
    *) falha "tentou: $(tentativa)" ;;
esac

[ -f /boot/firmware/project-os-wifi.txt ] \
    && falha "deixou o arquivo com a senha em texto puro numa partição que todo mundo lê" \
    || ok "apagou o arquivo depois de conectar"

echo
echo "== salvo no Windows (cada linha termina em CR) =="
printf 'ssid=MinhaRede\r\npassword=minhasenha\r\ncountry=BR\r\n' > "$TRAB/crlf"
rodar "$(cat "$TRAB/crlf")"
case "$(tentativa)" in
    *"MinhaRede password minhasenha"*) ok "o CR não foi parar dentro do nome da rede" ;;
    *) falha "o CR entrou nos valores -- tentou: $(cat -A "$TRAB/tentativa" 2>/dev/null | head -1)" ;;
esac

echo
echo "== com BOM no começo (TextEdit, Bloco de Notas) =="
printf '\xef\xbb\xbfssid=MinhaRede\npassword=minhasenha\n' > "$TRAB/bom"
rodar "$(cat "$TRAB/bom")"
case "$(tentativa)" in
    *"MinhaRede"*) ok "o BOM não escondeu a linha do ssid" ;;
    *) falha "o BOM fez o arquivo inteiro ser ignorado -- tentou: $(tentativa)" ;;
esac

echo
echo "== valores entre aspas =="
rodar 'ssid="Minha Rede"
password="senha com espaço"
'
case "$(tentativa)" in
    *"Minha Rede"*"senha com espaço"*) ok "as aspas saíram e o espaço no meio ficou" ;;
    *) falha "tentou: $(tentativa)" ;;
esac

echo
echo "== senha com = no meio =="
rodar 'ssid=MinhaRede
password=a=b=c
'
case "$(tentativa)" in
    *"a=b=c"*) ok "a senha inteira chegou (não cortou no primeiro =)" ;;
    *) falha "cortou a senha: $(tentativa)" ;;
esac

echo
echo "== psk como sinônimo de password =="
rodar 'ssid=MinhaRede
psk=minhasenha
'
case "$(tentativa)" in
    *"minhasenha"*) ok "psk vale como senha" ;;
    *) falha "ignorou o psk: $(tentativa)" ;;
esac

echo
echo "== comentários, linhas em branco e maiúsculas =="
rodar '# a rede da casa
SSID=MinhaRede

Password=minhasenha
'
case "$(tentativa)" in
    *"MinhaRede"*"minhasenha"*) ok "leu apesar dos comentários e das maiúsculas" ;;
    *) falha "tentou: $(tentativa)" ;;
esac

echo
echo "== sem ssid: não faz nada e não some com o arquivo =="
rodar 'password=minhasenha
'
[ "$(tentativa)" = "(nao tentou)" ] && ok "não tentou entrar em rede nenhuma" \
                                    || falha "tentou mesmo sem ssid: $(tentativa)"
[ -f /boot/firmware/project-os-wifi.txt ] && ok "o arquivo continua lá para ser corrigido" \
                                          || falha "apagou o arquivo que ele precisa consertar"

echo
echo "== a rede não aceitou: o arquivo tem que ficar =="
rodar 'ssid=MinhaRede
password=errada
' "disconnected"
[ -f /boot/firmware/project-os-wifi.txt ] \
    && ok "manteve o arquivo para ele corrigir no cartão em vez de regravar" \
    || falha "apagou o arquivo depois de falhar -- ele perderia o único jeito de corrigir"

echo
echo "== sem arquivo nenhum (cabo de rede) =="
rm -f /boot/firmware/project-os-wifi.txt
PATH="$SHIMS:$PATH" bash "$ORIGEM" >/dev/null 2>&1
[ "$?" = "0" ] && ok "sai quieto quando não há nada para fazer" || falha "saiu com erro sem arquivo"

rm -rf "$TRAB"
echo
if [ "$FALHAS" -eq 0 ]; then
    echo "TUDO OK"
    exit 0
fi
echo "$FALHAS FALHA(S)"
exit 1

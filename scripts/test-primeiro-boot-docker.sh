#!/bin/bash
# O primeiro boot do cartão, com o software do cartão.
#
#     "checa tudo, tudo tudo tudo, pra quando eu plugar o sd, pela ultima vez na
#      minha viida, dar 100% certo"
#
# Todo teste deste projeto roda o código do repositório. Este roda o que está
# **dentro da imagem**: a partição raiz é extraída do .img, virada em imagem de
# contêiner, e o serviço sobe de lá -- mesmo virtualenv, mesmo Python, mesma
# arquitetura (armv7, por emulação), mesmo usuário sem privilégio.
#
# O que só este teste vê: o virtualenv da imagem realmente importa tudo o que o
# app precisa naquela arquitetura, o serviço sobe como usuário project-os, e a
# tela de criar conta -- a primeira coisa que ele vai fazer -- grava no banco e
# devolve sessão. Se isso falhasse na caixa dele, não haveria conserto pela
# rede: o SSH sai trancado justamente até essa conta existir.
#
# A senha usada aqui é descartável e vive só neste contêiner. A senha da caixa
# dele é dele, criada por ele na tela.
#
# Uso:  scripts/test-primeiro-boot-docker.sh caminho/para/project-os-X.Y.Z.img.xz
set -euo pipefail

IMAGEM="${1:-}"
[ -n "$IMAGEM" ] || { echo "uso: $0 <imagem.img.xz>"; exit 2; }
[ -f "$IMAGEM" ] || { echo "não achei $IMAGEM"; exit 2; }

TRAB=$(mktemp -d)
MARCA="projectos-cartao:teste"
NOME="projectos-primeiro-boot"
PORTA=8124
FALHAS=0

ok()    { printf '  \033[32mok\033[0m   %s\n' "$1"; }
falha() { printf '  \033[31mFALHA\033[0m %s\n' "$1"; FALHAS=$((FALHAS + 1)); }

limpar() {
    docker rm -f "$NOME" >/dev/null 2>&1 || true
    rm -rf "$TRAB"
}
trap limpar EXIT

# Extrair 2,5 GB de raiz demora; com REUSAR=1 o teste roda de novo em cima da
# imagem de contêiner que já foi importada. Só para iterar à mão -- no CI, e
# sempre que a imagem muda, o caminho normal é o completo.
if [ "${REUSAR:-0}" = "1" ] && docker image inspect "$MARCA" >/dev/null 2>&1; then
    echo "== reusando a raiz já importada ($MARCA) =="
else

echo "== tirando a raiz de dentro da imagem =="
# Privilegiado porque precisa de losetup e de montar; a extração em si é só tar.
docker run --rm --privileged --platform linux/arm64 \
    -v "$(cd "$(dirname "$IMAGEM")" && pwd)":/entrada:ro \
    -v "$TRAB":/saida \
    debian:bookworm bash -euo pipefail -c "
        # Soltar o loop em qualquer saída, inclusive erro. Um loop deixado preso
        # não morre com o contêiner: fica na VM do Docker apontando para um
        # arquivo apagado -- e como esta imagem tem o mesmo identificador de
        # disco do cartão, o teste do ciclo completo achava a partição errada
        # pelo PARTUUID e acusava o produto por um lixo meu.
        trap 'umount /mnt/raiz 2>/dev/null || true; [ -n \"\${LOOP:-}\" ] && losetup -d \$LOOP 2>/dev/null || true' EXIT
        apt-get update -qq >/dev/null
        apt-get install -y -qq xz-utils fdisk parted >/dev/null
        xzcat /entrada/$(basename "$IMAGEM") > /saida/cartao.img
        LOOP=\$(losetup --show -Pf /saida/cartao.img)
        partprobe \$LOOP >/dev/null 2>&1 || true
        # Dentro do contêiner o /dev não é o do kernel: o loop cria as partições,
        # mas os arquivos de dispositivo não aparecem. Fazemos à mão, lendo o
        # major:minor que o próprio kernel publica em /sys.
        base=\$(basename \$LOOP)
        for entrada in /sys/block/\$base/\$base*/dev; do
            [ -r \"\$entrada\" ] || continue
            nome=\$(basename \$(dirname \$entrada))
            maj=\$(cut -d: -f1 \$entrada); min=\$(cut -d: -f2 \$entrada)
            [ -b /dev/\$nome ] || mknod /dev/\$nome b \$maj \$min
        done
        mkdir -p /mnt/raiz
        mount \${LOOP}p2 /mnt/raiz
        tar -C /mnt/raiz --numeric-owner -cf /saida/raiz.tar .
        umount /mnt/raiz
        losetup -d \$LOOP
        rm -f /saida/cartao.img
    " >/dev/null

[ -s "$TRAB/raiz.tar" ] && ok "raiz extraída ($(du -h "$TRAB/raiz.tar" | cut -f1))" \
                        || { falha "não consegui extrair a raiz"; exit 1; }

echo "== virando contêiner armv7 =="
docker rmi -f "$MARCA" >/dev/null 2>&1 || true
docker import --platform linux/arm/v7 "$TRAB/raiz.tar" "$MARCA" >/dev/null
ok "imagem de contêiner importada"

fi

echo "== subindo o serviço como usuário project-os =="
docker rm -f "$NOME" >/dev/null 2>&1 || true
# Pelo mesmo caminho que a unidade do systemd usa -- /opt/project-os/bin/project-os
# --, não chamando o python direto: é esse script que decide interpretador e
# módulo, e testar por fora dele testaria uma partida que ninguém dá.
docker run -d --name "$NOME" --platform linux/arm/v7 \
    --user project-os \
    -e PROJECT_OS_HOME=/var/lib/project-os \
    -w /opt/project-os \
    -p "$PORTA":8099 \
    "$MARCA" \
    /opt/project-os/bin/project-os --host 0.0.0.0 --port 8099 \
    >/dev/null

# Emulação é lenta: dá tempo de sobra antes de decidir que não subiu. Mas se o
# contêiner morreu, esperar dez minutos por ele é só desperdício.
SUBIU=0
for _ in $(seq 1 120); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORTA/api/system/health" >/dev/null 2>&1; then
        SUBIU=1
        break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$NOME" 2>/dev/null)" != "true" ]; then
        break
    fi
    sleep 5
done

if [ "$SUBIU" -ne 1 ]; then
    falha "o serviço da imagem não respondeu"
    echo "--- log do contêiner ---"
    docker logs "$NOME" 2>&1 | tail -40
    exit 1
fi
ok "o serviço subiu do virtualenv da imagem, como usuário sem privilégio"

SAUDE=$(curl -fsS "http://127.0.0.1:$PORTA/api/system/health")
echo "$SAUDE" | grep -q '"status":"ok"' && ok "health responde ok" || falha "health estranho: $SAUDE"
echo "$SAUDE" | grep -q '"setup_required":true' \
    && ok "a imagem sai de fábrica sem conta (é a tela que ele vê)" \
    || falha "a imagem já vem com conta criada: $SAUDE"

echo "== a tela que ele vai usar =="
BISCOITOS="$TRAB/biscoitos.txt"
SENHA="descartavel-so-deste-conteiner"
CODIGO=$(curl -s -o "$TRAB/criacao.json" -w '%{http_code}' -c "$BISCOITOS" -X POST \
    -H 'content-type: application/json' \
    -d "{\"username\":\"teste\",\"password\":\"$SENHA\"}" \
    "http://127.0.0.1:$PORTA/api/setup")
case "$CODIGO" in
    200|201) ok "criar a conta funcionou no software do cartão ($CODIGO)" ;;
    *)       falha "criar a conta respondeu $CODIGO: $(head -c 400 "$TRAB/criacao.json")" ;;
esac

grep -q '"user"' "$TRAB/criacao.json" && ok "a resposta traz o usuário" \
                                      || falha "resposta sem usuário: $(head -c 200 "$TRAB/criacao.json")"

CODIGO=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H 'content-type: application/json' \
    -d "{\"username\":\"outro\",\"password\":\"$SENHA\"}" \
    "http://127.0.0.1:$PORTA/api/setup")
[ "$CODIGO" = "409" ] && ok "a segunda tentativa de tomar a caixa é recusada (409)" \
                      || falha "a caixa pode ser tomada duas vezes (respondeu $CODIGO)"

curl -fsS -b "$BISCOITOS" -c "$BISCOITOS" -X POST \
    -H 'content-type: application/json' \
    -d "{\"username\":\"teste\",\"password\":\"$SENHA\"}" \
    "http://127.0.0.1:$PORTA/api/auth/login" >/dev/null \
    && ok "entrar com a senha criada funciona" \
    || falha "não consegui entrar depois de criar a conta"

echo "== as telas, uma por uma =="
for CAMINHO in \
    /api/system/info \
    /api/system/stats \
    /api/system/hardware \
    /api/system/processes \
    /api/system/services \
    /api/system/logs \
    /api/settings \
    /api/apps \
    /api/store \
    /api/devices \
    /api/network \
    /api/updates \
    /api/updates/system \
    /api/files/list \
    /api/birdtunes/library
do
    CODIGO=$(curl -s -o "$TRAB/corpo.json" -w '%{http_code}' -b "$BISCOITOS" \
        "http://127.0.0.1:$PORTA$CAMINHO")
    case "$CODIGO" in
        200|204) ok "$CAMINHO ($CODIGO)" ;;
        404)     ok "$CAMINHO (404 -- rota não existe nesta versão)" ;;
        *)       falha "$CAMINHO respondeu $CODIGO: $(head -c 200 "$TRAB/corpo.json")" ;;
    esac
done

echo "== o front que a imagem serve =="
for ARQUIVO in / /app.js /lib/dom.js /views/updates.js /style.css; do
    CODIGO=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORTA$ARQUIVO")
    [ "$CODIGO" = "200" ] && ok "serve $ARQUIVO" || falha "$ARQUIVO respondeu $CODIGO"
done

# O modo Advanced é "um linux normal": instalar pacote pelo navegador, com o
# sudoers da imagem. Fica atrás de COM_APT=1 porque baixa de verdade e, em
# emulação, demora -- mas é a promessa mais literal do produto, então quando a
# imagem muda vale rodar uma vez.
if [ "${COM_APT:-0}" = "1" ]; then
    echo "== instalar um pacote pelo navegador, como ele faria =="
    PEDIDO=$(curl -s -o "$TRAB/apt.json" -w '%{http_code}' -b "$BISCOITOS" -X POST \
        -H 'content-type: application/json' -d '{"package":"sl","source":"apt"}' \
        "http://127.0.0.1:$PORTA/api/packages/install")
    case "$PEDIDO" in
        200|201|202) ok "a instalação foi aceita ($PEDIDO)" ;;
        *) falha "instalar pacote respondeu $PEDIDO: $(head -c 300 "$TRAB/apt.json")" ;;
    esac

    ESTADO=""
    for _ in $(seq 1 120); do
        ESTADO=$(curl -s -b "$BISCOITOS" "http://127.0.0.1:$PORTA/api/packages/jobs" \
            | tr ',' '\n' | grep -m1 '"state"' | cut -d'"' -f4)
        case "$ESTADO" in
            done|error|failed) break ;;
        esac
        sleep 10
    done
    [ "$ESTADO" = "done" ] && ok "o apt terminou pelo sudoers da imagem" \
        || falha "a instalação terminou como '$ESTADO': $(curl -s -b "$BISCOITOS" "http://127.0.0.1:$PORTA/api/packages/jobs" | head -c 400)"

    # Pelo dpkg, não pelo PATH: o "sl" instala em /usr/games, que não está no
    # PATH de um shell não interativo -- a primeira versão desta conferência
    # acusou o produto de mentir sobre uma instalação que tinha funcionado.
    if docker exec "$NOME" dpkg-query -W -f '${Status}' sl 2>/dev/null | grep -q "install ok installed"; then
        ok "o dpkg confirma o pacote instalado na caixa"
    else
        falha "o apt disse que terminou mas o dpkg não conhece o pacote"
        curl -s -b "$BISCOITOS" "http://127.0.0.1:$PORTA/api/packages/jobs" | head -c 600
        echo
    fi
fi

echo "== a caixa consegue trocar a própria pasta de código? =="
# A troca de versão do app acontece em volta de /opt/project-os: pasta de
# trabalho ao lado e duas renomeações, todas em /opt. Se o serviço não escreve
# lá, a atualização do app não tem como funcionar nesta imagem -- e até a 0.4.7
# não tinha mesmo.
if docker exec "$NOME" test -w /opt; then
    ok "o serviço escreve em /opt (a atualização do app funciona nesta imagem)"
else
    falha "o serviço não escreve em /opt; só dá para atualizar pelo sistema inteiro"
fi

# E o próprio código concorda com o sistema de arquivos? A resposta do can_apply
# é o que a tela mostra; se ela discordar do "test -w" acima, alguém vai ver um
# botão que não devia estar lá (ou não ver um que devia).
VEREDITO=$(docker exec "$NOME" /opt/project-os/.venv/bin/python3 -c '
import sys
sys.path.insert(0, "/opt/project-os")
try:
    from project_os.core import updates
except Exception as exc:
    print("erro:%s" % exc); raise SystemExit(0)
pode = getattr(updates, "can_apply", None)
print("sem-can_apply" if pode is None else ("pode" if pode()[0] else "nao-pode"))
' 2>/dev/null | tail -n 1)
case "$VEREDITO" in
    pode)          ok "o can_apply concorda: dá para trocar o código nesta imagem" ;;
    nao-pode)      falha "o can_apply diz que não dá, mas o /opt é gravável (ou vice-versa)" ;;
    sem-can_apply) ok "esta imagem é anterior ao can_apply (0.4.7 ou mais velha)" ;;
    *)             falha "não consegui perguntar ao can_apply: $VEREDITO" ;;
esac

echo "== erros no log do serviço =="
if docker logs "$NOME" 2>&1 | grep -qE "Traceback|ERROR"; then
    falha "o serviço registrou erro"
    docker logs "$NOME" 2>&1 | grep -E "Traceback|ERROR" -A 6 | head -40
else
    ok "nenhum traceback nem ERROR no log"
fi

echo
if [ "$FALHAS" -eq 0 ]; then
    echo "tudo certo: o software do cartão sobe e a primeira tela funciona."
else
    echo "$FALHAS falha(s)."
fi
exit "$FALHAS"

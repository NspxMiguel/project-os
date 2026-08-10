#!/usr/bin/env bash
#
# O modo Advanced, do começo ao fim, num Debian de verdade.
#
#   docker run --rm -v "$PWD":/src:ro -v "$PWD/scripts":/s debian:bookworm-slim \
#       bash /s/test-advanced-debian.sh
#
# Sobe o project-os como o usuário do serviço, com o sudoers *exato* que a
# imagem instala, e vai até o fim: cria a conta, liga o modo Advanced, lê os
# backends, busca no apt, manda instalar um pacote e confere se ele apareceu no
# disco. Existe porque a suíte de testes não consegue provar essa parte: ela
# roda num Mac, sem apt e sem sudoers, e foi justamente aí que o probe de
# privilégio errou por meses sem nenhum teste reclamar.
#
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq sudo python3 python3-venv curl >/dev/null 2>&1
useradd --system --create-home --home-dir /var/lib/project-os --shell /bin/sh project-os
cat > /etc/sudoers.d/010_project-os <<'RULES'
project-os ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt-mark, /usr/bin/flatpak, /usr/bin/systemctl, /usr/local/sbin/project-os-set-password
RULES
chmod 440 /etc/sudoers.d/010_project-os
cp -a /src /opt/project-os
rm -rf /opt/project-os/.venv /opt/project-os/.git
chown -R project-os:project-os /opt/project-os
su project-os -s /bin/sh -c '
cd /opt/project-os
python3 -m venv .venv >/dev/null
.venv/bin/pip -q install fastapi uvicorn pyyaml python-multipart >/dev/null 2>&1
PROJECT_OS_HOME=/var/lib/project-os nohup .venv/bin/python -m project_os --host 127.0.0.1 --port 8099 >/tmp/os.log 2>&1 &
for i in $(seq 1 40); do curl -fsS --max-time 2 http://127.0.0.1:8099/api/system/health >/dev/null 2>&1 && break; sleep 1; done

J=/tmp/c.txt
echo "--- setup"
curl -s -c $J -X POST http://127.0.0.1:8099/api/auth/setup -H "content-type: application/json" \
  -d "{\"username\":\"miguel\",\"password\":\"segredo-longo-1\"}" | head -c 200; echo
echo "--- modo advanced"
curl -s -b $J -X PUT http://127.0.0.1:8099/api/settings -H "content-type: application/json" \
  -d "{\"ui\":{\"mode\":\"advanced\"}}" >/dev/null
echo "--- backends"
curl -s -b $J http://127.0.0.1:8099/api/packages | python3 -c "import sys,json;d=json.load(sys.stdin);[print(\"  \",b[\"id\"],b[\"can_install\"],b[\"reason\"]) for b in d[\"backends\"]]"
echo "--- busca por sl"
curl -s -b $J "http://127.0.0.1:8099/api/packages/search?q=sl&source=apt" | python3 -c "import sys,json;d=json.load(sys.stdin);print(\"  \",len(d[\"items\"]),\"resultados; primeiro:\",d[\"items\"][0][\"name\"] if d[\"items\"] else None)"
echo "--- instalar cowsay pela API"
RAW=$(curl -s -b $J -X POST http://127.0.0.1:8099/api/packages/install -H "content-type: application/json" -d "{\"source\":\"apt\",\"package\":\"cowsay\"}"); echo "  raw: $RAW"
JOB=$(printf "%s" "$RAW" | python3 -c "import sys,json;print(json.load(sys.stdin)[\"job\"][\"id\"])")
echo "  job: $JOB"
for i in $(seq 1 60); do
  S=$(curl -s -b $J "http://127.0.0.1:8099/api/packages/jobs/$JOB" | python3 -c "import sys,json;d=json.load(sys.stdin);j=d.get(\"job\",d);print(j[\"state\"])")
  [ "$S" = "running" ] || [ "$S" = "queued" ] || break
  sleep 2
done
echo "  estado final: $S"
curl -s -b $J "http://127.0.0.1:8099/api/packages/jobs/$JOB" | python3 -c "import sys,json;d=json.load(sys.stdin);j=d.get(\"job\",d);print(\"  \".join([\"\"]+j.get(\"log\",[])[-6:]))"
'
echo "--- cowsay existe no sistema?"
ls -l /usr/games/cowsay 2>/dev/null && /usr/games/cowsay "funciona" || echo "  NAO instalou"

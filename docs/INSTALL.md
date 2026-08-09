# Instalação

O ProjectOS é um sistema, não um programa que você instala por cima de outro.
O caminho normal é gravar o cartão:

1. Baixe `projectos-<versão>.img.xz` em
   [Releases](https://github.com/NspxMiguel/projectos/releases).
2. Grave no cartão com o [Balena Etcher](https://etcher.balena.io) — ele aceita
   o `.xz` direto, não precisa descompactar.
3. **Só no Wi-Fi:** com o cartão ainda no computador, abra a partição `bootfs`,
   renomeie `projectos-wifi.txt.exemplo` para `projectos-wifi.txt` e escreva o
   nome da rede e a senha lá dentro. No cabo de rede não precisa de nada.
4. Ponha o cartão na Raspberry Pi e ligue. Sem monitor, sem teclado.
5. Abra `http://projectos.local` no navegador e crie a sua conta. Se o `.local`
   não funcionar na sua rede, use o IP que aparece no roteador.

O arquivo do Wi-Fi é lido a cada boot e apagado assim que a rede conecta — ele
guarda a senha em texto puro, e a partição de boot qualquer computador lê.

Precisando do terminal: `ssh projectos@projectos.local`, senha inicial
`projectos`. No primeiro login ele obriga a trocar — a imagem é pública, então
a senha inicial também é.

Depois da primeira instalação você não precisa mais mexer no cartão: as
atualizações vêm pela própria tela, em **Updates**.

---

## Instalar por cima de um Raspberry Pi OS que já existe

Serve para quem já tem a placa configurada e não quer regravar. Uma linha:

```bash
curl -fsSL https://raw.githubusercontent.com/NspxMiguel/projectos/main/install.sh | sudo bash
```

Depois abra `http://raspberrypi.local:8099`. A diferença para a imagem é só o
endereço: aqui a porta é 8099, porque a 80 pode já estar ocupada por outra
coisa que você instalou.

---

## O que o instalador faz

1. Confere que é Linux com systemd, que tem Python 3.9+ e que a placa tem pelo
   menos ~1 GB de RAM.
2. `apt-get install python3 python3-venv python3-dev git build-essential`.
3. Cria o usuário de sistema `projectos`, sem shell, e o coloca nos grupos
   `video`, `gpio`, `i2c` e `spi` — é por eles que passam temperatura, ventoinha
   e barramentos.
4. Clona o repositório em `/opt/projectos` (ou atualiza, se já estiver lá).
5. Cria a virtualenv e instala o pacote com os extras.
6. Escreve `/etc/systemd/system/projectos.service`, habilita e sobe.
7. Espera a saúde responder em `/api/system/health` antes de dizer que deu certo.

Rodar duas vezes é seguro: cada passo confere antes de agir, então uma segunda
execução atualiza em vez de duplicar.

## Variáveis

Todas podem ser passadas na frente do comando.

| Variável | Padrão | O que muda |
|---|---|---|
| `PROJECTOS_PREFIX` | `/opt/projectos` | Onde o código fica |
| `PROJECTOS_HOME` | `/var/lib/projectos` | Onde ficam banco, config e dados dos apps |
| `PROJECTOS_USER` | `projectos` | Usuário do serviço |
| `PROJECTOS_PORT` | `8099` | Porta |
| `PROJECTOS_EXTRAS` | decidido pela RAM | `all`, `system`, `devices`, `ha` ou `none` |
| `PROJECTOS_BRANCH` | `main` | Branch a instalar |
| `PROJECTOS_REPO` | o repositório oficial | De onde clonar |

```bash
PROJECTOS_PORT=9000 PROJECTOS_EXTRAS=system sudo -E bash install.sh
```

### Sobre os extras

Só três dependências são obrigatórias: FastAPI, uvicorn e PyYAML — mais
`python-multipart`, que o FastAPI exige em tempo de import para a rota de upload
existir.

O resto é opcional de verdade. Sem elas o ProjectOS sobe igual, e a tela que
precisaria de cada uma diz o que falta e como instalar.

| Extra | Traz | Custa |
|---|---|---|
| `system` | psutil — CPU, memória, disco, temperatura detalhados | quase nada |
| `devices` | zeroconf, pyatv, PyChromecast — achar e tocar em Apple TV e Chromecast | pesado: compila `cryptography` e `protobuf` |
| `ha` | httpx — falar com o Home Assistant | pouco |
| `all` | tudo acima | |

Em placa com menos de ~1,8 GB o instalador escolhe `system` sozinho. Numa Pi 3B
de 1 GB, instalar `devices` é um compile de vinte minutos e um pedaço real de
RAM em execução — dá pra fazer depois, quando você souber que quer:

```bash
sudo /opt/projectos/.venv/bin/pip install '/opt/projectos[devices]'
sudo systemctl restart projectos
```

## Instalar à mão

Sem systemd, ou porque você quer ver cada passo:

```bash
git clone https://github.com/NspxMiguel/projectos.git
cd ProjectOS
python3 -m venv .venv
.venv/bin/pip install -e '.[system]'
.venv/bin/pip install python-multipart
PROJECTOS_HOME=~/.projectos .venv/bin/python3 -m projectos --port 8099
```

Isso é exatamente o que a unidade do systemd faz, só que em primeiro plano.

## Atualizar

```bash
curl -fsSL https://raw.githubusercontent.com/NspxMiguel/projectos/main/install.sh | sudo bash
```

Mesma linha da instalação. O estado vive em `PROJECTOS_HOME`, separado do
código em `PROJECTOS_PREFIX`, então atualizar não encosta nos seus dados.

## Desinstalar

```bash
sudo systemctl disable --now projectos
sudo rm /etc/systemd/system/projectos.service
sudo systemctl daemon-reload
sudo rm -rf /opt/projectos
```

Isso tira o ProjectOS e deixa seus dados. Para apagar tudo, inclusive banco,
configuração e o que os apps guardaram:

```bash
sudo rm -rf /var/lib/projectos
sudo userdel projectos
```

## Quando não sobe

```bash
systemctl status projectos
journalctl -u projectos -n 50 --no-pager
journalctl -u projectos -f
```

**`Address already in use`** — outra coisa está na 8099. Veja com
`sudo ss -lntp | grep 8099` e mude `PROJECTOS_PORT`.

**Não abre pelo `.local`** — mDNS depende do Avahi:
`sudo apt-get install avahi-daemon`. Enquanto isso, use o IP:
`hostname -I` na Pi.

**`ModuleNotFoundError: multipart`** — falta `python-multipart`. O FastAPI
recusa a rota de upload em tempo de import, então o processo nem sobe:

```bash
sudo /opt/projectos/.venv/bin/pip install python-multipart
```

**Instalar demorando muito** — é `cryptography` ou `protobuf` compilando, do
extra `devices`. Numa Pi 3B isso passa de vinte minutos. Não travou.

**Sem temperatura nem ventoinha** — o usuário `projectos` precisa estar no grupo
`video`. O instalador faz isso; se você instalou à mão, falta
`sudo usermod -aG video projectos` e reiniciar o serviço.

**Esqueci a senha** — não há recuperação por e-mail, porque não há e-mail. Pare
o serviço, apague a tabela de usuários e refaça a primeira conta:

```bash
sudo systemctl stop projectos
sudo -u projectos sqlite3 /var/lib/projectos/projectos.db 'DELETE FROM users;'
sudo systemctl start projectos
```

O sistema volta a pedir a criação da primeira conta, como se fosse a primeira
vez. Os apps e os dados continuam onde estavam.

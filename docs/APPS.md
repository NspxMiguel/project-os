# Apps — o catálogo e o contrato

> Este documento estende `ARCHITECTURE.md` §7. Onde os dois divergirem, este vale
> para tudo que envolva app de sistema (`kind: "service"`); o §7 continua valendo
> para plugin Python (`kind: "builtin"`).

O pedido que gerou isto:

> "tbm ja coloca o ha como app pro sistema ne, vai adicionando varios apps famosos
> de casa inteligente... suporte a tuya ekaza, tudo. deixe a casa inteligente de vdd"

Home Assistant não é um caso especial. Ele é o primeiro item de um catálogo, e o
catálogo é o que transforma o project-os de "painel com um tocador de música" em
sistema de casa inteligente.

---

## 1. As três espécies de app

O project-os é o sistema operacional, não um container — ele hospeda processos, não
roda dentro de um. Isso dá três formas de um app existir, e a escolha entre elas é
quase sempre uma decisão de memória.

| `kind` | O que é | Custo de RAM | Exemplos |
| --- | --- | --- | --- |
| `builtin` | Módulo Python dentro do venv do project-os. Sem processo próprio. | 0–8 MB | BirdTunes, Kasa, Tuya, MQTT |
| `service` | Processo externo que o project-os instala, supervisiona e serve. | 40–500 MB | Home Assistant, Zigbee2MQTT, Node-RED, Argos |
| `link` | Nada é instalado; aponta pra algo que já roda em outro lugar. | 0 | HA que já existe na rede |

**A regra do Pi 3B.** Esta máquina tem 1 GB e nada de swap decente num cartão SD.
Um app `builtin` cabe sempre; um `service` precisa ser pesado o bastante para
justificar o processo. Por isso Kasa e Tuya são `builtin` em vez de "instale o
Home Assistant": controlar uma lâmpada não vale 450 MB.

---

## 2. Manifesto de um app de serviço

`apps/<id>/manifest.json`. Os campos que `builtin` já tinha continuam iguais; o
que segue é o que `service` acrescenta.

```json
{
  "id": "home-assistant",
  "name": "Home Assistant",
  "kind": "service",
  "version": "1.0.0",
  "icon": "home",
  "category": "smart-home",
  "summary": "A central de casa inteligente que fala com quase tudo.",
  "homepage": "https://www.home-assistant.io",
  "license": "Apache-2.0",

  "requires": {
    "ram_mb": 450,
    "disk_mb": 2200,
    "arch": ["aarch64", "armv7l", "x86_64"],
    "python": ">=3.13",
    "apt": ["libturbojpeg0", "libpcap0.8", "libffi-dev", "libssl-dev"]
  },

  "install": {
    "steps": [
      {"type": "apt", "packages": ["libturbojpeg0", "libpcap0.8"]},
      {"type": "venv", "path": "venv", "python": "python3"},
      {"type": "pip", "venv": "venv", "packages": ["homeassistant"]}
    ]
  },

  "service": {
    "exec": "{app_dir}/venv/bin/hass --config {data_dir}",
    "working_dir": "{data_dir}",
    "environment": {"PYTHONUNBUFFERED": "1"},
    "restart": "on-failure",
    "restart_sec": 10,
    "port": 8123,
    "startup_grace_seconds": 300
  },

  "health": {"type": "http", "path": "/", "expect_status": [200, 302, 401]},

  "ui": {
    "mode": "proxy",
    "path": "/",
    "websocket": true,
    "open_in_new_tab_hint": true
  },

  "settings_schema": {
    "trusted_proxy": {
      "type": "boolean", "default": true,
      "label": "Confiar no proxy do project-os",
      "help": "Escreve http_trusted_proxies no configuration.yaml do HA."
    }
  }
}
```

### `requires` — a checagem que roda **antes** de qualquer download

Nenhum passo de instalação começa antes de `requires` passar. O que é verificado,
nesta ordem, e o que acontece quando falha:

1. **`arch`** — incompatível é erro duro. O app nem aparece instalável.
2. **`disk_mb`** — comparado com `shutil.disk_usage` do volume de dados. Falta de
   espaço é erro duro; um cartão SD que enche no meio de um `pip install` corrompe
   mais coisa do que o app.
3. **`ram_mb`** — comparado com **`MemAvailable`**, não com o total. Não é erro
   duro: é um aviso que o usuário pode ignorar conscientemente, com o número na
   cara dele ("o Home Assistant quer ~450 MB; sobram 380 MB agora").
4. **`python` / `node`** — versão insuficiente vira um passo de instalação a mais
   (ver `node` abaixo), não uma recusa.
5. **`apt`** — pacotes ausentes viram passos; se não houver `sudo` sem senha, o
   project-os mostra a linha exata pro usuário colar num terminal em vez de fingir
   que instalou.

**Nada disso é estimativa inventada.** Cada `ram_mb` no catálogo é o RSS medido em
repouso do processo, arredondado pra cima, e o número aparece na loja junto com a
memória livre da máquina no momento. Um app que não cabe é dito que não cabe.

### `install.steps` — as receitas

Cada passo é uma das formas abaixo. Todas são idempotentes: rodar de novo em cima
de uma instalação boa não deve fazer nada.

| `type` | Faz | Observação |
| --- | --- | --- |
| `apt` | `apt-get install -y` | Só com privilégio; senão, instrui o usuário. |
| `venv` | Cria um venv isolado dentro do `app_dir` | Nunca no venv do project-os. |
| `pip` | Instala dentro daquele venv | `--no-cache-dir` — o cartão SD agradece. |
| `node` | Garante Node ≥ N via NodeSource | Uma vez só; compartilhado entre apps. |
| `npm` | `npm install --omit=dev` no `app_dir` | |
| `git` | Clona/atualiza um repositório | Para apps que vivem em repo. |
| `download` | Baixa e extrai um release, com checksum | `sha256` obrigatório. |
| `script` | Roda um script do próprio app | Último recurso, sempre visível no log. |

Instalação é **assíncrona e transmitida ao vivo**: `POST /api/apps/{id}/install`
devolve na hora e o progresso vai pelo event bus (`app.install`), linha a linha,
pro terminal embutido da loja. Um `pip install homeassistant` num Pi 3B leva de 10
a 25 minutos e compila coisa — esconder isso atrás de um spinner seria mentira.

Falhou? O `app_dir` é apagado e o app volta a `not_installed`. Meia instalação é
pior que nenhuma.

### `service` — a unidade systemd

O project-os gera `/etc/systemd/system/project_os-app-<id>.service` a partir do
manifesto, com o mínimo de superfície:

```ini
[Service]
User=project_os
Group=project_os
ExecStart=...
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/project-os/apps/<id>
MemoryMax=<requires.ram_mb * 1.5>M
```

`MemoryMax` existe por causa desta máquina: um app com vazamento leva o kernel a
matar *ele*, não o project-os e não o SSH. Numa placa de 1 GB, a diferença entre
"um app morreu" e "a placa sumiu da rede" é essa linha.

Se não houver privilégio pra escrever unidade, há um degradê honesto: o project-os
supervisiona o processo ele mesmo (`asyncio.create_subprocess_exec` + reinício com
backoff), com o aviso de que sem systemd o app não sobe sozinho no boot.

### `ui.mode` — como o app aparece

- **`proxy`** (padrão): o project-os serve o app em `/app/<id>/`, repassando HTTP e
  WebSocket pro `127.0.0.1:<port>`. É o que faz "tudo integrado" ser verdade — uma
  URL, um login, e o app do celular não precisa saber de porta nenhuma.
- **`panel`**: o app traz o próprio `panel.js` e é desenhado dentro da UI do
  project-os, como o BirdTunes.
- **`external`**: só um link. Pra quem se recusa a rodar em iframe.

O proxy é reverso de verdade, não iframe puro: reescreve `Location`, injeta
`X-Forwarded-For` / `X-Forwarded-Proto` / `X-Ingress-Path`, e mantém a conexão
WebSocket aberta (o HA, o Node-RED e o Zigbee2MQTT são inutilizáveis sem isso).
Apps que recusam `X-Frame-Options` recebem `open_in_new_tab_hint: true` e a UI
oferece o link em vez de uma moldura vazia.

---

## 3. O catálogo

Nada aqui é código embutido no project-os. É `apps/catalog.json`, e a lista de
repositórios que o alimenta está em `apps.repositories` na config — o repo oficial
vem configurado, e qualquer um pode apontar pro seu. Um app de terceiro instala
exatamente pelo mesmo caminho que os oficiais.

Os números de RAM abaixo são de repouso, num ARM. `cabe?` é contra um Pi 3B de
1 GB com o project-os rodando (≈ 700 MB livres).

### Casa inteligente

| App | `kind` | RAM | Porta | Cabe no 3B? |
| --- | --- | --- | --- | --- |
| **Home Assistant Core** | service | ~450 MB | 8123 | aperta — só ele e mais nada |
| **Mosquitto** (broker MQTT) | service | ~6 MB | 1883 | sim |
| **Zigbee2MQTT** | service | ~120 MB | 8080 | sim (precisa de stick Zigbee) |
| **ESPHome** | service | ~150 MB | 6052 | sim, mas compilar firmware é lento |
| **Node-RED** | service | ~90 MB | 1880 | sim |
| **Z-Wave JS UI** | service | ~130 MB | 8091 | sim (precisa de stick Z-Wave) |
| **Scrypted** | service | ~250 MB | 10443 | no limite |
| **Frigate** (câmeras/IA) | service | ~1.5 GB | 5000 | **não roda aqui** |
| **Kasa** (TP-Link) | builtin | ~4 MB | — | sim |
| **Tuya / Smart Life** | builtin | ~5 MB | — | sim |
| **MQTT client** | builtin | ~3 MB | — | sim |
| **Home Assistant (existente)** | link | 0 | — | sim |

O Frigate fica na lista **de propósito**. Uma loja que esconde o que não cabe faz o
usuário procurar fora e descobrir do jeito ruim; uma que mostra "precisa de ~1.5 GB,
esta máquina tem 1 GB" responde a pergunta antes dela ser feita.

### Utilidades

| App | `kind` | RAM | Porta |
| --- | --- | --- | --- |
| **Argos** (o assistente do Miguel) | service | ~60 MB | 8787 |
| **Pi-hole** | service | ~60 MB | 8081 |
| **Syncthing** | service | ~80 MB | 8384 |
| **Uptime Kuma** | service | ~120 MB | 3001 |
| **BirdTunes** | builtin | ~12 MB | — |

---

## 4. Estados e transições

```
not_installed → installing → installed → starting → running
                    ↓             ↓          ↓         ↓
                  failed      (uninstall)  failed   stopped
```

`GET /api/apps` devolve todos os apps do catálogo **e** os instalados, com
`state`, `installed_version`, `latest_version`, `ram_mb`, `fits` e, quando não
cabe, `fits_reason` já em texto pronto pra tela.

### API

| Método | Rota | O que faz |
| --- | --- | --- |
| `GET` | `/api/apps` | Catálogo + instalados, com `fits` calculado agora. |
| `GET` | `/api/apps/{id}` | Detalhe, incluindo `requires` e o log da última instalação. |
| `POST` | `/api/apps/{id}/install` | Começa a instalar. 202 + eventos. |
| `POST` | `/api/apps/{id}/uninstall` | Para, remove a unidade, apaga o `app_dir`. `?keep_data=true` preserva os dados. |
| `POST` | `/api/apps/{id}/start` \| `/stop` \| `/restart` | Ciclo de vida. |
| `GET` | `/api/apps/{id}/logs?tail=200` | Log do serviço (journal ou arquivo). |
| `PUT` | `/api/apps/{id}/settings` | Config do app, validada pelo `settings_schema`. |
| `ANY` | `/app/{id}/{path:path}` | O proxy reverso, com WebSocket. |

Tudo atrás da mesma sessão do project-os. Instalar app é operação privilegiada: se
`security.allow_service_control` estiver desligado, `install`/`start`/`stop`
respondem 403 nomeando a chave, igual ao shell.

---

## 5. Home Assistant, especificamente

Ele entra de duas formas, e a loja pergunta qual antes de qualquer coisa:

**"Você já tem um Home Assistant?"**

- **Tenho** → `kind: link`. O project-os já acha ele sozinho no mDNS
  (`_home-assistant._tcp`), pede só o token de longa duração, e a partir daí as
  entidades do HA aparecem no project-os como qualquer outro dispositivo. Custo: 0.
- **Não tenho** → `kind: service`, instalação de verdade, com o aviso de RAM na
  cara. Nesta placa, isso significa **HA e mais nada**: BirdTunes continua (é
  builtin), mas Zigbee2MQTT junto não vai passar.

Instalando pelo project-os, três coisas são feitas que o usuário não deveria
precisar saber:

1. `http.use_x_forwarded_for` + `http.trusted_proxies: [127.0.0.1]` no
   `configuration.yaml`, senão o HA recusa tudo que vem do proxy.
2. `startup_grace_seconds: 300` — o primeiro boot do HA num Pi 3B leva minutos e o
   health check não pode declarar falha antes disso.
3. `MemoryMax` em 675 MB, para o HA ser a coisa que morre se ele vazar.

---

## 6. Dispositivos não são apps

Tuya, Kasa, WLED, Tasmota, Shelly, Sonoff — isso é dispositivo, não aplicativo.
A porta de entrada deles é a autodetecção da §9 da arquitetura e o modelo de
entidades em `HOME.md`. Instalar o app "Kasa" não instala nada: liga um provedor
que já estava ali, esperando.

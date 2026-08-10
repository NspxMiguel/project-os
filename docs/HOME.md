# A casa — entidades, provedores, e o que fala em voz alta

> Estende `ARCHITECTURE.md` §9 (autodetecção). Onde divergirem, este vale para
> tudo que seja dispositivo de casa.

O pedido:

> "suporte a tuya ekaza, tudo. deixe a casa inteligente de vdd"

"De verdade" quer dizer: a lâmpada acende a partir do project-os, sem Home
Assistant no meio, numa placa de 1 GB. O que segue é como.

---

## 1. Uma entidade, não um dispositivo

Um dispositivo é uma caixa na parede; uma entidade é uma coisa que tem estado e
aceita ordem. Uma régua de tomada Kasa é **um** dispositivo e **seis** entidades.
O project-os guarda entidades porque é isso que a tela mostra e é isso que o Argos
vai querer ligar.

```
entity_id = "<provider>.<domain>.<local_id>"
            kasa.light.8006A1B2C3
            tuya.switch.bf1a2b3c4d5e
            airplay.media_player.living-room
```

Domínios, e só estes até que um dispositivo real exija outro:

| Domínio | Estado | Ações |
| --- | --- | --- |
| `light` | `on` / `off` | `turn_on` (com `brightness`, `hs_color`, `color_temp`), `turn_off`, `toggle` |
| `switch` | `on` / `off` | `turn_on`, `turn_off`, `toggle` |
| `sensor` | número + unidade | — (leitura) |
| `binary_sensor` | `on` / `off` | — (leitura) |
| `cover` | `open`/`closed`/`opening`/`closing` | `open`, `close`, `stop`, `set_position` |
| `fan` | `on` / `off` | `turn_on` (com `percentage`), `turn_off` |
| `climate` | modo | `set_temperature`, `set_mode` |
| `media_player` | `playing`/`paused`/`idle`/`off` | `play`, `pause`, `stop`, `set_volume`, `play_media`, **`announce`** |

Tabela `home_entities` (id, provider, domain, local_id, name, room, state,
attributes JSON, available, last_changed, last_seen) e `home_rooms`. Estado ao
vivo mora em memória; o SQLite guarda o que sobrevive a reboot (nome, cômodo,
favorito, o último estado conhecido pra tela não abrir vazia).

Toda mudança vira evento `home.state` no bus. É por aí que a UI atualiza sem
polling e é por aí que o Argos vai escutar.

---

## 2. O contrato de provedor

Um provedor é um módulo Python que sabe falar um protocolo. Nada mais.

```python
class Provider:
    id: str                      # "kasa"
    name: str                    # "TP-Link Kasa"
    needs_credentials: bool      # a loja mostra ou não um formulário

    async def probe(self) -> ProbeResult:
        """Está utilizável? Nunca levanta exceção — devolve o motivo."""

    async def discover(self, timeout: float = 5.0) -> List[DiscoveredDevice]:
        """Varre a rede. Pode achar coisa que ainda não dá pra controlar."""

    async def entities(self) -> List[Entity]:
        """O que existe agora, com estado."""

    async def call(self, entity_id: str, action: str, params: dict) -> Entity:
        """Executa e devolve o estado novo. Erro vira ApiError, não traceback."""

    async def poll(self) -> None:
        """Atualiza estado. O supervisor chama conforme poll_interval."""
```

`probe()` que falha desliga o provedor, não o sistema. Um stick Zigbee arrancado
da porta USB tira o Zigbee da tela; não tira a luz da sala.

---

## 3. Kasa — o fácil

`python-kasa` 0.7.7, assíncrono nativo, roda em Python 3.9. **Escolhido porque a
descoberta não custa nada e não precisa de conta pra maior parte do hardware.**

```python
from kasa import Discover, Device, Module, Credentials

devices = await Discover.discover(discovery_timeout=5)   # {host: Device}
dev = devices["192.168.1.42"]
await dev.update()
await dev.turn_on()
```

O que o project-os usa da biblioteca, verificado na versão instalada:

- `Discover.discover(*, target, discovery_timeout, discovery_packets, credentials, username, password)` — broadcast UDP, devolve `{host: Device}`.
- `Device.connect(*, host=..., config=...)` — reconexão direta, sem varrer de novo.
- `dev.update()`, `dev.turn_on()`, `dev.turn_off()`, `dev.set_state(bool)`
- `dev.alias`, `dev.model`, `dev.mac`, `dev.device_id`, `dev.device_type`, `dev.children`
- `dev.features` — dicionário de `Feature`, que é o mapeamento pra `attributes`
  sem escrever um `if` por modelo de lâmpada.
- `dev.modules[Module.Brightness | Module.Color | Module.ColorTemperature | Module.Energy | Module.Fan | Module.LightEffect]`

**A divisão que precisa estar na tela.** Hardware Kasa antigo (protocolo IOT) fala
sem credencial nenhuma — descobre e controla, offline, zero configuração. Kasa e
Tapo novos usam KLAP e exigem o **e-mail e senha da conta TP-Link**, que ficam no
disco da placa. Isso não é detalhe de implementação: é a diferença entre "achei sua
lâmpada" e "me dá sua senha". A loja pede a credencial **só quando** a descoberta
encontrou um aparelho que a exigiu, nomeando qual, e o resto continua funcionando
sem ela.

Régua de tomadas vira pai + filhos: `dev.children` → uma entidade `switch` por
tomada, com o nome que o usuário deu no app da TP-Link.

---

## 4. Tuya — o chato, dito com todas as letras

`tinytuya` 1.20.0. Controle **100 % local** depois de configurado, e a configuração
é genuinamente irritante. Fingir o contrário só transfere a frustração pra depois.

```python
import tinytuya
d = tinytuya.OutletDevice(dev_id, address, local_key, version=3.3)
d.set_socketPersistent(True)
d.status()          # {'dps': {'1': True, '2': 50}}
d.turn_on()
d.set_value(2, 80)
```

Três fatos que mandam no desenho:

**1. `tinytuya` é bloqueante.** Sockets síncronos. Toda chamada vai pra
`asyncio.to_thread`, e o provedor mantém **uma** conexão persistente por
dispositivo (`set_socketPersistent(True)`) em vez de reconectar a cada clique — o
handshake Tuya é caro e o Pi 3B sente.

**2. A `local_key` não é descobrível.** `tinytuya.deviceScan()` acha os aparelhos
na rede — IP, `gwId`, versão do protocolo — e **não** a chave. Sem chave, o
project-os enxerga o dispositivo e não consegue tocar nele. A chave sai de um destes
caminhos, e a UI os apresenta nesta ordem:

- **Tuya IoT Cloud** (o oficial): criar conta de desenvolvedor em
  `iot.tuya.com`, criar um projeto Cloud, vincular a conta do app Smart Life,
  pegar *Access ID* e *Access Secret*. O project-os usa `tinytuya.Cloud` pra puxar a
  lista de dispositivos **com as chaves**, grava localmente, e a partir daí **a
  nuvem nunca mais é usada** — controle é LAN pura, funciona com a internet caída.
- **Colar a chave na mão**, pra quem já tem.

O projeto Cloud da Tuya expira em 1 ano no plano gratuito. Quando isso acontece o
controle local **continua funcionando**; o que quebra é adicionar dispositivo novo.
Isso vai escrito na tela, não num README que ninguém lê.

**3. Os `dps` são números sem significado universal.** `1` costuma ser liga/desliga
e `2` costuma ser brilho, e "costuma" não é contrato. O project-os mantém um mapa de
`product_id`/`category` → papéis dos dps para as categorias comuns (`cz` tomada,
`dj` lâmpada, `kg` interruptor, `cl` cortina), usa `detect_available_dps()` pro
resto, e quando não souber, mostra os dps crus com um seletor — o usuário diz qual
é o liga/desliga uma vez e fica gravado. Chutar errado aqui é abrir a cortina
quando alguém quis apagar a luz.

**Descoberta sem chave ainda vale.** Achar 3 aparelhos Tuya e não poder controlá-los
vira um cartão de sugestão — "3 dispositivos Tuya na rede. Pra controlar sem nuvem,
preciso das chaves locais; leva uns 10 minutos, te mostro" — e não silêncio.

---

## 5. Os outros provedores

| Provedor | Como acha | Credencial | Custo |
| --- | --- | --- | --- |
| `kasa` | broadcast UDP 9999/20002 | só Tapo/KLAP | ~4 MB |
| `tuya` | broadcast UDP 6666/6667 | Access ID/Secret uma vez | ~5 MB |
| `mqtt` | broker configurado + autodiscovery HA | opcional | ~3 MB |
| `homeassistant` | mDNS `_home-assistant._tcp` | token de longa duração | ~2 MB |
| `airplay` | mDNS `_airplay._tcp` / `_raop._tcp` | nenhuma | já existe |
| `cast` | mDNS `_googlecast._tcp` | nenhuma | já existe |
| `shelly` | mDNS `_shelly._tcp` + HTTP | nenhuma | ~2 MB |
| `wled` | mDNS `_wled._tcp` | nenhuma | ~2 MB |
| `tasmota` | MQTT | via broker | — |

O provedor `mqtt` fala **MQTT Discovery do Home Assistant** (`homeassistant/+/+/config`).
Isso é de propósito: é o formato que Zigbee2MQTT, Tasmota e ESPHome já publicam.
Instalar Mosquitto + Zigbee2MQTT pelo project-os (≈126 MB) traz a casa Zigbee
inteira sem Home Assistant nenhum.

O provedor `homeassistant` é ponte, não dependência: quem já tem HA vê as entidades
dele aqui; quem não tem, não sente falta.

---

## 6. Cômodos, e o cartão que pergunta

Cômodo é do usuário, não do fabricante. Mas o fabricante quase sempre já sabe: uma
lâmpada chamada "Sala TV" não precisa que ninguém digite "Sala". Na primeira
descoberta o project-os propõe os cômodos a partir dos nomes, agrupa, e mostra **um**
cartão pra confirmar tudo de uma vez. Errou? Arrastar corrige.

Sem cômodo, o aparelho cai em "Sem cômodo" e continua funcionando. Nada aqui é
obrigatório antes de acender uma luz.

---

## 7. Falar em voz alta — a API que o Argos vai usar

`media_player` tem uma ação que os outros domínios não têm, e ela é a razão deste
capítulo existir separado:

```http
POST /api/home/announce
{
  "target": "airplay.media_player.living-room",   // ou "room": "Sala", ou "all"
  "text": "o bolo tá pronto",
  "class": "PUBLICO",
  "volume": 0.4,
  "resume": true
}
```

O que ele faz: abaixa o que estiver tocando, fala, e devolve a música de onde
parou (`resume: true`). O BirdTunes é o primeiro cliente disso — um anúncio não
pode simplesmente atropelar o passarinho e deixar o silêncio.

**`class` não é enfeite.** É o teto de sensibilidade do canal, e o alto-falante é
`PUBLICO` — um alto-falante não tem tela de bloqueio nem dono, ele transmite pra
quem estiver na sala. Um `announce` com `class` acima de `PUBLICO` é recusado com
**403**, e a recusa é do project-os, não do cliente. Isso está aqui porque é
exatamente a garantia que o Argos foi construído em cima (ver `ARGOS.md`), e uma
garantia que depende de o chamador se comportar não é garantia.

Quiet hours valem aqui também: o `announce` respeita `home.quiet_hours` do
sistema, com um `override: true` disponível para o que é urgente de verdade — e o
override fica registrado no log, com quem pediu.

---

## 8. API

| Método | Rota | O que faz |
| --- | --- | --- |
| `GET` | `/api/home/entities` | Tudo, com estado. `?room=`, `?domain=`, `?provider=` |
| `GET` | `/api/home/entities/{id}` | Uma, com histórico curto |
| `POST` | `/api/home/entities/{id}/call` | `{"action": "turn_on", "params": {...}}` |
| `PATCH` | `/api/home/entities/{id}` | Renomear, mudar cômodo, favoritar |
| `GET`/`POST`/`DELETE` | `/api/home/rooms` | Cômodos |
| `GET` | `/api/home/providers` | Estado de cada provedor + o que falta |
| `POST` | `/api/home/providers/{id}/credentials` | Credencial (nunca volta na leitura) |
| `POST` | `/api/home/providers/{id}/discover` | Varredura sob demanda |
| `POST` | `/api/home/announce` | Falar (§7) |
| `WS` | `/api/ws` → tópico `home.state` | Estado ao vivo |

---

## 9. O que isso custa nesta placa

Kasa + Tuya + MQTT + os provedores de mídia que já existem: **≈ 15 MB** somados,
sem processo externo nenhum, sem Home Assistant.

É esse o argumento. Numa placa de 1 GB, a casa inteligente "de verdade" não é a que
instala o maior programa — é a que fala com as lâmpadas direto.

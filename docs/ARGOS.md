# Preparar a casa pro Argos

> "tbm prepare casa para o project jarvis, que estou criando, pode analizar os
> arquivos, ta no meu pc, se n me engano nome dele ta como argos"

Analisei. `Projetos/argos`: assistente pessoal em Node 22, zero dependências, sem
build, sem banco — JSONL em `~/.argos`. Lembretes que escalam por vários canais até
alguém responder, memória com busca, e uma regra dura de privacidade.

Este documento é o que o ProjectOS precisa expor pro Argos ser bom aqui. Não é uma
proposta de mudança no Argos: é o outro lado da tomada.

---

## 1. A observação que muda o desenho

Do README do Argos, sobre o backstop do ntfy:

> "Se a máquina estiver dormindo, fechada ou num avião, o servidor manda mesmo
> assim — uns minutos atrasado (...). Não custa nada e cobre a maior parte do que
> um Raspberry Pi no canto cobriria."

O Raspberry Pi no canto passou a existir.

O backstop foi construído porque o Argos morava num laptop que fecha. Rodando no
ProjectOS ele mora numa placa que não fecha, não dorme e reinicia sozinha via
systemd. O backstop não some — continua cobrindo queda de energia e cartão SD
morto, que são falhas reais — mas deixa de ser o que segura o projeto de pé. Um
lembrete das 7h da manhã passa a disparar às 7h da manhã.

Essa é a maior coisa que o ProjectOS faz pelo Argos, e ela não custa uma linha de
código: é só ser a máquina que não desliga.

---

## 2. Argos como app

`kind: "service"`, e um caso limpo: Node puro, sem módulo nativo, sem build.

```json
{
  "id": "argos",
  "name": "Argos",
  "kind": "service",
  "icon": "eye",
  "category": "assistant",
  "summary": "Assistente que te persegue em todos os canais até a tarefa sair.",
  "requires": {"ram_mb": 60, "disk_mb": 120, "node": ">=22"},
  "install": {
    "steps": [
      {"type": "node", "version": "22"},
      {"type": "git", "url": "https://github.com/NspxMiguel/argos"},
      {"type": "npm", "omit_dev": true}
    ]
  },
  "service": {
    "exec": "{node} {app_dir}/bin/argos.js start",
    "environment": {"ARGOS_CONFIG_DIR": "{data_dir}"},
    "port": 8787
  },
  "ui": {"mode": "proxy", "path": "/", "websocket": true}
}
```

Três detalhes que só aparecem quando se olha o código dele:

- **Node 22 é requisito duro** — o `WebSocket` global, sem polyfill. O passo
  `node` do instalador existe basicamente por causa disso: o Raspberry Pi OS ainda
  entrega Node 18 no apt, e um `npm start` que morre com `WebSocket is not defined`
  é o pior jeito de descobrir isso.
- **`ARGOS_CONFIG_DIR`** já é lido do ambiente (`src/config.js`). Então o ProjectOS
  aponta pro `data_dir` do app e o backup/restauração do Argos vem de graça, junto
  com o do resto do sistema. Nenhuma mudança no Argos.
- **O PWA dele é o canal `pwa`**, com Web Push. Atrás do proxy do ProjectOS ele
  ganha uma origem estável na LAN, que é do que um service worker precisa.

---

## 3. O canal `projectos` — o que o `echo` sempre quis ser

O `echo.js` diz, e com razão:

> "Vai através do Home Assistant, e só através do Home Assistant. (...) é Python,
> seis dependências e um venv, num projeto cuja premissa é Node puro sem módulo
> nativo e sem build."

O raciocínio está certo e a conclusão muda quando o Argos roda **dentro** do
ProjectOS. O venv Python já está lá, rodando, com os alto-falantes já descobertos.
Falar com ele é HTTP puro em `127.0.0.1` — exatamente o mesmo trade que o `echo`
fez com o HA (HTTP simples, token, descoberta de graça), sem precisar do HA.

Então o ProjectOS oferece um alvo que um canal novo do Argos consome:

```http
POST /api/home/announce
{"target": "room:Sala", "text": "...", "class": "PUBLICO", "resume": true}
```

E honra as mesmas duas propriedades que o `echo` já tem:

- **Nada é configurado, tudo é achado.** `GET /api/home/entities?domain=media_player`
  devolve o que fala. Sem nome de dispositivo em arquivo de config nenhum — que é a
  regra em que o Argos inteiro se apoia.
- **Sem se declarar indisponível, o canal some.** Se o ProjectOS não responder ou
  não tiver alto-falante, o `detect()` devolve indisponível com o motivo e a escada
  de escalonamento passa por cima. Idêntico ao que o `echo` faz hoje.

Vantagem sobre o caminho do HA nesta placa: **≈ 450 MB de RAM que não precisam
existir**, e o HomePod mini e o Apple TV que o Argos hoje não alcança (o `echo`
procura entidades `notify` da integração Alexa Devices; o ProjectOS fala AirPlay
direto, via `pyatv`).

---

## 4. O teto de sensibilidade é aplicado dos dois lados

O `sensitivity.js` do Argos é a parte mais bem pensada do projeto: em vez de
perguntar a um modelo "isso é sensível?" — pergunta indecidível, com 25 % de
variância no mesmo prompt — ele pergunta "este canal pode emitir esta classe?",
que é decidível em código.

Uma trava assim só vale se ninguém puder contorná-la, e um canal que fala com um
serviço externo passou a ter uma superfície nova: quem chama o
`/api/home/announce` pode simplesmente não mandar a classe.

Por isso o ProjectOS aplica o teto **também**, no servidor (`HOME.md` §7):
alto-falante é `PUBLICO`, ponto, e classe acima disso volta **403**. A tabela do
Argos vira, do lado de cá, uma constante de módulo — não uma configuração, não um
campo de request. Duas travas independentes na mesma porta, e a de cá não confia na
de lá.

Não estou pedindo que o Argos mude nada. A trava dele continua rodando antes do
prompt ser montado, que é onde ela é forte. Esta é a segunda.

---

## 5. Quiet hours num lugar só

Hoje há três relógios com a mesma opinião: o `quietHours` do Argos, o
`quiet_hours` do BirdTunes, e o silêncio que qualquer app futuro vai querer. Três
lugares pra configurar a mesma coisa é dois a mais.

O ProjectOS passa a ter `home.quiet_hours` no nível do sistema, e apps leem:

```http
GET /api/home/quiet   →  {"quiet": true, "until": "07:00", "source": "system"}
```

Com a semântica que o Argos definiu, que é a certa e não vou reinventar: **a
janela represa o alerta, não cancela** — nada se perde, e nada toca no cinema. O
BirdTunes já se comporta assim (não toca; retoma depois). Um app pode manter
janela própria mais restritiva; nenhum pode ser menos restritivo que o sistema.

---

## 6. O que o ProjectOS entrega, em lista

| O que | Rota | Pra quê no Argos |
| --- | --- | --- |
| Máquina que não dorme | — | O lembrete dispara na hora certa |
| Alto-falantes achados | `GET /api/home/entities?domain=media_player` | Canal de voz sem HA |
| Falar, com teto | `POST /api/home/announce` | O canal `echo` sem Alexa e sem HA |
| Estado da casa ao vivo | `WS /api/ws` → `home.state` | "apaga a luz se ele saiu" |
| Controlar a casa | `POST /api/home/entities/{id}/call` | Lâmpada, tomada, cortina |
| Quiet hours do sistema | `GET /api/home/quiet` | Um relógio só |
| Dispositivos crus | `GET /api/devices` | A autodetecção que ele já ama |
| Config e dados persistentes | `ARGOS_CONFIG_DIR` → `data_dir` | Backup junto com o sistema |
| UI numa origem estável | proxy em `/app/argos/` | Service worker e Web Push |
| Reinício e log | systemd + `/api/apps/argos/logs` | Não morrer calado |

---

## 7. O que **não** vou fazer

- **Não vou mexer no Argos.** Ele é do Miguel e está sendo escrito agora. O que
  está aqui é superfície do lado do ProjectOS; o canal `projectos` no Argos é umas
  60 linhas quando ele quiser, ou nunca, e nada aqui quebra se ele nunca existir.
- **Não vou hospedar a memória dele.** `memory/seal.js` cifra localmente e a chave é
  metade do que decifra os lembretes. Um segundo sistema com acesso a isso é
  superfície de ataque em troca de nada.
- **Não vou duplicar o escalonamento.** O ProjectOS notifica coisas do próprio
  sistema ("disco em 90 %"). Perseguir uma pessoa até ela responder é o Argos, e
  ter dois programas na mesma casa com opinião sobre quando insistir é como se
  chega num despertador em que ninguém acredita.

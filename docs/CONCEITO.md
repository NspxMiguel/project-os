# Conceito — o que o project-os é

> "ele seria um sistema base, sem nada por padrao, mas dai vc vai conectando coisas nele
> pra deixar ele um monstro, vamos pensar comigo por favor..."

Este documento existe porque o pedido acima muda o projeto de lugar. O que estava sendo
construído era "um sistema com uns apps dentro". O que ele quer é outra coisa: **um chão
vazio onde tudo é encaixável.** Isso não é um detalhe de interface, é a arquitetura.

---

## 1. A regra que decide tudo

**Se pode ser removido, é encaixe. Se não pode, é núcleo.**

Aplicando essa régua com honestidade, o núcleo fica pequeno:

| Núcleo (sempre existe, não se desinstala) | Por quê |
|---|---|
| Login e sessão | Sem isso não há sistema |
| Loja | É de onde vem todo o resto |
| Detecção (rede, máquina, barramento) | É o que a loja usa pra saber o que te oferecer |
| Gerenciador de encaixes | Instalar, iniciar, parar, atualizar, ler log |
| Painel da máquina | CPU, RAM, disco, temperatura, ventoinha, energia |
| Terminal | Sempre presente, mas desligado por padrão |
| Modos Simple / Advanced | Não é app, é uma lente sobre tudo |

**Todo o resto é encaixe. Inclusive o BirdTunes.** Instalação limpa não tem BirdTunes,
não tem Home Assistant, não tem Kasa, não tem nada. Tem uma tela dizendo o que foi
encontrado na sua rede e um botão pra loja.

Isso resolve o item 26 de raiz e resolve um problema que eu ia ter mais tarde: enquanto
o BirdTunes vinha embutido, ele era um app privilegiado, e todo app privilegiado vira uma
exceção no código. Fazendo o primeiro app dele passar pelo mesmo funil que qualquer outro,
o funil fica testado desde o primeiro dia.

---

## 2. Cinco tipos de encaixe

Chamar tudo de "app" esconde diferenças que importam pra quem instala. São cinco coisas
distintas, e a loja separa por isso:

### 2.1 App
Tem cara e tela própria. Roda no Pi. Ocupa RAM.
*Jellyfin, Home Assistant, Node-RED, bot de zap, BirdTunes, Pi-hole, Syncthing.*

### 2.2 Integração
Não tem tela própria. Ensina o project-os a falar com alguma coisa que já existe.
*Kasa, Tuya, Chromecast, AirPlay, PS5, Xbox, impressora 3D (Moonraker/OctoPrint), impressora
de papel (IPP), Spotify Connect, câmera ONVIF.*

Integração é o que faz o item 34 ("detecta praticamente tudo") virar útil em vez de virar
uma lista bonita e inerte.

### 2.3 Ajudante
Uma **máquina** que entra no sistema e contribui com algo que o Pi não tem.
*ESP32, outro Raspberry Pi, seu PC com Windows/Mac/Linux.*

Ver seção 4 — é a ideia mais importante desta reescrita.

### 2.4 Serviço de sistema
Mexe na máquina, não tem cara de app.
*Controle de ventoinha, VPN (Tailscale/WireGuard), backup, DNS, atualização automática,
overclock, desligar o LED do Pi.*

### 2.5 Receita
"Quando isso, faça aquilo." Automação pequena, sem precisar do Node-RED inteiro.
*"Quando eu sair de casa, ligue o BirdTunes." "Quando a CPU passar de 70 °C por 5 min,
me avise no zap."*

Deixo pro final — mas o modelo de dados precisa comportar desde já, senão vira gambiarra.

---

## 3. Detecção → Receita → Loja: a espinha do sistema

Este é o fluxo central. Tudo que o project-os acha vira uma linha numa lista só, e toda
linha responde **"e daí, o que eu faço com isso?"**.

### 3.1 Três detectores

**Rede** — mDNS/Bonjour, SSDP/UPnP, ARP + fabricante do MAC (OUI), portas conhecidas,
nomes de DHCP. Acha: celular, PC, TV, Chromecast, Apple TV, HomePod, Xbox, PS5, impressora,
impressora 3D, câmera, roteador, NAS, outro Pi.

**Máquina** — o que já está instalado aqui dentro: `dpkg`, `flatpak`, `snap`, arquivos
`.desktop`, unidades do systemd, contêineres, portas escutando. É isto que resolve o item 36:
o cara foi no Advanced, instalou um Firefox e uma loja Flatpak, voltou pro Simple e **aparece
em Apps**, porque o detector de máquina viu.

**Barramento** — USB, serial, GPIO, I²C, câmera CSI. Acha ESP32, adaptador Zigbee, HAT de
ventoinha, webcam, leitor de cartão.

### 3.2 Honestidade sobre certeza

Cada achado carrega **como** foi identificado. Um PS5 reconhecido por OUI da Sony **e**
porta 9295 aberta é certeza. Um "dispositivo Linux" tirado de uma entrada ARP é chute.
A interface mostra a diferença, porque uma lista que erra com confiança é pior que uma
lista curta.

### 3.3 Receita: o "te ensina como adicionar"

> "e te ensina como adicionar o app, (deixando tudo o maximo automatizado possivel sempre)"

Cada coisa detectada aponta pra zero ou mais **receitas**. Receita é um objeto de verdade
no sistema, não um texto de ajuda. Ela declara o que entrega e **quanto de você ela precisa**:

| Nível | Significa | Exemplo |
|---|---|---|
| **Automático** | Um botão, acabou | Chromecast achado → instala a integração e pronto |
| **Um dado seu** | Um botão + um campo | Kasa novo → e-mail e senha da conta TP-Link |
| **Passo a passo** | O sistema não consegue sozinho, mas guia | Tuya → pegar a `local_key` num projeto da Tuya IoT |

A regra do "máximo automatizado possível" vira uma obrigação de projeto: **uma receita só
pode ser de nível mais alto se for tecnicamente impossível baixar.** A `local_key` da Tuya
é passo a passo porque ela genuinamente não é descobrível na rede — não porque deu preguiça.

---

## 4. Ajudantes: ESP32 e PC são a mesma ideia

> "o esp32 tbm n precisa ser só um sistema, poderia ser tbm algo q ajuda o rp"
> "opção de conectar outros pcs pra ajuda no processamento tbm é legal"

Esses dois pedidos vieram separados, mas são o mesmo conceito, e tratá-los como um só é o
que impede o sistema de virar duas gambiarras paralelas:

> **Ajudante é qualquer máquina que entra no project-os e contribui com o que o Pi não tem.**

O Pi continua sendo o cérebro e o dono da fila. O ajudante nunca manda, só oferece.

### 4.1 O que cada um contribui

| Ajudante | Contribui | Não contribui |
|---|---|---|
| ESP32 | GPIO, sensores, presença, botão físico, relé | Processamento |
| Outro Pi | Um pouco de tudo | — |
| PC (Win/Mac/Linux) | CPU, GPU, disco, rede rápida | Estar sempre ligado |

### 4.2 Por que "emprestar CPU" genérico não existe

Preciso ser franco aqui, porque é onde essa ideia costuma morrer: **não dá pra pegar um
processo e espalhar num PC pela rede.** Isso não é como funciona. O que dá — e é muito útil —
é uma **fila de tarefas pesadas** que o Pi publica e o ajudante puxa:

- transcodificar vídeo (Jellyfin sofrendo no Pi → o PC faz em segundos, com NVENC se tiver)
- baixar e converter do YouTube (BirdTunes)
- **compilar firmware do ESPHome** — no Pi leva minutos, no PC leva segundos
- gerar miniaturas, indexar biblioteca de mídia
- compactar backup
- mais pra frente: transcrição, TTS e inferência local pro Argos

Ou seja: o Pi não fica mais rápido. **As tarefas pesadas param de acontecer no Pi.** Na
prática é o que ele quer, e é honesto sobre o que a coisa faz.

### 4.3 Como o ajudante conecta

O ajudante conecta **de fora pra dentro** (websocket do PC → Pi), nunca o contrário. Motivo
prático: firewall de Windows e Mac barram conexão de entrada, e ninguém vai abrir porta no
notebook pra isso funcionar.

Emparelhamento por código de 6 dígitos mostrado na tela do Pi. O ajudante declara o que sabe
fazer (`ffmpeg`, `nvenc`, `cuda`, `python3.11`, `docker`, `platformio`) e o Pi só manda
tarefa que casa. PC desligado = tarefa volta pra fila, não some.

### 4.4 O ESP32 como acessório

Coisas concretas que um ESP32 de R$ 20 faz por um Pi:

- sensor de temperatura e umidade do cômodo dos passarinhos
- botão físico: pular música, parar, "modo silêncio"
- presença por BLE — chegou em casa, o BirdTunes para
- display e-ink com status do Pi na parede
- relé pra ligar/desligar coisa burra
- **watchdog**: o ESP32 pinga o Pi e corta a energia dele por relé se travar. Um Pi que
  trava às 3 da manhã e volta sozinho é uma diferença real, e custa quase nada.

---

## 5. Simple e Advanced não são dois sistemas

São **profundidade**, não conteúdo. A mesma lista de apps nos dois. O que muda:

| | Simple | Advanced |
|---|---|---|
| Apps | Sim | Sim |
| Dispositivos | Sim | Sim, com portas, MAC, como foi detectado |
| Máquina | Um resumo | Processos, serviços, systemd, arquivos, log |
| Terminal | Só se você ligar | Só se você ligar |
| Instalar o que não cabe | Avisa e deixa | Avisa e deixa |

O item 36 (Firefox instalado no Advanced aparecendo no Simple) só faz sentido porque a
lista é a mesma. Se fossem dois sistemas, seria um bug estranho; sendo uma lente, é o
comportamento óbvio.

---

## 6. "O usuário escolhe, ele que se fode"

> "hospedar servidores (dependendo da rasp fica ruim, mas fds, usuario q escolhe ele q se fode)"

Decisão registrada: **o cálculo de "cabe nesta placa" é aviso, nunca trava.** O
`project_os/core/catalog.py` já mede isso; o que muda é que instalar algo que não cabe pede
uma confirmação e segue. O Frigate aparece num Pi de 1 GB dizendo "precisa de ~1500 MB, você
tem ~700 MB livres" — e o botão instala do mesmo jeito.

A única trava dura que continua de pé é a placa de 512 MB, porque ali o próprio project-os
não roda — não é opinião sobre o gosto de ninguém, é o serviço não subir.

---

## 7. Terminal, ventoinha e as "coisas legais"

### 7.1 Terminal como doca, não como página

> "em configurações do dev, adiciona um botao pra adicioanr o terminal ali no canto"

O terminal não é um item da barra lateral. Ele mora em **Configurações → Desenvolvedor →
"Fixar terminal"**. Ligado, vira um painel no canto, presente em **qualquer** tela e nos
**dois** modos — porque a utilidade dele é justamente estar aberto enquanto você mexe em
outra coisa. Desligado por padrão, e a tela diz sem rodeio o que isso significa.

### 7.2 Hardware do Pi

Seção "Máquina" nas configurações. O que existe depende da placa, e o sistema só mostra o
que aquela placa tem:

- **ventoinha** — curva de temperatura, ou desligar (Pi 5 nativo; Pi 4 com o case oficial;
  HAT via PWM). Item 33.
- **frequência** — economia, padrão, overclock (com o aviso que merece)
- **LED verde** — desligar
- **HDMI** — desligar num Pi headless: sobra RAM e energia
- **Wi-Fi power save** — desligar, que é a causa nº 1 de "o Pi some da rede"
- **split de GPU** — mais RAM pro sistema numa placa sem tela

### 7.3 Outras coisas que fazem diferença

Acesso de fora sem abrir porta no roteador (Tailscale), backup pra pendrive ou pro PC
ajudante, snapshot antes de instalar algo grande, atualização automática com janela de
horário, notificação por Telegram/zap quando algo cai.

---

## 8. Catálogo é dado, não código

Sendo repositório público, o catálogo da loja precisa ser **arquivo** (YAML/JSON), não
Python. Assim alguém acrescenta o Jellyfin com um pull request de 12 linhas em vez de mexer
no código. O `catalog.py` atual vira o **leitor** do arquivo, não o dono da lista.

---

## 9. Contêiner: decidido em 08/08/2026

**O project-os é nativo. Os apps de terceiros podem ser contêiner.**

Ele me deixou escolher ("escolha por mim"), então registro a escolha e o motivo:

- **O project-os em si nunca roda em contêiner.** systemd + venv, direto na placa. Isso é o
  que ele disse desde o começo — *"project_os n é um container, é um sistema operacional"* —
  e não muda.
- **Encaixe leve roda nativo**, no próprio processo do project-os: BirdTunes, Kasa, Tuya,
  MQTT, receitas. Custo de memória perto de zero, e é a maioria do que ele vai usar.
- **App grande de terceiro roda em contêiner**: Jellyfin, Node-RED, bot de zap, Zigbee2MQTT,
  Uptime Kuma. Uma linha de definição por app em vez de uma receita de `apt`/`pip`/`npm` que
  quebra em placa diferente da dele.
- **Home Assistant é a exceção deliberada**: continua nativo em venv próprio, porque num Pi
  pequeno o contêiner do HA custa memória que a placa não tem sobrando, e a receita nativa
  do HA é das poucas que são estáveis de verdade.

O motor de contêiner não vem instalado (o sistema sai vazio, seção 1). Ele é instalado sob
demanda, na primeira vez que alguém pede um app que precisa dele, com o custo escrito na tela
antes: cerca de 50 MB de RAM parada e alguns minutos de instalação.

**Regra pra quem for acrescentar app no catálogo:** se dá pra fazer nativo sem sofrimento,
faz nativo. Contêiner é pra quando a alternativa é uma receita frágil.

---

## 10. O que muda no que já foi escrito

| Já existe | O que acontece |
|---|---|
| `core/hardware.py` + testes | Fica como está. É base. |
| `core/sysinfo.py` | Fica, e ganha ventoinha/frequência/LED |
| `core/discovery.py` | Vira o detector de **rede**; faltam os de **máquina** e **barramento** |
| `core/suggestions.py` | Vira o motor de **receitas** (mesmo lugar, contrato maior) |
| `core/catalog.py` | Vira leitor de arquivo; `fits` passa a ser aviso |
| `api/*` | Continuam |
| `web/*` | **Refeito.** Item 24. |
| `apps/birdtunes` | Deixa de vir instalado. Passa a ser o app nº 1 da loja. |
| `docs/SIMPLE-PROJECT-OS.md` | Ganha o capítulo "ESP32 como ajudante" (item 37) |

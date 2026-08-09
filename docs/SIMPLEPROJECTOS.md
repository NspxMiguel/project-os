# SimpleProjectOS — o ProjectOS que cabe num ESP32

> "seria legal até talvez ter integração com esp32, um SimpleProjectOS, pra rodar
> direto de um esp32 pra qm quiser ou necessitar, com funcoes basicas obvio"

Um ESP32 tem **520 KB de SRAM** e nenhum sistema operacional. O ProjectOS grande
tem FastAPI, SQLite e um venv Python — nada disso entra aqui, e fingir que entra
seria a pior forma de responder a esse pedido.

O que entra é a **ideia**: uma placa na tomada, com uma página no navegador, que se
acha sozinha na rede. Só que 400 vezes menor.

---

## 1. As duas coisas que ele é

**Sozinho.** Você tem um ESP32 e mais nada. Liga na tomada, ele abre um Wi-Fi
próprio na primeira vez, você diz a senha da sua rede, e a partir daí tem uma
página em `http://simpleprojectos.local` com os pinos, os sensores e uns horários.
Sem Raspberry Pi, sem nuvem, sem app.

**Satélite.** Você tem um Pi rodando o ProjectOS. O ESP32 se anuncia no mDNS, o Pi
acha, pergunta se você quer adotar, e os pinos dele viram entidades na mesma tela
onde estão as lâmpadas Kasa. O ESP32 continua funcionando sozinho se o Pi cair —
adotar não é ficar dependente.

O mesmo firmware faz as duas. Não há build separado: satélite é o que acontece
quando alguém adota.

---

## 2. Por que MicroPython

| | MicroPython | C++ / ESP-IDF |
| --- | --- | --- |
| RAM livre pro app | ~100 KB | ~280 KB |
| Compilar pra alterar | não | sim |
| Atualizar | copiar um `.py` | recompilar e regravar |
| Quem consegue contribuir | quem já mexe no ProjectOS | quem tem toolchain |

O C++ ganha em memória e perde no que este projeto é. O ProjectOS inteiro foi
escrito sem passo de build — frontend sem bundler, backend sem compilar — e um
firmware que exige toolchain seria a única coisa no repositório que ninguém
conseguiria mexer num domingo à tarde. **MicroPython.** 100 KB dá pra fazer o que
está na §4, e o que não dá está na §7.

**ESP8266 não.** 80 KB de heap, e o servidor web mais o mDNS mais TLS não cabem
juntos. Prefiro dizer que não roda a entregar algo que trava em três dias.

| Placa | Roda? | Observação |
| --- | --- | --- |
| ESP32 (original) | sim | O alvo. 520 KB SRAM, Wi-Fi + BT |
| ESP32-S3 | sim | O melhor: mais RAM, e PSRAM em muitas versões |
| ESP32-S2 | sim | Sem Bluetooth, tudo bem |
| ESP32-C3 | sim | RISC-V, barato, o que eu recomendaria comprar hoje |
| ESP32-C6 | sim | Wi-Fi 6 + Thread/Zigbee |
| ESP8266 | não | Memória insuficiente |

---

## 3. O que roda dentro

```
simple/
  boot.py            Wi-Fi, ou o portal de configuração se ainda não há rede
  main.py            o laço
  net/
    server.py        HTTP mínimo, 2 conexões simultâneas, sem framework
    discovery.py     anúncio mDNS _projectos._tcp
    api.py           as rotas
  io/
    pins.py          GPIO, ADC, PWM
    sensors.py       DHT11/22, DS18B20, analógicos
    entities.py      o modelo de entidades — o mesmo do HOME.md
  rules.py           horários e gatilhos
  state.py           config em JSON, escrita atômica
  www/
    index.html.gz    a página inteira, comprimida, ~9 KB
```

Orçamento de memória, medido e não estimado (a medição vem quando eu tiver a
placa na mão — ver §8):

| | Heap |
| --- | --- |
| MicroPython em repouso | ~100 KB livres |
| Servidor + mDNS | −18 KB |
| Entidades e regras | −12 KB |
| Pico ao servir uma requisição | −20 KB |
| **Folga** | **~50 KB** |

A folga existe porque um ESP32 sem folga é um ESP32 que reinicia às 3 da manhã.

---

## 4. As funções básicas

O que "básico" quer dizer aqui, em lista fechada:

- **Saída digital** — relé, lâmpada, válvula. Liga, desliga, inverte.
- **Entrada digital** — botão, sensor de porta, PIR. Vira `binary_sensor`.
- **PWM** — dimerizar LED, servo, ventilador. `0–100 %`.
- **ADC** — luminosidade, umidade de solo, qualquer divisor de tensão.
- **Sensores de 1 fio** — DHT11, DHT22, DS18B20. Temperatura e umidade.
- **Horários** — "liga às 18:00, desliga às 23:00", com dias da semana.
- **Gatilhos simples** — "se a entrada 4 fechar, liga a saída 12 por 30 s".
- **Atualização pela rede** — sem desparafusar nada da parede.

E o que **não** é básico e por isso não está: câmera, áudio, tela, Bluetooth,
Matter, criptografia de disco, mais de um usuário.

---

## 5. Ele fala a mesma língua

Esta é a parte que faz a integração custar quase nada. O ESP32 devolve entidades
no formato exato do [HOME.md](HOME.md) §1:

```http
GET /api/entities
{
  "node": {"id": "a4cf12ab90d4", "name": "Gaiola", "fw": "0.1.0", "uptime": 88213},
  "entities": [
    {"id": "switch.relay1", "domain": "switch", "name": "Lâmpada de aquecimento",
     "state": "on", "attributes": {"pin": 26}},
    {"id": "sensor.temp", "domain": "sensor", "name": "Temperatura",
     "state": 27.4, "attributes": {"unit": "°C", "pin": 4}}
  ]
}
```

```http
POST /api/call   {"entity": "switch.relay1", "action": "turn_off"}
```

Do lado do Pi, isso é um provedor a mais (`projectos_node`) e nenhuma tradução: as
entidades chegam prontas. É por isso que escrevi o `HOME.md` antes — o modelo de
entidades já foi desenhado sabendo que um dia ia haver algo pequeno do outro lado.

Descoberta: `_projectos._tcp.local`, TXT com `id`, `name`, `fw`, `ents`. O
ProjectOS grande já varre mDNS; um nó novo aparece como cartão de sugestão
("achei um SimpleProjectOS chamado Gaiola, 2 saídas e 1 sensor — adotar?").

---

## 6. Segurança, dita com honestidade

**Não há TLS.** Um handshake TLS no ESP32 come ~40 KB de heap — dois terços do que
sobra — e deixaria a placa sem memória pra fazer o trabalho dela. Então:

- É **só LAN**. O firmware não abre porta pra internet e o documento não vai
  ensinar a redirecionar uma.
- Toda escrita exige um **token**, gerado no primeiro boot e mostrado uma vez na
  tela de configuração. Leitura sem token devolve só o nome e a versão.
- Adotar pelo Pi troca esse token uma vez; depois disso o Pi guarda e o usuário
  nunca mais vê.
- O portal de configuração inicial é um AP aberto com uma senha impressa —
  aberto por 5 minutos, e fecha sozinho.

Um token em HTTP na LAN é fraco contra quem já está dentro da sua rede. Isso é
verdade, está escrito aqui, e é o teto do que 520 KB permitem. Quem precisa de
mais deve usar o Pi, que tem TLS de verdade.

---

## 7. O que o ESP32 nunca vai fazer

Direto, pra ninguém descobrir depois:

- **BirdTunes não roda aqui.** Decodificar MP3 e falar AirPlay não cabe, e não é
  perto de caber.
- **Sem banco.** O histórico do sensor vive no Pi, se houver Pi. Sozinho, o ESP32
  guarda o último valor e mais nada.
- **Sem loja de apps.** O firmware é o que é; para mudar, atualiza.
- **Sem Home Assistant, Zigbee2MQTT, Node-RED.** São programas de computador.
- **Sem múltiplos usuários.** Um token, uma pessoa.

O SimpleProjectOS é a ponta do sistema, não uma versão reduzida dele. A régua é:
se precisa de sistema de arquivos, é do Pi; se é um pino e um sensor, é daqui.

---

## 8. Gravar pelo próprio ProjectOS

A integração que fecha o ciclo. Espeta o ESP32 na USB do Raspberry Pi:

1. O ProjectOS vê a porta serial aparecer (`/dev/ttyUSB*`, `/dev/ttyACM*`) e
   reconhece o conversor pelo ID USB — CP2102 `10c4:ea60`, CH340 `1a86:7523`,
   FTDI `0403:6001`, ou o USB nativo do S3/C3.
2. Aparece um cartão de sugestão: "achei um ESP32 na USB. Gravar o
   SimpleProjectOS?"
3. Grava com `esptool` (pip, Python puro, sem toolchain) e copia os `.py`, com o
   progresso ao vivo na tela.
4. A placa reinicia, sobe o portal de Wi-Fi, e o próprio ProjectOS já entrega as
   credenciais da rede — porque ele está nela.
5. Trinta segundos depois o nó aparece no mDNS e se oferece pra ser adotado.

Do plugar ao controlar sem abrir um terminal. É isso que "integração com ESP32"
deveria significar.

---

## 9. Estado

Isto é projeto, não código. E há um limite que preciso declarar: **não tenho um
ESP32 na mão.** Os números da §3 são de orçamento, não de medição, e um firmware é
exatamente o tipo de coisa em que orçamento e medição divergem.

Então o plano é: escrevo o firmware com a lógica testável (entidades, regras,
roteamento, parser HTTP) coberta por testes que rodam em CPython — MicroPython é
Python suficiente pra isso — e a parte de hardware fica marcada como não
verificada até alguém gravar numa placa. O que eu não vou fazer é dizer que
funciona antes de alguém ver funcionar.

Ordem: o ProjectOS do Pi primeiro, inteiro e testado. Isto depois. Se você tiver um
ESP32 aí, ele sobe na fila — dá pra fechar o ciclo da §8 de verdade.

# Prompt pra refazer a interface

> "bem ruim a interface kkk, manda prompt pro claude desing e gg. evita emojis por favor."

Cola o bloco abaixo no Claude Design. Está em português porque o produto é em português.
O que vem depois do bloco é a lista de defeitos do que existe hoje — não faz parte do prompt,
é a justificativa de cada exigência, pra quando precisar ajustar.

---

## Prompt (copiar daqui)

```
Projete a interface do ProjectOS: um sistema operacional de casa e servidor caseiro que roda
num Raspberry Pi e é usado inteiro pelo navegador, de outro computador na mesma rede.
Concorrente mental: Home Assistant, Proxmox, Umbrel, CasaOS.

CONTEXTO DO PRODUTO
Instalação limpa vem vazia — nenhum aplicativo. O usuário abre, vê o que o sistema encontrou
sozinho na rede dele (celular, PC, TV, Chromecast, impressora, PS5, Xbox, impressora 3D,
lâmpadas, ESP32 no USB) e vai instalando coisas de uma loja pra montar o sistema que quiser:
central de casa inteligente, servidor de filmes, bot de mensagens, automações, o que for.
Há dois modos: Simples (a casa e os apps) e Avançado (a máquina por dentro). Não são dois
produtos, é a mesma coisa com mais profundidade.

TELAS
1. Painel — estado da máquina (CPU, RAM, disco, temperatura, tempo ligado), o que está
   rodando, o que foi encontrado agora, cartões de sugestão do tipo "achei um Chromecast na
   sala, quer usar?" com ação direta.
2. Aplicativos — grade do que está instalado, com estado (rodando, parado, com erro) e a
   memória que cada um está usando de verdade. Estado vazio é a tela mais importante do
   produto: é o que 100% dos usuários veem no primeiro minuto.
3. Loja — catálogo com busca, categorias e filtro. Cada item mostra quanta memória pede e se
   cabe na placa daquela pessoa. O que não cabe continua listado e instalável, marcado, com
   o motivo escrito.
4. Dispositivos — tudo que foi encontrado na rede, na máquina e no USB. Cada linha diz o que
   é, onde está e COMO foi identificado, com um indicador de certeza (confirmado / provável /
   palpite). Cada linha tem um caminho de "o que dá pra fazer com isso".
5. Ajudantes — outras máquinas ligadas ao sistema: um ESP32, outro Raspberry Pi, um PC com
   Windows/Mac/Linux que empresta processamento pesado. Mostra o que cada um oferece, se está
   online, e a fila de tarefas pesadas passando por eles.
6. Configurações — seções: Geral, Máquina (ventoinha, frequência, LED, HDMI, Wi-Fi),
   Segurança, Rede, Desenvolvedor.
7. Sistema (só no modo Avançado) — processos, serviços, arquivos, registro de eventos.

ELEMENTO ESPECIAL: TERMINAL EM DOCA
Em Configurações → Desenvolvedor existe um botão que fixa um terminal num canto da tela.
Ligado, ele fica sobre qualquer tela, nos dois modos, redimensionável, recolhível a uma
barrinha. Não é uma página do menu. Projete os três estados: recolhido, meia altura, e
expandido.

DIREÇÃO VISUAL
- Escuro por padrão, com tema claro funcionando de verdade. Não é um tema claro invertido às
  pressas.
- Denso e técnico, não fofo. Muita informação por tela, sem parecer entulhado. A referência
  é um painel de infraestrutura, não um app de banco.
- Uma cor de destaque só, usada com parcimônia. Verde-água (#5ac8a8) é o ponto de partida,
  troque se tiver argumento melhor. Cores só ganham significado quando são raras.
- Vermelho, âmbar e verde reservados exclusivamente para estado (erro, atenção, ok). Nunca
  para decoração.
- Números são o conteúdo principal em metade das telas: temperatura, memória, porcentagem,
  tempo. Trate tipografia numérica como assunto sério — fonte com tabular numbers, alinhamento
  consistente, unidade em peso menor que o número.
- Cantos discretos, sombra quase nenhuma, hierarquia por espaçamento e contraste de peso.

PROIBIDO
- Emoji. Nenhum, em lugar nenhum: nem em ícone de aplicativo, nem em cartão, nem em estado
  vazio, nem em mensagem de erro. Ícones são traçado monocromático de biblioteca coerente.
- Ilustração fofa, mascote, gradiente decorativo, vidro fosco, animação de entrada em tudo.
- Barra lateral com uma lista frouxa de itens de tamanhos diferentes e ícones desalinhados.
- Cartões que crescem no hover só pra crescer.

O QUE PRECISO DE VOLTA
- Tokens: cor (claro e escuro), tipografia, espaçamento, raio, elevação, duração.
- Componentes: barra lateral com dois níveis, cartão de aplicativo, cartão de sugestão com
  ação, linha de dispositivo com indicador de certeza, mostrador de métrica, distintivo de
  estado, tabela, campo de formulário, modal, aviso, doca do terminal, estado vazio,
  estado de carregamento, estado de erro.
- As sete telas montadas com esses componentes, em desktop e em celular (o celular importa:
  metade do uso é conferir a casa do sofá).
- HTML e CSS puros, sem framework e sem dependência externa. O sistema roda numa rede sem
  internet — nada de CDN, nada de fonte remota. Use pilha de fontes do sistema.

RESTRIÇÕES TÉCNICAS QUE MUDAM O DESENHO
- Roda num Raspberry Pi: o servidor é lento, o navegador não. Prefira desenho que aceite
  dados chegando aos poucos, com lugar reservado, em vez de tela travada esperando tudo.
- Os dados chegam ao vivo por websocket e mudam a cada 5 segundos. Números que piscam ou
  saltam de largura a cada atualização são um defeito, não um detalhe.
- O texto está em português do Brasil e vai ser traduzido: nada pode depender de caber num
  espaço exato. "Configurações" é quase o dobro de "Settings".
```

---

## Por que cada exigência está aí

Defeitos do que existe hoje (o print que ele mandou mostra a barra lateral):

1. **Emoji como ícone de app.** Um passarinho, uma casa, uma lâmpada, um olho e um plugue —
   cinco desenhos de origens diferentes, cores diferentes, pesos diferentes e alinhamento
   diferente na mesma coluna. Isso sozinho já derruba a barra. Ele pediu direto: sem emoji.
2. **Dois alfabetos de ícone na mesma lista.** Os itens de navegação usam ícone de traçado
   monocromático; os apps usam emoji colorido. Lado a lado, parece defeito.
3. **Hierarquia de um nível só.** "Dashboard", "Apps", os cinco apps e "Devices" estão todos
   no mesmo recuo, então os apps não parecem estar dentro de Apps — parecem irmãos dele.
4. **A lista está errada por princípio.** Mostra cinco apps instalados de fábrica, e ele quer
   instalação vazia. O estado inicial correto dessa barra é quase nada.
5. **Vazio embaixo, apertado em cima.** Dois terços da barra são espaço morto, com "App store"
   solto no rodapé, longe do único lugar de onde alguém vai querer chamar a loja: a tela de
   apps vazia.
6. **Bolinha verde sem legenda.** Aparece em três apps. Não dá pra saber se é "rodando",
   "atualização disponível" ou "notificação". Estado precisa de forma, não só de cor —
   inclusive por quem não distingue verde de vermelho.

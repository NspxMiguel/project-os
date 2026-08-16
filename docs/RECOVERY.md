# Nunca mais o cartão

    "acha uma solucão, de fleshar o sistema COMPLETAMENTE. Pra eu NUNCA MAIS ter q
     fleshar manualmente essa porra. sla irmao, tipo um recovery, igual twrp. da teu
     jeito, n quero nunca mais ter q pegar esse sdcard e plugar no meu pc dnv"

Ele gravou o cartão duas vezes no mesmo dia. Isso não é falta de sorte: é defeito
de projeto. O que se atualizava pela rede era **o app**, e o que quebrou não era o
app — era o arquivo do serviço, o ajudante root e o sudoers, que moram fora de
`/opt/project-os`. Qualquer conserto ali exigia o cartão de volta no PC.

A regra que este documento estabelece:

> **Tudo que está no cartão pode ser substituído pela rede. Tudo.** Kernel, sistema
> de arquivos raiz, arquivos de serviço, sudoers, o próprio project-os. A única
> gravação manual que existe é a primeira.

## 1. Por que uma partição só nunca vai dar conta

Trocar o sistema raiz enquanto ele roda é impossível de fazer direito. Não dá para
substituir o `/` de baixo dos pés de um processo que está lendo dele, e não dá para
voltar atrás se a troca falhar no meio — a essa altura não existe mais um sistema
inteiro para dar a resposta.

É por isso que celular, Android TV, ChromeOS e carro fazem todos a mesma coisa:
**dois sistemas, um ativo e outro reserva.**

## 2. O cartão depois desta mudança

```
p1  boot    FAT32   512 MB   firmware, kernel, cmdline.txt, initramfs, estado do slot
p2  rootA   ext4      6 GB   um sistema completo
p3  rootB   ext4      6 GB   outro sistema completo
p4  data    ext4    resto    /var/lib/project-os — banco, config, música, downloads
```

Os dois slots são sistemas inteiros e intercambiáveis. `data` fica de fora dos dois
de propósito: atualizar troca **sistema**, nunca os teus dados. É a mesma regra que
o atualizador do app já segue (`PROJECT_OS_HOME` sempre fora da árvore de código),
levada até o fim.

## 3. Quem escolhe o slot: o initramfs

O firmware do Pi lê `cmdline.txt` da partição FAT e sobe um **initramfs** nosso —
uns poucos MB de busybox — antes de qualquer sistema raiz existir. Ele é o TWRP
desta história, e faz três coisas:

1. lê `/boot/project-os-slot.conf` (na FAT, que ele consegue montar sem depender de
   slot nenhum);
2. monta o slot pedido e entrega o boot para ele;
3. se o slot pedido falhou em subir vezes demais, entrega para o outro.

```
slot=B        # quem deve subir agora
good=A        # o último que provou que sobe
tries=2       # quantas vezes tentamos sem confirmação
recovery=0    # 1 = não suba sistema nenhum, abra o modo recovery
```

O initramfs incrementa `tries` **antes** de entregar o boot. Um sistema que sobe de
verdade zera esse contador (seção 5). Um sistema que trava no meio do boot não zera
nada — e na terceira tentativa o initramfs desiste dele e sobe o `good`.

Essa é a parte que faz a promessa valer: quem decide é código que roda **antes** do
sistema, num lugar que a atualização nunca toca. Um slot novo completamente quebrado
custa dois minutos de boot em falso, não uma viagem até o PC.

### Três detalhes que fazem a diferença entre funcionar e parecer que funciona

Cada um destes já estava errado uma vez, e nenhum dá erro quando está errado.

**A escolha do slot sai pelo `/conf/param.conf`, não por `export ROOT`.** Os scripts
de `local-top` não rodam dentro do shell do init: o arquivo `ORDER`, que o
initramfs-tools gera, chama cada um como processo filho e só então faz `source` de
`/conf/param.conf`. Um `export` morre com o filho — o init seguiria montando a
partição que o `cmdline.txt` manda, a p2, sempre. O sintoma seria o pior possível:
o Pi liga normal, tudo funciona, e a troca de sistema nunca acontece. Toda
atualização gravaria o slot B e todo boot subiria o slot A.
Preso por `tests/test_slot_boot.py`, que roda o script como processo filho.

**O identificador do disco é preservado no reparticionamento.** O `cmdline.txt` pede
`root=PARTUUID=<id>-02` e o `fstab` monta `/boot/firmware` por `PARTUUID=<id>-01` —
os dois derivam do `label-id` do MBR, e o `sfdisk` sorteia um novo quando a tabela
que a gente escreve não diz qual é. Trocá-lo apaga as duas referências de uma vez:
a partição de boot deixa de montar. Preso por `tests/test_layout.py`.

**O `fstab` de um slot aponta para o próprio slot.** Um sistema chega ao slot B
copiado ou desempacotado, e nos dois casos o `/etc/fstab` que vem junto diz que a
raiz é a p2, porque era verdade na origem. Apagar a linha não resolve: o
`cmdline.txt` não tem `rw`, então o kernel monta a raiz **somente leitura** e quem a
remonta para escrita é o `systemd-remount-fs`, lendo exatamente essa linha. Sem ela
o slot sobe, responde, e não grava nada. Quem conserta é o `fstab-slot.sh`, chamado
pelos dois caminhos. Preso por `tests/test_sysupdate_fstab.py`.

**Quem grava o estado é um ajudante de root.** A FAT é montada com `uid=0` e umask
0022 (o padrão do vfat) e o project-os roda como usuário comum: ele não consegue
criar arquivo nenhum em `/boot/firmware`. Sem o `project-os-slot-state`, o
`boot_into()` falharia e uma atualização gravaria o slot B inteiro sem conseguir
apontar o boot para ele — a tela diria que deu certo e o Pi reiniciaria no mesmo
lugar. Preso por `tests/test_slot_state_helper.py`.

E um quarto, do lado do firmware: o `config.txt` usa `auto_initramfs=1` e **não** tem
linha `initramfs <arquivo>`. O cartão traz quatro kernels e o Pi 3B sobe o `kernel7`;
um initramfs carrega dentro os módulos do kernel para o qual foi gerado. Forçar um
arquivo só — que na v0.4.1 era o de 64 bits — deixa o Pi esperando para sempre um
cartão SD cujo driver não está lá dentro. Preso pela build e por
`scripts/verify-image-docker.sh`, que confere os quatro, um a um.

**Um slot só conta como caminho de volta se estiver carimbado.** O arquivo
`/etc/project-os-slot-completo` é escrito no fim de quem termina de gravar um slot
— o clone, a atualização — e já vem na imagem, no slot A. A pergunta "esse slot já
tem sistema?" é respondida por ele, e não por "tem `/usr` e `/etc`?": uma cópia
interrompida tem os dois, e essa resposta errada é cara. O slot fica com meio
sistema, o clone marca "já fiz" e nunca mais tenta, e a caixa passa a contar com um
caminho de volta que não sobe. No dia em que o slot de hoje parar, o initramfs
alterna para esse lixo, ele também não sobe, e não há mais de onde voltar.
E o `rsync` que sai com **24** ("sumiu arquivo durante a cópia") copiou tudo que
importa: num sistema ligado isso é o normal — log rotacionado, temporário de
serviço — e tratar como falha abortaria a cópia de um sistema perfeitamente bom,
quase toda vez. Preso por `tests/test_ciclo_completo.py`, que faz o cartão viver o
ciclo inteiro num disco de verdade.

## 4. Como uma atualização de sistema acontece

Nada disso é escrito à mão: é o botão "Atualizar" da tela, com o passo a passo
aparecendo.

1. O manifesto anuncia uma versão de **sistema** (um tarball do rootfs, com sha256),
   além da versão do app.
2. O project-os baixa e **confere o sha256 antes de escrever qualquer coisa**. Um
   tarball que não bate é apagado, não instalado — a mesma regra do atualizador do
   app, pelo mesmo motivo: sem isso, toda atualização é um convite a quem conseguir
   responder pelo servidor.
3. Formata o **slot inativo** e desempacota lá dentro. O slot que está rodando não é
   tocado em momento nenhum.
4. Escreve `slot=<novo>`, `tries=0` na FAT e reinicia.
5. O sistema novo sobe, se apresenta, e só então marca `good=<novo>`.

Se o passo 5 não acontecer — o slot novo não sobe, ou sobe e o project-os não
responde — o initramfs volta sozinho para o `good` na terceira tentativa. O cartão
não sai do Pi.

### O que uma atualização de sistema **não** troca

A partição FAT — firmware, kernel, `config.txt`, `cmdline.txt` e o initramfs — fica
como saiu da gravação. O tarball da atualização é empacotado com
`--exclude=./boot/firmware/*` de propósito.

Isso é escolha, não esquecimento: a camada que socorre as outras não pode ser
trocada pela mesma coisa que ela existe para socorrer. Um initramfs novo com defeito
seria o único jeito de voltar a precisar do cartão na mão, e não há terceiro slot
para voltar dele.

O preço é que uma mudança no decisor de slot só chega numa gravação nova. Enquanto o
decisor fizer o que promete — e ele é pequeno e testado justamente por isso — esse
preço não é cobrado.

### As duas atualizações, e qual funciona onde

A tela oferece duas, e elas não são a mesma coisa:

* **Atualizar o app** troca só a árvore de código em `/opt/project-os`. Rápido,
  alguns megabytes.
* **Atualizar o sistema** escreve um rootfs inteiro no slot livre e reinicia nele.
  É meio giga, e é a única que troca o que está fora do `/opt` — ajudantes em
  `/usr/local/sbin`, o sudoers, o systemd, as permissões do próprio `/opt`.

A troca do app tem dois caminhos, e o app escolhe sozinho (`updates.swap_strategy`):

* **por fora** — pasta de trabalho ao lado do código, renomeia a atual para
  `project-os.previous-<versão>`, renomeia a nova para o lugar. Uma renomeação só
  para quem olha: nunca existe meia árvore. Precisa de escrita em `/opt`, e é o que
  roda em qualquer cartão gravado da **0.4.8** em diante, onde a imagem deixa o
  grupo do serviço escrever lá.
* **por dentro** — a mesma ideia um andar abaixo: o que está em `/opt/project-os`
  desce para `/opt/project-os/.previous-<versão>`, o que veio no pacote sobe no
  lugar, e o `.venv` não se mexe. Precisa apenas da pasta do código, que é do
  serviço em toda imagem.

Num cartão gravado até a **0.4.7** o `/opt` é `root:root 755`, enquanto o serviço
roda como `project-os` — ali a troca por fora é recusada em todas as três operações,
e por um tempo isso significou que atualizar o app era impossível: baixava o pacote
e morria com `Permission denied`. A única saída era um rootfs de meio giga para
entregar uma correção de setecentos kilobytes.

Da **0.4.14** em diante essas caixas se atualizam pelo caminho de dentro, sem
gravação nova e sem tocar em permissão nenhuma. Só quando nem a pasta do código é
escrevível é que a tela recusa antes de baixar e aponta a atualização de sistema.

A troca por dentro não é uma renomeação só, então existe uma janela de alguns
milissegundos com a árvore pela metade. Se ela for interrompida no meio, o que já
tinha se movido volta (`_move_names` desfaz o que fez); e é um preço barato perto de
"esta caixa não se atualiza".

### Voltar a versão anterior do app

A árvore trocada não é apagada: ela fica em `/opt/project-os.previous-<versão>`
quando a troca foi por fora, ou em `/opt/project-os/.previous-<versão>` quando foi
por dentro. A tela procura nos dois lugares. É o A/B da atualização de app — mais
pobre que o do sistema, porque só cobre o que está dentro do `/opt`, e suficiente
para o caso comum, que é uma versão nova pior que a anterior.

Duas coisas que essa pasta exige, e que a **0.4.10** conserta:

* **A tela procura a pasta no disco, e não na memória do processo.** O caminho
  era lembrado pelo trabalho de atualização em andamento; como a atualização
  reinicia o serviço, o botão desaparecia no instante em que passaria a servir
  para alguma coisa. Quem quisesse voltar tinha que fazer as duas renomeações
  na mão, por SSH.
* **O virtualenv volta junto.** A atualização por fora *move* o `.venv` para a
  árvore nova (é o mesmo interpretador, e copiar meio giga de dependências a cada
  versão não faria sentido), então a árvore guardada fica sem ele. Voltar sem
  trazê-lo de volta devolvia uma instalação sem interpretador: o `bin/project-os`
  cai no python do sistema, que na imagem não tem uvicorn, e o serviço não sobe.
  O socorro deixava a caixa pior que o acidente que ele existe para consertar.

Depois de voltar, o serviço reinicia sozinho — os arquivos são os antigos e a
memória ainda é a nova até isso acontecer.

## 5. O que conta como "esse sistema presta"

Um slot só vira `good` quando o project-os **atende uma requisição**. Não quando o
kernel sobe, não quando o systemd termina: quando `/api/system/health` responde.

Um kernel que sobe num sistema onde a rede não funciona ou o serviço morre no
arranque é exatamente o tipo de meia-vitória que trancaria o Pi para sempre — e é
por isso que a confirmação é a última coisa, feita pelo próprio serviço, e não uma
dedução do boot ter chegado ao fim.

## 6. O modo recovery

`recovery=1` na FAT (ou três falhas nos **dois** slots) faz o initramfs parar de
tentar subir sistema e abrir o recovery: rede por DHCP, mDNS respondendo em
`project-os.local`, e uma página única que faz uma coisa —

> **gravar um sistema novo, do zero, nos dois slots.**

É o "flesha completamente". Serve para o caso que nenhum esquema de A/B resolve
sozinho: os dois slots corrompidos, ou você simplesmente querendo começar limpo. E
serve pelo navegador, do sofá, sem cartão nenhum saindo do lugar.

O recovery mora na FAT junto do initramfs e **nunca é atualizado por uma atualização
de sistema**. Ele é a coisa que precisa continuar funcionando quando todo o resto
falhou; atualizá-lo junto seria serrar o galho.

## 7. A primeira gravação, e por que ela ainda existe

Um cartão de uma partição só não vira um cartão de quatro partições sozinho: não se
reparticiona um disco montado por baixo do sistema que está rodando nele. Então esta
mudança custa **uma** gravação, a última.

Depois dela, a lista do que ainda exigiria o cartão no PC é curta e nenhum item é
um bug do nosso lado:

* o cartão morreu fisicamente;
* alguém escreveu lixo na partição FAT.

Bug em qualquer parte do sistema — inclusive no initramfs que escolhe o slot — sai
pela rede, porque a FAT também é escrita pelo atualizador.

## 8. O que a primeira imagem já traz pronta

A imagem sobe com **os dois slots iguais e bootáveis**. Não existe um estado inicial
em que só um slot presta: a primeira atualização já tem para onde ir e de onde
voltar.

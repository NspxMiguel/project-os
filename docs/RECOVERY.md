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

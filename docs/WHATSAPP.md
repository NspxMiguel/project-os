# Bot de WhatsApp — o que ele é e o que ele não é

Este documento existe porque "bot de zap" esconde uma decisão desconfortável: **não existe
API oficial, gratuita e auto-hospedável de WhatsApp.** Toda escolha aqui é uma escolha entre
dois jeitos ruins de um jeito diferente, e o app não finge que um deles é de graça.

---

## 1. Os dois caminhos, sem maquiagem

| | Cloud API (Meta) | Ponte local (bridge) |
|---|---|---|
| Custo | Grátis até um limite de conversas/mês, depois cobra por conversa | Grátis |
| Precisa de | Conta de negócios da Meta, número verificado, aprovação | Nada além de rodar a ponte |
| Estabilidade | Oficial, suportada, não quebra do nada | Depende de engenharia reversa de terceiro (whatsapp-web.js, Baileys, WPPConnect); quebra quando a Meta muda algo |
| Risco pro número | Nenhum — é a API de verdade | A Meta pode banir o número. É automação disfarçada de app oficial, e ela sabe detectar isso |
| Onde roda | Nuvem da Meta; o Pi só fala HTTP com ela | Um processo Node (container, outro serviço) que **o project-os não hospeda** |

Nenhuma opção é "a certa". Cloud API é o caminho de quem tem paciência para o cadastro e não
se importa de pagar depois de um tempo. Ponte local é o caminho de quem quer testar hoje sem
burocracia e aceita que o número pode ser banido — normalmente vale usar um número secundário,
não o pessoal.

---

## 2. Por isso o app é um encaixe de encaixe: provedores

`project_os/apps/whatsapp-bot/providers/` define uma interface pequena (`connect`,
`disconnect`, `status`, `send_text`, mais três ganchos de webhook) e três implementações:

- **`null`** — não fala com nada, só registra a mensagem. É o padrão. O app instala,
  inicia e é testável sem nenhuma credencial.
- **`cloud_api`** — Meta de verdade. Envia via `POST /{phone_number_id}/messages` na Graph
  API; recebe via webhook, com verificação de assinatura `X-Hub-Signature-256` (HMAC-SHA256
  contra o `app_secret`, comparação em tempo constante) e o handshake `hub.challenge` que a
  Meta exige antes de aceitar o endpoint.
- **`bridge`** — fala HTTP com uma ponte que **você** roda em outro lugar. Contrato mínimo:
  `POST {base_url}/send` para enviar, e a ponte chama de volta o webhook deste app com um
  header `X-Bridge-Token` para provar quem ela é. O project-os não sobe o processo Node — só
  conversa com ele.

Trocar de provedor é mudar um campo de configuração, nunca uma reescrita.

---

## 3. Sobre o container: o que este app é, hoje

`docs/CONCEITO.md` (seção 9) registra o bot de zap como exemplo de "app grande de terceiro,
roda em contêiner" — junto com Jellyfin e Node-RED. O que está implementado aqui é diferente:
um app nativo em Python, `kind: builtin` no catálogo, porque é isso que `catalog.yaml` já
declarava antes desta implementação começar. Não mudei o catálogo — não era escopo desta
tarefa. Se a decisão da seção 9 for para valer literalmente, o próximo passo é migrar este
app (ou pelo menos a ponte Node, que é a parte que de fato pesa) para `kind: service`/contêiner
mais tarde; hoje ele é leve o bastante (nenhuma dependência obrigatória, `httpx` é opcional e
só entra em uso quando um provedor de verdade está configurado) para não doer rodando nativo.

---

## 4. Segurança: três decisões, por escrito

- **Lista de permissão vazia por padrão.** Um bot que responde a qualquer número no minuto em
  que é instalado é um problema morando no bolso de alguém. Ninguém fala com ele até o dono
  cadastrar o número.
- **Assinatura do webhook, sempre.** A rota `POST /webhook` não pede login do project-os —
  não tem como a Meta ou a ponte apresentar um cookie de sessão — então ela é protegida pela
  assinatura/token do próprio provedor. Sem isso, o webhook seria um jeito de qualquer um na
  internet mandar mensagem "recebida" fabricada.
- **Segredos nomeados para serem redigidos.** `access_token`, `verify_token`, `app_secret` e
  `bridge.token` batem no filtro de `project_os/config.py` (`is_secret_key`) e voltam como
  `********` em qualquer leitura de configuração — o painel nunca vê o valor real depois de
  salvo.

---

## 5. Comandos

O prefixo é configurável (`!` por padrão). Três comandos vêm prontos:

- `!help` — lista os comandos disponíveis.
- `!status` — CPU, RAM, temperatura e disco, lendo `project_os/core/sysinfo.py`.
- `!apps` — quais apps instalados estão rodando agora.

Outros apps registram comando sem importar este módulo (o id tem hífen, então nem daria para
importar direto): publicam no barramento de eventos —

```python
ctx.bus.publish_nowait("whatsapp.command.register", {
    "name": "luzes",
    "help": "luzes <on|off> — luzes da sala",
    "handler": meu_handler,
})
```

— e o whatsapp-bot escuta esse tópico em segundo plano e adiciona o comando à mesma tabela
dos embutidos.

---

## 6. O que degrada sem `httpx`

`httpx` é opcional e só é importado dentro dos métodos que precisam de rede (`send_text` dos
provedores `cloud_api` e `bridge`). Sem ele instalado: o app sobe normalmente, o provedor
`null` funciona como sempre, e tentar enviar por `cloud_api`/`bridge` falha com um
`ProviderError` legível ("...pip install httpx") em vez de um erro de importação no boot.

# tk

Fila local de tickets com imagens, para agents de código multimodais.

Você cria o ticket no terminal colando um print do clipboard. Abre o Claude Code
ou o Codex e manda pegar a próxima. O agent recebe título, descrição **e a imagem
como anexo de verdade** — não um caminho de arquivo que ele não consegue abrir.

Sem Docker. Sem Postgres. Sem token. Sem daemon. Um arquivo SQLite em `~/.tickets/`.

```
tk new "Botão de logout não responde no iOS 17" -k mobile
  ↑ tira o print antes (Cmd+Shift+Ctrl+4) — ele cola sozinho

tk web        # board em http://localhost:7777, arrasta entre colunas

claude
> pega a próxima task e executa
```

## Por que existe

Nenhum kanban de terminal resolve as duas coisas ao mesmo tempo: colar imagem do
clipboard **e** entregar essa imagem pro modelo enxergar.

Os TUIs (`zaloog/kanban-tui`, `cainban`, `kb`) guardam a imagem como caminho em
markdown — o modelo recebe a string `![](/tmp/foo.png)` e não vê nada. O Cline
Kanban dispara os agents sozinho, que é o oposto de controle manual. O Planka é
uma plataforma de equipe com dois containers pra servir uma fila de uma pessoa.

Então: 600 linhas, um arquivo, três interfaces no mesmo banco.

## Instalação

macOS. Um comando:

```bash
curl -fsSL https://raw.githubusercontent.com/dominiquedutra/tk/main/install.sh -o /tmp/tk-install.sh \
  && less /tmp/tk-install.sh \
  && bash /tmp/tk-install.sh
```

Lê antes de rodar. Vale pra qualquer script que alguém te manda.

O instalador põe `tk` e `tk-mcp` no `~/.local/bin`, cria um venv com o SDK do MCP,
e registra o servidor no Claude Code (`~/.claude.json`) e no Codex
(`~/.codex/config.toml`) — **fazendo merge**, seus outros MCP servers ficam intactos.
É idempotente: rodar de novo atualiza sem perder ticket.

Depois, reinicia o Claude Code e o Codex. Eles só leem config de MCP no boot.

Dependências: `python3`, `pngpaste` (o instalador põe via Homebrew), e `sips`,
que já vem no macOS.

## Uso

### Terminal

```bash
tk new "título" -k mobile          # cola a imagem do clipboard automaticamente
tk new "título" --no-paste         # só texto
tk new "título" -i foto.png        # arquivo específico
tk new "título" -b "descrição"

tk ls                              # tudo
tk ls -s todo                      # só uma coluna
tk show 3
tk mv 3 done
tk ok 7                            # aprova o que o agent levantou na triagem
tk rm 7
```

### Web

```bash
tk web                             # http://localhost:7777
```

Board de cinco colunas. Arrasta card entre elas. Clica pra abrir, ver a imagem
inteira e os comentários. No formulário de ticket novo, `Cmd+V` cola imagem direto
— ou arrasta o arquivo.

### Agents

Seis tools no MCP `tickets`:

| tool | o que faz |
|---|---|
| `list_tickets(status, kind)` | lista resumida — barata em tokens |
| `next_ticket(worker, kind)` | pega o próximo e marca `doing`. Atômico. |
| `get_ticket(id)` | lê um específico, com as imagens |
| `advance_ticket(id, status)` | move entre colunas |
| `comment_ticket(id, text)` | registra o que foi feito |
| `raise_ticket(title, body, kind)` | agent registra o que ele mesmo achou → vai pra triagem |

Pra orientar o agent no seu projeto, copia o [`AGENTS.md`](AGENTS.md) deste repo
pra raiz do seu. O Claude Code lê `CLAUDE.md`, o Codex lê `AGENTS.md` — um symlink
resolve os dois:

```bash
cp AGENTS.md /seu/projeto/AGENTS.md
cd /seu/projeto && ln -s AGENTS.md CLAUDE.md
```

## As colunas

```
triagem → a fazer → fazendo → feito
                       ↓
                    travado
```

**triagem** é onde o agent deposita o que ele levanta sozinho durante o trabalho.
`next_ticket` nunca puxa de lá. Você aprova com `tk ok <id>` ou descarta com
`tk rm <id>`. Isso existe pra que o agent nunca crie trabalho pra si mesmo — se ele
pudesse escrever direto em "a fazer", a próxima chamada pegaria a sugestão dele e
começaria a executar sem ninguém ter olhado.

## Decisões que valem explicar

**Imagem redimensionada antes de servir.** Print de tela Retina tem 3-5 MB. Em
base64 viram ~5 MB de contexto, e dois tickets entopem a janela do modelo. O `sips`
reduz pro teto de 1568 px (limite prático de visão) e converte pra JPEG. O original
fica intacto no disco; o modelo recebe a versão leve. Na prática: 69 KB → 26 KB.

**Imagem vai como content block, não como base64 dentro de JSON.** Se o tool
devolvesse `{"image": "iVBORw0KG..."}`, o modelo veria uma string gigante de lixo.
O MCP tem tipo próprio pra imagem, e é ele que faz o modelo enxergar de fato.

**Claim atômico com `BEGIN IMMEDIATE`.** Dois agents na mesma fila pegam tickets
diferentes, sempre. O segundo espera o write-lock do SQLite e leva o próximo da
fila. Sem race, sem retry, sem campo de versão.

**Reaper preguiçoso.** Ticket parado em `fazendo` há mais de 45 min volta pra fila.
Roda junto com o `next_ticket`, não tem processo de fundo. Agent que crashou não
manda heartbeat — então heartbeat não resolveria nada.

**Dedup por hash.** Mesma imagem colada dez vezes ocupa espaço uma vez.

## Dados

```
~/.tickets/
├── db.sqlite3      tickets, imagens, comentários
└── img/
    ├── <sha>.png         original
    └── <sha>.serve.jpg   versão que vai pro modelo
```

Backup é copiar a pasta. Migração de schema é automática ao abrir o banco.

## Licença

MIT.

# Instruções do projeto

> Copie este arquivo para a raiz do seu projeto.
> O Codex lê `AGENTS.md`; o Claude Code lê `CLAUDE.md`. Para servir aos dois:
> `ln -s AGENTS.md CLAUDE.md`

## Tickets

O trabalho vem do kanban local, via MCP `tickets`. Não invente tarefa: quando eu
disser "pega a próxima", chama `next_ticket` e trabalha no que vier — título,
descrição e os prints anexados são o requisito, não ilustração.

Um ticket por sessão. Ao terminar, comenta o que fez e fecha com `advance_ticket`.
Travou? Marca `blocked` e diz por quê. Não deixa em `doing` no escuro.

Achou outra coisa quebrada no caminho? `raise_ticket`. Cai na triagem pro meu aval,
não vira trabalho de ninguém. Anota e segue no que estava fazendo.

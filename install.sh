#!/usr/bin/env bash
# ============================================================
#  tk — fila local de tickets com imagens, para agents multimodais
#
#  Instala:
#    ~/.local/share/tickets/tickets.py   o programa
#    ~/.local/bin/tk                     CLI + web
#    ~/.local/bin/tk-mcp                 MCP server (stdio)
#  Registra o MCP no Claude Code e no Codex — merge, nunca sobrescreve.
#  Dados em ~/.tickets/ — preservados entre instalações.
#
#  Idempotente. Rode de novo pra atualizar.
# ============================================================
set -euo pipefail

REPO="${TK_REPO:-dominiquedutra/tk}"
REF="${TK_REF:-main}"
SRC="https://raw.githubusercontent.com/${REPO}/${REF}/tickets.py"

SHARE="$HOME/.local/share/tickets"
BIN="$HOME/.local/bin"

say()  { printf '\033[36m→\033[0m %s\n' "$1"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!\033[0m %s\n' "$1"; }

[[ "$(uname)" == "Darwin" ]] || warn "feito pra macOS; em outro SO o paste e o resize não funcionam"

say "checando dependências"
command -v python3 >/dev/null || { echo "python3 não encontrado"; exit 1; }
if ! command -v pngpaste >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    say "instalando pngpaste (colar imagem do clipboard)"
    brew install pngpaste
  else
    warn "pngpaste ausente e sem Homebrew — 'tk new' vai precisar de --img PATH"
  fi
fi
command -v sips >/dev/null 2>&1 || warn "sips ausente — imagens não serão redimensionadas"

mkdir -p "$SHARE" "$BIN"

say "baixando tickets.py de ${REPO}@${REF}"
if [[ -f "$(dirname "$0")/tickets.py" ]]; then
  cp "$(dirname "$0")/tickets.py" "$SHARE/tickets.py"   # instalação a partir do clone
  ok "copiado do diretório local"
else
  curl -fsSL "$SRC" -o "$SHARE/tickets.py"
  ok "baixado"
fi
python3 -c "import ast,sys; ast.parse(open('$SHARE/tickets.py').read())" \
  || { echo "arquivo baixado está corrompido"; exit 1; }

say "criando venv e instalando o SDK do MCP"
python3 -m venv "$SHARE/venv"
"$SHARE/venv/bin/pip" install --quiet --upgrade pip
"$SHARE/venv/bin/pip" install --quiet mcp
ok "venv pronto ($("$SHARE/venv/bin/python" -c 'import importlib.metadata as m; print("mcp", m.version("mcp"))'))"

cat > "$BIN/tk" <<EOF
#!/usr/bin/env bash
exec "$SHARE/venv/bin/python" "$SHARE/tickets.py" "\$@"
EOF
chmod +x "$BIN/tk"

cat > "$BIN/tk-mcp" <<EOF
#!/usr/bin/env bash
exec "$SHARE/venv/bin/python" "$SHARE/tickets.py" mcp
EOF
chmod +x "$BIN/tk-mcp"
ok "tk e tk-mcp em $BIN"

# ---- Claude Code: merge no JSON, com backup ------------------------
say "registrando MCP no Claude Code"
"$SHARE/venv/bin/python" - "$HOME/.claude.json" "$BIN/tk-mcp" <<'PY'
import json, sys, pathlib, shutil
cfg, cmd = pathlib.Path(sys.argv[1]), sys.argv[2]
d = {}
if cfg.exists():
    shutil.copy(cfg, cfg.with_suffix(".json.bak"))
    try:
        d = json.loads(cfg.read_text() or "{}")
    except json.JSONDecodeError:
        print("  config ilegível; preservada em .bak, criando nova")
        d = {}
servers = d.setdefault("mcpServers", {})
if servers.get("tickets", {}).get("command") == cmd:
    print("  já registrado")
else:
    servers["tickets"] = {"command": cmd, "args": []}
    cfg.write_text(json.dumps(d, indent=2))
    print(f"  tickets adicionado — {len(servers)} server(s), os outros preservados")
PY

# ---- Codex: append só se ainda não existe --------------------------
say "registrando MCP no Codex"
CODEX="$HOME/.codex/config.toml"
mkdir -p "$HOME/.codex"
if [[ -f "$CODEX" ]] && grep -q '^\[mcp_servers\.tickets\]' "$CODEX"; then
  echo "  já registrado"
else
  [[ -f "$CODEX" ]] && cp "$CODEX" "$CODEX.bak" && echo "  backup em $CODEX.bak"
  printf '\n[mcp_servers.tickets]\ncommand = "%s"\n' "$BIN/tk-mcp" >> "$CODEX"
  echo "  tickets adicionado"
fi

# ---- PATH ----------------------------------------------------------
PATH_OK=1
echo "$PATH" | tr ':' '\n' | grep -qx "$BIN" || PATH_OK=0
if [[ $PATH_OK -eq 0 ]]; then
  RC="$HOME/.zshrc"
  grep -q "\.local/bin" "$RC" 2>/dev/null || echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$RC"
  warn "PATH atualizado em $RC"
fi

# ---- smoke test ----------------------------------------------------
say "testando"
"$BIN/tk" ls >/dev/null && ok "CLI responde (migração de schema roda ao abrir o banco)"

cat <<BANNER

────────────────────────────────────────────────────────────
 pronto.
$( [[ $PATH_OK -eq 0 ]] && printf ' \033[33mabre um terminal novo\033[0m (ou: source ~/.zshrc)\n' )
 TERMINAL
   tk new "Botão logout quebrado" -k mobile
       tira o print antes (Cmd+Shift+Ctrl+4) — cola sozinho
       --no-paste   só texto        -i foto.png   arquivo
   tk ls · tk show 1 · tk mv 1 done
   tk ok 3          aprova o que o agent levantou na triagem

 WEB
   tk web              →  http://localhost:7777
       arrasta card entre colunas; Cmd+V cola imagem no form

 AGENTS   (reinicia Claude Code e Codex — só leem MCP no boot)
   claude  →  "pega a próxima task e executa"
   codex   →  /mcp   deve listar  tickets

   copia o AGENTS.md do repo pra raiz do teu projeto:
   https://github.com/${REPO}/blob/${REF}/AGENTS.md

 dados em ~/.tickets/
────────────────────────────────────────────────────────────
BANNER

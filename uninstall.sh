#!/usr/bin/env bash
# ============================================================
#  tk — desinstalador
#
#  Remove tudo que o install.sh criou, cirurgicamente:
#  outros MCP servers no Claude Code e no Codex ficam intactos.
#
#  Uso:
#    bash tk-uninstall.sh              pergunta sobre os dados
#    bash tk-uninstall.sh --purge      apaga os dados também
#    bash tk-uninstall.sh --keep-data  mantém ~/.tickets
# ============================================================
set -euo pipefail

SHARE="$HOME/.local/share/tickets"
BIN="$HOME/.local/bin"
DATA="$HOME/.tickets"

MODE="ask"
case "${1:-}" in
  --purge) MODE="purge" ;;
  --keep-data) MODE="keep" ;;
esac

say() { printf '\033[36m→\033[0m %s\n' "$1"; }
ok()  { printf '\033[32m✓\033[0m %s\n' "$1"; }
skip(){ printf '\033[90m·\033[0m %s\n' "$1"; }

# ---- 1. binários e programa ----------------------------------------
for f in "$BIN/tk" "$BIN/tk-mcp"; do
  [[ -e "$f" ]] && rm -f "$f" && ok "removido $f" || skip "não existia: $f"
done
[[ -d "$SHARE" ]] && rm -rf "$SHARE" && ok "removido $SHARE (programa + venv)" \
                  || skip "não existia: $SHARE"

# ---- 2. Claude Code: tira só a chave tickets -----------------------
CJ="$HOME/.claude.json"
if [[ -f "$CJ" ]]; then
  python3 - "$CJ" <<'PY'
import json, sys, pathlib, shutil
p = pathlib.Path(sys.argv[1])
try:
    d = json.loads(p.read_text() or "{}")
except json.JSONDecodeError:
    print("  \033[33m!\033[0m ~/.claude.json ilegível — não mexi nele"); sys.exit(0)
srv = d.get("mcpServers", {})
if "tickets" in srv:
    shutil.copy(p, p.with_suffix(".json.bak-uninstall"))
    del srv["tickets"]
    p.write_text(json.dumps(d, indent=2))
    print(f"  \033[32m✓\033[0m tickets removido do Claude Code — {len(srv)} server(s) preservado(s)")
else:
    print("  \033[90m·\033[0m Claude Code: tickets não estava registrado")
PY
else
  skip "~/.claude.json não existe"
fi

# ---- 3. Codex: tira só o bloco [mcp_servers.tickets] ---------------
CT="$HOME/.codex/config.toml"
if [[ -f "$CT" ]] && grep -q '^\[mcp_servers\.tickets\]' "$CT"; then
  cp "$CT" "$CT.bak-uninstall"
  python3 - "$CT" <<'PY'
import sys, pathlib, re
p = pathlib.Path(sys.argv[1]); t = p.read_text()
# remove o bloco até o próximo [header] ou fim do arquivo
out = re.sub(r'\n*^\[mcp_servers\.tickets\]\s*\n(?:(?!^\[).*\n?)*', '\n', t, flags=re.M)
p.write_text(out.strip() + "\n")
print("  \033[32m✓\033[0m tickets removido do Codex (backup em config.toml.bak-uninstall)")
PY
else
  skip "Codex: tickets não estava registrado"
fi

# ---- 4. dados -------------------------------------------------------
if [[ -d "$DATA" ]]; then
  N=$(sqlite3 "$DATA/db.sqlite3" "SELECT COUNT(*) FROM tickets" 2>/dev/null || echo "?")
  SZ=$(du -sh "$DATA" 2>/dev/null | cut -f1)
  case "$MODE" in
    purge) rm -rf "$DATA"; ok "removido $DATA ($N tickets, $SZ)" ;;
    keep)  skip "mantido $DATA ($N tickets, $SZ)" ;;
    ask)
      echo
      read -r -p "Apagar os dados? $DATA — $N tickets, $SZ  [s/N] " R
      if [[ "$R" =~ ^[SsYy]$ ]]; then rm -rf "$DATA"; ok "removido"; else skip "mantido"; fi ;;
  esac
else
  skip "$DATA não existe"
fi

# ---- 5. o que sobrou pra você decidir ------------------------------
cat <<EOF

────────────────────────────────────────────────────────────
 desinstalado.

 O que eu NÃO toquei, de propósito:

 · ~/.zshrc — pode ter uma linha "export PATH=...local/bin".
   É inofensiva e provavelmente útil pra outras coisas. Se quiser
   tirar:  grep -n 'local/bin' ~/.zshrc

 · AGENTS.md / CLAUDE.md do seu projeto — se você colou o bloco
   "## Tickets" lá, apague à mão. Não mexo em arquivo do seu repo.

 · Backups: ~/.claude.json.bak-uninstall e
   ~/.codex/config.toml.bak-uninstall (se havia o que remover).

 Reinicie o Claude Code e o Codex pra eles esquecerem o servidor.
────────────────────────────────────────────────────────────
EOF

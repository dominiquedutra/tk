#!/usr/bin/env python3
"""
tickets — fila local de tickets com imagens, para agents multimodais.

Um arquivo. SQLite. Sem Docker, sem servidor permanente, sem token.

  tickets.py new "título" [-k web|mobile] [-b corpo] [-i img.png ...] [--no-paste]
  tickets.py ls [-s todo] [-k mobile]
  tickets.py show <id>
  tickets.py mv <id> <todo|doing|done|blocked>
  tickets.py rm <id>
  tickets.py web [-p 7777]
  tickets.py mcp
"""
import os
import sys
import json
import base64
import sqlite3
import hashlib
import pathlib
import argparse
import subprocess
import mimetypes

HOME = pathlib.Path(os.environ.get("TICKETS_HOME", pathlib.Path.home() / ".tickets"))
DB_PATH = HOME / "db.sqlite3"
IMG_DIR = HOME / "img"

# triage = levantado pelo agent, esperando teu aval. next_ticket NUNCA puxa daqui.
STATUSES = ("triage", "todo", "doing", "done", "blocked")
KINDS = ("web", "mobile", "infra", "other")
CLAIM_TTL_MIN = 45          # ticket preso em doing volta pra todo depois disso
SERVE_MAX_PX = 1568         # limite prático pra visão multimodal

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tickets (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL DEFAULT 'web',
  status     TEXT NOT NULL DEFAULT 'todo',
  origin     TEXT NOT NULL DEFAULT 'human',
  claimed_by TEXT,
  claimed_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS images (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  sha256    TEXT NOT NULL,
  orig      TEXT NOT NULL,
  serve     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  author    TEXT NOT NULL DEFAULT '',
  text      TEXT NOT NULL,
  at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_status ON tickets(status, id);
"""


# ---------------------------------------------------------------- db

# colunas adicionadas depois da v1 — CREATE TABLE IF NOT EXISTS não altera
# tabela existente, então cada uma precisa de ALTER explícito.
MIGRATIONS = [
    ("tickets", "origin", "TEXT NOT NULL DEFAULT 'human'"),
]


def _migrate(c):
    for table, col, decl in MIGRATIONS:
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def db():
    HOME.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    _migrate(c)
    return c


# ------------------------------------------------------------- imagens

def _resize(src: pathlib.Path, dst: pathlib.Path) -> bool:
    """Reduz pra servir ao modelo. sips é nativo do macOS."""
    try:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82",
             "-Z", str(SERVE_MAX_PX), str(src), "--out", str(dst)],
            check=True, capture_output=True, timeout=30,
        )
        return dst.exists()
    except Exception:
        return False


def store_image(path) -> dict:
    raw = pathlib.Path(path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()[:16]
    ext = pathlib.Path(path).suffix.lower() or ".png"
    orig = IMG_DIR / f"{sha}{ext}"
    serve = IMG_DIR / f"{sha}.serve.jpg"
    if not orig.exists():
        orig.write_bytes(raw)
    if not serve.exists() and not _resize(orig, serve):
        serve = orig                      # sem sips: serve o original
    return {"sha256": sha, "orig": str(orig), "serve": str(serve)}


def paste_clipboard() -> pathlib.Path | None:
    """pngpaste → arquivo temporário. None se o clipboard não tem imagem."""
    import tempfile
    tmp = pathlib.Path(tempfile.mkstemp(suffix=".png")[1])
    r = subprocess.run(["pngpaste", str(tmp)], capture_output=True)
    if r.returncode != 0 or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return None
    return tmp


# ---------------------------------------------------------------- core

def create(title, body="", kind="web", images=(), status="todo", origin="human") -> int:
    c = db()
    cur = c.execute(
        "INSERT INTO tickets (title, body, kind, status, origin) VALUES (?,?,?,?,?)",
        (title, body, kind if kind in KINDS else "web",
         status if status in STATUSES else "todo", origin),
    )
    tid = cur.lastrowid
    for p in images:
        m = store_image(p)
        c.execute(
            "INSERT INTO images (ticket_id, sha256, orig, serve) VALUES (?,?,?,?)",
            (tid, m["sha256"], m["orig"], m["serve"]),
        )
    return tid


def reap(c, ttl=CLAIM_TTL_MIN):
    c.execute(
        "UPDATE tickets SET status='todo', claimed_by=NULL, claimed_at=NULL "
        "WHERE status='doing' AND claimed_at IS NOT NULL "
        "AND claimed_at < datetime('now', ?)",
        (f"-{ttl} minutes",),
    )


def claim(worker="agent", kind=None):
    """Pega o próximo todo e marca doing. Atômico."""
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        reap(c)
        q = "SELECT id FROM tickets WHERE status='todo'"
        p = []
        if kind:
            q += " AND kind=?"
            p.append(kind)
        q += " ORDER BY id LIMIT 1"
        row = c.execute(q, p).fetchone()
        if not row:
            c.execute("COMMIT")
            return None
        c.execute(
            "UPDATE tickets SET status='doing', claimed_by=?, "
            "claimed_at=datetime('now') WHERE id=?",
            (worker, row["id"]),
        )
        c.execute("COMMIT")
        return row["id"]
    except Exception:
        c.execute("ROLLBACK")
        raise


def get(tid):
    c = db()
    t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        return None
    d = dict(t)
    d["images"] = [dict(r) for r in c.execute(
        "SELECT sha256, orig, serve FROM images WHERE ticket_id=?", (tid,))]
    d["comments"] = [dict(r) for r in c.execute(
        "SELECT author, text, at FROM comments WHERE ticket_id=? ORDER BY id", (tid,))]
    return d


def listing(status=None, kind=None, limit=50):
    c = db()
    q = ("SELECT t.id, t.title, t.kind, t.status, t.claimed_by, t.origin, "
         "(SELECT COUNT(*) FROM images WHERE ticket_id=t.id) AS n_img "
         "FROM tickets t WHERE 1=1")
    p = []
    if status:
        q += " AND t.status=?"
        p.append(status)
    if kind:
        q += " AND t.kind=?"
        p.append(kind)
    q += " ORDER BY t.id LIMIT ?"
    p.append(limit)
    return [dict(r) for r in c.execute(q, p)]


def advance(tid, status):
    if status not in STATUSES:
        raise ValueError(f"status inválido: {status} (use {'|'.join(STATUSES)})")
    c = db()
    if not c.execute("SELECT 1 FROM tickets WHERE id=?", (tid,)).fetchone():
        raise ValueError(f"ticket {tid} não existe")
    clear = status in ("todo", "done")
    c.execute(
        "UPDATE tickets SET status=?, claimed_by=CASE WHEN ? THEN NULL ELSE claimed_by END, "
        "claimed_at=CASE WHEN ? THEN NULL ELSE claimed_at END WHERE id=?",
        (status, clear, clear, tid),
    )
    return status


def comment(tid, text, author=""):
    c = db()
    if not c.execute("SELECT 1 FROM tickets WHERE id=?", (tid,)).fetchone():
        raise ValueError(f"ticket {tid} não existe")
    c.execute("INSERT INTO comments (ticket_id, author, text) VALUES (?,?,?)",
              (tid, author, text))


def delete(tid):
    db().execute("DELETE FROM tickets WHERE id=?", (tid,))


# ----------------------------------------------------------------- MCP

def run_mcp():
    # SDK v2 renomeou FastMCP -> MCPServer. Funciona nas duas.
    try:
        from mcp.server.mcpserver import MCPServer as Server, Image
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server, Image

    mcp = Server("tickets")

    def _meta(t):
        return {
            "id": t["id"], "title": t["title"], "kind": t["kind"],
            "status": t["status"], "body": t["body"],
            "comments": [f'{c["at"]} {c["author"]}: {c["text"]}' for c in t["comments"]],
            "n_images": len(t["images"]),
        }

    def _payload(t):
        """Texto + imagens como content blocks de verdade (não base64 em string)."""
        out = [json.dumps(_meta(t), ensure_ascii=False)]
        for im in t["images"]:
            p = pathlib.Path(im["serve"])
            if p.exists():
                fmt = "jpeg" if p.suffix in (".jpg", ".jpeg") else p.suffix.lstrip(".")
                out.append(Image(data=p.read_bytes(), format=fmt))
        return out

    @mcp.tool()
    def list_tickets(status: str = "todo", kind: str = "") -> str:
        """Lista tickets resumidos (id, título, tipo, se tem imagem). Barato em tokens."""
        try:
            rows = listing(status or None, kind or None)
            return json.dumps(rows, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    def next_ticket(worker: str = "agent", kind: str = "") -> list:
        """Pega o próximo ticket da fila e marca como doing. Retorna texto + imagens."""
        try:
            tid = claim(worker, kind or None)
            if tid is None:
                return [json.dumps({"empty": True, "message": "fila vazia"})]
            return _payload(get(tid))
        except Exception as e:
            return [json.dumps({"error": str(e)})]

    @mcp.tool()
    def get_ticket(id: int) -> list:
        """Lê um ticket específico sem mudar status. Retorna texto + imagens."""
        try:
            t = get(id)
            if not t:
                return [json.dumps({"error": f"ticket {id} não existe"})]
            return _payload(t)
        except Exception as e:
            return [json.dumps({"error": str(e)})]

    @mcp.tool()
    def advance_ticket(id: int, status: str) -> str:
        """Move o ticket: todo, doing, done ou blocked."""
        try:
            return json.dumps({"ok": True, "id": id, "status": advance(id, status)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    def comment_ticket(id: int, text: str, author: str = "agent") -> str:
        """Deixa um comentário no ticket (o que foi feito, decisões, PR)."""
        try:
            comment(id, text, author)
            return json.dumps({"ok": True})
        except Exception as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    def raise_ticket(title: str, body: str = "", kind: str = "web") -> str:
        """Registra algo que VOCÊ encontrou e precisa ser feito, mas está fora do
        escopo do ticket atual.

        Cai na coluna 'triagem', esperando aval humano. next_ticket nunca puxa de
        lá, então registrar aqui não cria trabalho automático pra ninguém.

        Não interrompa o que está fazendo: anote e siga. Descreva o suficiente pra
        ser acionável depois (arquivo, sintoma, por que importa). Não registre o
        que já faz parte do ticket em andamento.
        """
        try:
            tid = create(title, body, kind, status="triage", origin="agent")
            return json.dumps({"ok": True, "id": tid, "status": "triage"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    mcp.run()


# ----------------------------------------------------------------- web

PAGE = r"""<!doctype html><meta charset=utf-8>
<title>tickets</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e6e6;font:14px -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif}
header{padding:14px 18px;border-bottom:1px solid #22252c;display:flex;gap:12px;align-items:center}
h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
.muted{color:#6f7681}
button{background:#22252c;color:#e6e6e6;border:1px solid #2d323b;border-radius:6px;padding:5px 11px;cursor:pointer;font-size:13px}
button:hover{background:#2d323b}
button.p{background:#2f6feb;border-color:#2f6feb}
button.p:hover{background:#4079ee}
#board{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;padding:18px;align-items:start}
.col.triage h2{color:#d9a441}
.card.bot{border-left:2px solid #d9a441}
.col h2{font-size:11px;text-transform:uppercase;letter-spacing:.9px;color:#8b93a0;margin:0 0 10px;font-weight:600}
.card{background:#171a20;border:1px solid #22252c;border-radius:8px;padding:11px;margin-bottom:9px;cursor:grab}
.card:active{cursor:grabbing}
.card.drag{opacity:.35}
.col{min-height:90px;padding:6px;border-radius:9px;border:1px solid transparent;transition:background .12s,border-color .12s}
.col.over{background:#12151b;border-color:#2f6feb}
.card:hover{border-color:#39404b}
.card .t{font-weight:500;line-height:1.35;margin-bottom:7px}
.tag{display:inline-block;font-size:10px;padding:2px 7px;border-radius:99px;background:#22252c;color:#9aa3b0;text-transform:uppercase;letter-spacing:.5px}
.thumbs{display:flex;gap:5px;margin-top:8px;flex-wrap:wrap}
.thumbs img{width:58px;height:58px;object-fit:cover;border-radius:5px;border:1px solid #2d323b}
dialog{background:#171a20;color:#e6e6e6;border:1px solid #2d323b;border-radius:10px;padding:0;max-width:640px;width:92vw}
dialog::backdrop{background:rgba(0,0,0,.65)}
.dh{padding:16px 18px;border-bottom:1px solid #22252c}
.db{padding:16px 18px;max-height:62vh;overflow:auto}
.df{padding:12px 18px;border-top:1px solid #22252c;display:flex;gap:8px;flex-wrap:wrap}
.db img{max-width:100%;border-radius:6px;margin:8px 0;border:1px solid #2d323b}
input,textarea,select{width:100%;background:#0f1115;color:#e6e6e6;border:1px solid #2d323b;border-radius:6px;padding:9px;font:inherit;margin-bottom:9px}
textarea{min-height:90px;resize:vertical}
#drop{border:1px dashed #39404b;border-radius:7px;padding:16px;text-align:center;color:#6f7681;font-size:13px}
#drop.on{border-color:#2f6feb;color:#8fb4ff}
pre{white-space:pre-wrap;word-wrap:break-word;margin:0;font:inherit}
.cmt{border-left:2px solid #2d323b;padding:3px 0 3px 10px;margin:7px 0;color:#9aa3b0;font-size:13px}
</style>
<header>
  <h1>tickets</h1>
  <button class=p onclick="openNew()">+ novo</button>
  <span class=muted id=stat></span>
</header>
<div id=board></div>

<dialog id=dlg><div class=dh><b id=dt></b></div><div class=db id=dbody></div><div class=df id=dfoot></div></dialog>

<dialog id=ndlg>
  <div class=dh><b>novo ticket</b></div>
  <div class=db>
    <input id=ntitle placeholder="título" autofocus>
    <textarea id=nbody placeholder="descrição (opcional)"></textarea>
    <select id=nkind><option>web</option><option>mobile</option><option>infra</option><option>other</option></select>
    <div id=drop>cola a imagem aqui (⌘V) ou arrasta</div>
    <div class=thumbs id=nthumbs></div>
  </div>
  <div class=df>
    <button class=p onclick="saveNew()">criar</button>
    <button onclick="ndlg.close()">cancelar</button>
  </div>
</dialog>

<script>
const COLS=[["triage","triagem"],["todo","a fazer"],["doing","fazendo"],["done","feito"],["blocked","travado"]];
let imgs=[];

async function load(){
  const r=await fetch('/api/tickets');const all=await r.json();
  board.innerHTML=COLS.map(([k,label])=>{
    const cs=all.filter(t=>t.status===k);
    return `<div class="col ${k}" ondragover="dOver(event,this)" ondragleave="this.classList.remove('over')" ondrop="dDrop(event,'${k}',this)">`
      +`<h2>${label} · ${cs.length}</h2>`+cs.map(card).join('')+`</div>`;
  }).join('');
  stat.textContent=all.length+' tickets';
}
function card(t){
  const th=(t.images||[]).map(s=>`<img src="/img/${s}">`).join('');
  const bot=t.origin==='agent';
  return `<div class="card${bot?' bot':''}" draggable="true"
    ondragstart="dStart(event,${t.id})" ondragend="dEnd(event)"
    onclick="maybeOpen(${t.id})">
    <div class=t>${esc(t.title)}</div>
    <span class=tag>${t.kind}</span>
    ${bot?'<span class=tag title="levantado pelo agent">🤖</span>':''}
    ${t.claimed_by?`<span class=tag>${esc(t.claimed_by)}</span>`:''}
    ${th?`<div class=thumbs>${th}</div>`:''}</div>`;
}
async function openT(id){
  const t=await (await fetch('/api/ticket/'+id)).json();
  dt.textContent='#'+t.id+' · '+t.title;
  dbody.innerHTML=(t.body?`<pre>${esc(t.body)}</pre>`:'<span class=muted>sem descrição</span>')
    +(t.images||[]).map(s=>`<img src="/img/${s}">`).join('')
    +(t.comments||[]).map(c=>`<div class=cmt><b>${esc(c.author||'—')}</b> ${c.at}<br>${esc(c.text)}</div>`).join('');
  dfoot.innerHTML=COLS.filter(([k])=>k!==t.status)
    .map(([k,l])=>`<button onclick="mv(${t.id},'${k}')">→ ${l}</button>`).join('')
    +`<button onclick="del(${t.id})" style="margin-left:auto">excluir</button>`;
  dlg.showModal();
}
async function mv(id,s){await fetch('/api/ticket/'+id+'/status',{method:'POST',body:s});dlg.close();load()}
async function del(id){if(!confirm('excluir?'))return;await fetch('/api/ticket/'+id,{method:'DELETE'});dlg.close();load()}
function openNew(){ntitle.value='';nbody.value='';imgs=[];nthumbs.innerHTML='';ndlg.showModal()}
async function saveNew(){
  if(!ntitle.value.trim())return ntitle.focus();
  await fetch('/api/tickets',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({title:ntitle.value,body:nbody.value,kind:nkind.value,images:imgs})});
  ndlg.close();load();
}
function addImg(f){const r=new FileReader();r.onload=e=>{imgs.push(e.target.result);
  nthumbs.innerHTML+=`<img src="${e.target.result}">`};r.readAsDataURL(f)}
document.addEventListener('paste',e=>{if(!ndlg.open)return;
  for(const it of e.clipboardData.items)if(it.type.startsWith('image/'))addImg(it.getAsFile())});
drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('on')});
drop.addEventListener('dragleave',()=>drop.classList.remove('on'));
drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('on');
  for(const f of e.dataTransfer.files)if(f.type.startsWith('image/'))addImg(f)});
function esc(s){return (s||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}
// ---- drag & drop entre colunas ----
let dragId=null, dragging=false;
function dStart(e,id){
  dragId=id; dragging=true;
  e.dataTransfer.effectAllowed='move';
  e.dataTransfer.setData('text/plain',String(id));
  e.currentTarget.classList.add('drag');
}
function dEnd(e){
  e.currentTarget.classList.remove('drag');
  document.querySelectorAll('.col.over').forEach(c=>c.classList.remove('over'));
  setTimeout(()=>{dragging=false;dragId=null},60);   // segura o click fantasma
}
function dOver(e,el){ e.preventDefault(); e.dataTransfer.dropEffect='move'; el.classList.add('over'); }
async function dDrop(e,status,el){
  e.preventDefault(); el.classList.remove('over');
  const id=dragId!=null?dragId:parseInt(e.dataTransfer.getData('text/plain'),10);
  if(!id) return;
  dragId=null;
  await fetch('/api/ticket/'+id+'/status',{method:'POST',body:status});
  load();
}
function maybeOpen(id){ if(!dragging) openT(id); }

load();
// não recarrega no meio de um arrasto nem com modal aberto
setInterval(()=>{ if(!dragging && !dlg.open && !ndlg.open) load(); },5000);
</script>"""


def run_web(port=7777):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if p == "/api/tickets":
                out = []
                for t in listing(limit=500):
                    full = get(t["id"])
                    t["images"] = [i["sha256"] for i in full["images"]]
                    out.append(t)
                return self._send(200, json.dumps(out))
            if p.startswith("/api/ticket/"):
                t = get(int(p.split("/")[3]))
                return self._send(200, json.dumps(t) if t else '{"error":"nao existe"}')
            if p.startswith("/img/"):
                sha = p.split("/")[2]
                c = db()
                r = c.execute("SELECT serve FROM images WHERE sha256=? LIMIT 1", (sha,)).fetchone()
                if r and pathlib.Path(r["serve"]).exists():
                    f = pathlib.Path(r["serve"])
                    ct = mimetypes.guess_type(str(f))[0] or "image/jpeg"
                    return self._send(200, f.read_bytes(), ct)
                return self._send(404, b"", "text/plain")
            self._send(404, b"", "text/plain")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            p = self.path
            if p == "/api/tickets":
                d = json.loads(raw)
                import tempfile
                paths = []
                for durl in d.get("images", []):
                    b64 = durl.split(",", 1)[-1]
                    tmp = pathlib.Path(tempfile.mkstemp(suffix=".png")[1])
                    tmp.write_bytes(base64.b64decode(b64))
                    paths.append(tmp)
                tid = create(d["title"], d.get("body", ""), d.get("kind", "web"), paths)
                for t in paths:
                    t.unlink(missing_ok=True)
                return self._send(200, json.dumps({"id": tid}))
            if p.endswith("/status"):
                tid = int(p.split("/")[3])
                advance(tid, raw.decode())
                return self._send(200, '{"ok":true}')
            self._send(404, b"", "text/plain")

        def do_DELETE(self):
            if self.path.startswith("/api/ticket/"):
                delete(int(self.path.split("/")[3]))
                return self._send(200, '{"ok":true}')
            self._send(404, b"", "text/plain")

    db()
    print(f"→ http://localhost:{port}   (ctrl-c para parar)")
    HTTPServer(("127.0.0.1", port), H).serve_forever()


# ----------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(prog="tk", description="fila local de tickets")
    sub = ap.add_subparsers(dest="cmd")

    n = sub.add_parser("new", help="cria ticket (cola imagem do clipboard por padrão)")
    n.add_argument("title")
    n.add_argument("-k", "--kind", default="web", choices=KINDS)
    n.add_argument("-b", "--body", default="")
    n.add_argument("-i", "--img", action="append", default=[])
    n.add_argument("--no-paste", action="store_true")

    l = sub.add_parser("ls", help="lista tickets")
    l.add_argument("-s", "--status", default=None, choices=STATUSES)
    l.add_argument("-k", "--kind", default=None, choices=KINDS)

    s = sub.add_parser("show", help="detalhe do ticket")
    s.add_argument("id", type=int)

    m = sub.add_parser("mv", help="muda status")
    m.add_argument("id", type=int)
    m.add_argument("status", choices=STATUSES)

    o = sub.add_parser("ok", help="aprova da triagem: manda pra fila (todo)")
    o.add_argument("id", type=int)

    d = sub.add_parser("rm", help="apaga ticket")
    d.add_argument("id", type=int)

    w = sub.add_parser("web", help="abre o board no browser")
    w.add_argument("-p", "--port", type=int, default=7777)

    sub.add_parser("mcp", help="roda como MCP server (stdio)")

    a = ap.parse_args()

    if a.cmd == "new":
        imgs = [pathlib.Path(p) for p in a.img]
        tmp = None
        if not a.no_paste and not imgs:
            tmp = paste_clipboard()
            if tmp:
                imgs.append(tmp)
            else:
                print("· clipboard sem imagem — criando só com texto", file=sys.stderr)
        tid = create(a.title, a.body, a.kind, imgs)
        if tmp:
            tmp.unlink(missing_ok=True)
        print(f"#{tid}  {a.title}  [{a.kind}]" + (f"  +{len(imgs)} img" if imgs else ""))

    elif a.cmd == "ls":
        rows = listing(a.status, a.kind, 200)
        if not rows:
            print("(vazio)")
        for r in rows:
            img = f" 🖼{r['n_img']}" if r["n_img"] else ""
            who = f" ({r['claimed_by']})" if r["claimed_by"] else ""
            bot = "🤖 " if r["origin"] == "agent" else ""
            print(f"#{r['id']:<4} {r['status']:<8} {r['kind']:<7} {bot}{r['title']}{img}{who}")
        n_tri = sum(1 for r in rows if r["status"] == "triage")
        if n_tri and not a.status:
            print(f"\n⚡ {n_tri} em triagem — 'tk ok <id>' aprova, 'tk rm <id>' descarta")

    elif a.cmd == "show":
        t = get(a.id)
        if not t:
            sys.exit(f"ticket {a.id} não existe")
        print(f"#{t['id']}  {t['title']}\n{t['status']} · {t['kind']} · {t['created_at']}")
        if t["body"]:
            print(f"\n{t['body']}")
        for i in t["images"]:
            print(f"\n🖼 {i['orig']}")
        for c in t["comments"]:
            print(f"\n[{c['at']}] {c['author']}: {c['text']}")

    elif a.cmd == "mv":
        advance(a.id, a.status)
        print(f"#{a.id} → {a.status}")

    elif a.cmd == "ok":
        advance(a.id, "todo")
        print(f"#{a.id} → todo (aprovado, entrou na fila)")

    elif a.cmd == "rm":
        delete(a.id)
        print(f"#{a.id} apagado")

    elif a.cmd == "web":
        try:
            subprocess.Popen(["open", f"http://localhost:{a.port}"])
        except Exception:
            pass
        run_web(a.port)

    elif a.cmd == "mcp":
        run_mcp()

    else:
        ap.print_help()


if __name__ == "__main__":
    main()

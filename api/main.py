import asyncio
import json
import os
import sqlite3
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BITNET_BASE = os.getenv("BITNET_URL", "http://bitnet-server:8080")
DB_PATH = os.getenv("DB_PATH", "/data/bitnet.db")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

_TAGS = [
    {"name": "chat",    "description": "Envio de mensagens ao modelo BitNet."},
    {"name": "session", "description": "Gerenciamento de sessões de conversa persistidas em SQLite."},
    {"name": "sistema", "description": "Saúde, interface web e proxy OpenAI-compatible."},
]

app = FastAPI(
    title="BitNet Chat API",
    version="1.0.0",
    description="""
API para interagir com o **BitNet-b1.58** da Microsoft via `llama-server`.

## Funcionalidades
- Chat com histórico persistido em **SQLite**
- Streaming SSE token a token
- Interface web estilo ChatGPT em `http://localhost:3002`
- Proxy OpenAI-compatible em `/v1/*`

## Acesso rápido
| URL | Descrição |
|-----|-----------|
| `/` | Interface de chat web |
| `/docs` | Swagger UI |
| `/v1/chat/completions` | API OpenAI-compatible |
""",
    openapi_tags=_TAGS,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------------------
# SQLite — inicialização e helpers
# ---------------------------------------------------------------------------

def _db_init() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT 'Nova conversa',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
        """)


def _db_sessions_list() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _db_session_messages(sid: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id",
            (sid,),
        ).fetchall()
    return [dict(r) for r in rows]


def _db_session_create(sid: str, title: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, title) VALUES (?, ?)",
            (sid, title),
        )


def _db_message_add(sid: str, role: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (sid, role, content),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = datetime('now','localtime') WHERE id = ?",
            (sid,),
        )


def _db_session_rename(sid: str, title: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, sid))


def _db_session_delete(sid: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))


_db_init()

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., description="Mensagem do usuário.", examples=["Explique recursão em Python"])
    session_id: str | None = Field(default=None, description="ID da sessão. Omitir cria nova sessão.")


class ChatResponse(BaseModel):
    response: str = Field(description="Resposta do modelo.")
    session_id: str = Field(description="ID da sessão.")


class RenameRequest(BaseModel):
    title: str = Field(..., description="Novo título da conversa.", examples=["Minha conversa"])


class HealthResponse(BaseModel):
    api: str
    model: str

# ---------------------------------------------------------------------------
# Interface web (ChatGPT-like)
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BitNet</title>
<script src="https://cdn.jsdelivr.net/npm/marked@9/marked.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#212121;color:#ececec;height:100vh;display:flex;overflow:hidden}

/* ── Sidebar ── */
#sidebar{width:260px;background:#171717;display:flex;flex-direction:column;flex-shrink:0;border-right:1px solid rgba(255,255,255,.08);transition:width .2s}
#sidebar-header{padding:12px;display:flex;align-items:center;gap:8px;border-bottom:1px solid rgba(255,255,255,.08)}
#sidebar-title{font-weight:700;font-size:.95rem;color:#ececec;flex:1}
#new-chat-btn{width:100%;display:flex;align-items:center;gap:10px;padding:10px 12px;background:transparent;border:1px solid rgba(255,255,255,.15);border-radius:8px;color:#ececec;font-size:.875rem;cursor:pointer;transition:background .15s;margin-top:8px}
#new-chat-btn:hover{background:rgba(255,255,255,.06)}
#sessions-container{flex:1;overflow-y:auto;padding:8px 6px}
#sessions-container::-webkit-scrollbar{width:4px}
#sessions-container::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:2px}
.grp-label{font-size:.68rem;font-weight:600;color:#8e8ea0;text-transform:uppercase;letter-spacing:.05em;padding:6px 8px 3px}
.s-item{display:flex;align-items:center;padding:8px 10px;border-radius:8px;cursor:pointer;transition:background .1s;gap:8px;position:relative;user-select:none}
.s-item:hover{background:rgba(255,255,255,.05)}
.s-item.active{background:rgba(255,255,255,.1)}
.s-title{flex:1;font-size:.84rem;color:#ececec;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.s-title-input{flex:1;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.2);border-radius:4px;color:#ececec;font-size:.84rem;padding:1px 6px;outline:none;font-family:inherit}
.s-del{opacity:0;background:none;border:none;color:#8e8ea0;cursor:pointer;padding:2px 4px;border-radius:4px;flex-shrink:0;line-height:1;transition:opacity .15s,color .15s}
.s-item:hover .s-del{opacity:1}
.s-del:hover{color:#ef4444}

/* ── Main ── */
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#chat-area{flex:1;overflow-y:auto;padding:24px 0}
#chat-area::-webkit-scrollbar{width:6px}
#chat-area::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:3px}
#welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:14px;color:#8e8ea0}
#welcome h2{font-size:1.9rem;color:#ececec;font-weight:700}
#welcome p{font-size:.9rem}

/* ── Messages ── */
.msg-wrap{padding:6px 0}
.message{max-width:740px;margin:0 auto;padding:0 24px;display:flex;gap:14px;align-items:flex-start}
.message.user{flex-direction:row-reverse}
.msg-avatar{width:30px;height:30px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.7rem;font-weight:700;margin-top:3px}
.message.user .msg-avatar{background:#10a37f;color:#fff}
.message.assistant .msg-avatar{background:rgba(25,195,125,.12);border:1px solid rgba(25,195,125,.3);color:#19c37d}
.msg-body{flex:1;min-width:0}
.message.user .msg-body{background:#2f2f2f;border-radius:18px 18px 4px 18px;padding:10px 16px;font-size:.95rem;line-height:1.6;max-width:fit-content;margin-left:auto}
.message.assistant .msg-body{font-size:.95rem;line-height:1.75;color:#ececec;padding-top:3px}
.msg-body p{margin-bottom:10px}.msg-body p:last-child{margin-bottom:0}
.msg-body pre{background:#1a1a1a;border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:12px;overflow-x:auto;margin:10px 0;font-size:.85rem}
.msg-body code{font-family:'Consolas','Monaco',monospace;font-size:.875em;background:rgba(255,255,255,.08);padding:1px 5px;border-radius:4px}
.msg-body pre code{background:none;padding:0;font-size:.85rem}
.msg-body ul,.msg-body ol{padding-left:22px;margin-bottom:10px}.msg-body li{margin-bottom:4px}
.msg-body h1,.msg-body h2,.msg-body h3{margin:16px 0 8px;color:#ececec}
.msg-body blockquote{border-left:3px solid #10a37f;padding-left:12px;color:#8e8ea0;margin:8px 0}
.msg-body table{border-collapse:collapse;width:100%;margin:10px 0;font-size:.875rem}
.msg-body th,.msg-body td{border:1px solid rgba(255,255,255,.1);padding:6px 12px}
.msg-body th{background:rgba(255,255,255,.05)}
.msg-body a{color:#10a37f;text-decoration:none}.msg-body a:hover{text-decoration:underline}

/* Typing indicator */
.typing{display:flex;gap:5px;padding:6px 0;align-items:center}
.typing span{width:7px;height:7px;border-radius:50%;background:#8e8ea0;animation:blink 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}

/* ── Input ── */
#input-wrapper{padding:14px 24px 18px;background:#212121;border-top:1px solid rgba(255,255,255,.06)}
#input-box{max-width:740px;margin:0 auto;background:#2f2f2f;border:1px solid rgba(255,255,255,.1);border-radius:14px;display:flex;align-items:flex-end;padding:10px 12px;gap:8px;transition:border-color .15s}
#input-box:focus-within{border-color:rgba(255,255,255,.25)}
#input{flex:1;background:none;border:none;color:#ececec;font-size:.95rem;line-height:1.55;resize:none;outline:none;max-height:200px;font-family:inherit}
#input::placeholder{color:#8e8ea0}
#send-btn{width:34px;height:34px;border-radius:8px;background:#10a37f;border:none;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s,opacity .15s}
#send-btn:hover:not(:disabled){background:#0d8f6f}
#send-btn:disabled{opacity:.35;cursor:not-allowed}
#hint{text-align:center;font-size:.72rem;color:#8e8ea0;margin-top:8px;max-width:740px;margin-left:auto;margin-right:auto}

/* ── Mobile ── */
@media(max-width:640px){
  #sidebar{position:fixed;left:-260px;top:0;height:100%;z-index:100;transition:left .25s}
  #sidebar.open{left:0}
  #menu-btn{display:flex!important}
}
#menu-btn{display:none;position:fixed;top:12px;left:12px;z-index:101;background:rgba(0,0,0,.5);border:none;color:#ececec;cursor:pointer;padding:6px;border-radius:6px;align-items:center;justify-content:center}
</style>
</head>
<body>

<button id="menu-btn" onclick="toggleSidebar()">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
</button>

<div id="sidebar">
  <div id="sidebar-header">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#10a37f" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
    <span id="sidebar-title">BitNet</span>
  </div>
  <button id="new-chat-btn" onclick="newChat()">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14M5 12h14"/></svg>
    Nova conversa
  </button>
  <div id="sessions-container">
    <div id="sessions-list"></div>
  </div>
</div>

<div id="main">
  <div id="chat-area">
    <div id="welcome">
      <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="#10a37f" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      <h2>BitNet</h2>
      <p>LLM 1-bit da Microsoft rodando localmente. Como posso ajudar?</p>
    </div>
  </div>

  <div id="input-wrapper">
    <div id="input-box">
      <textarea id="input" placeholder="Mensagem para o BitNet…" rows="1"></textarea>
      <button id="send-btn" onclick="send()" disabled>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </div>
    <div id="hint">BitNet pode cometer erros. Verifique informações importantes. Shift+Enter para nova linha.</div>
  </div>
</div>

<script>
let currentSessionId = null;
let isLoading = false;

// Markdown
marked.use({ breaks: true, gfm: true });

// ── Input ────────────────────────────────────────────────────────────────
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
  sendBtn.disabled = !inputEl.value.trim() || isLoading;
});
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!sendBtn.disabled) send(); }
});

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
}

// ── Sessions ─────────────────────────────────────────────────────────────
async function loadSessions() {
  const res = await fetch('/sessions');
  renderSessions(await res.json());
}

function groupByDate(sessions) {
  const now = new Date();
  const today    = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today - 86400000);
  const lastWeek  = new Date(today - 7 * 86400000);
  const lastMonth = new Date(today - 30 * 86400000);
  const groups = [
    { label: 'Hoje',           items: [] },
    { label: 'Ontem',          items: [] },
    { label: 'Últimos 7 dias', items: [] },
    { label: 'Últimos 30 dias',items: [] },
    { label: 'Mais antigos',   items: [] },
  ];
  sessions.forEach(s => {
    const d = new Date(s.updated_at);
    if      (d >= today)     groups[0].items.push(s);
    else if (d >= yesterday) groups[1].items.push(s);
    else if (d >= lastWeek)  groups[2].items.push(s);
    else if (d >= lastMonth) groups[3].items.push(s);
    else                     groups[4].items.push(s);
  });
  return groups;
}

function renderSessions(sessions) {
  const list = document.getElementById('sessions-list');
  list.innerHTML = '';
  if (!sessions.length) {
    list.innerHTML = '<p style="color:#8e8ea0;font-size:.8rem;padding:16px 8px;text-align:center">Nenhuma conversa ainda</p>';
    return;
  }
  groupByDate(sessions).forEach(({ label, items }) => {
    if (!items.length) return;
    const grp = document.createElement('div');
    const lbl = document.createElement('div');
    lbl.className = 'grp-label';
    lbl.textContent = label;
    grp.appendChild(lbl);
    items.forEach(s => grp.appendChild(makeSessionItem(s)));
    list.appendChild(grp);
  });
}

function makeSessionItem(s) {
  const item = document.createElement('div');
  item.className = 'session-item s-item' + (s.id === currentSessionId ? ' active' : '');
  item.dataset.id = s.id;
  item.onclick = () => loadSession(s.id);

  const title = document.createElement('span');
  title.className = 's-title';
  title.textContent = s.title;
  title.title = 'Duplo clique para renomear';
  title.ondblclick = (e) => { e.stopPropagation(); startRename(item, s.id, title); };

  const del = document.createElement('button');
  del.className = 's-del';
  del.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>';
  del.title = 'Excluir conversa';
  del.onclick = (e) => { e.stopPropagation(); deleteSession(s.id); };

  item.appendChild(title);
  item.appendChild(del);
  return item;
}

function startRename(item, id, titleEl) {
  const input = document.createElement('input');
  input.className = 's-title-input';
  input.value = titleEl.textContent;
  item.replaceChild(input, titleEl);
  input.focus();
  input.select();

  const commit = async () => {
    const newTitle = input.value.trim() || titleEl.textContent;
    await fetch(`/session/${id}/title`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: newTitle }),
    });
    titleEl.textContent = newTitle;
    item.replaceChild(titleEl, input);
  };
  input.onblur = commit;
  input.onkeydown = e => { if (e.key === 'Enter') input.blur(); if (e.key === 'Escape') { item.replaceChild(titleEl, input); } };
}

async function loadSession(id) {
  currentSessionId = id;
  document.querySelectorAll('.s-item').forEach(el => el.classList.toggle('active', el.dataset.id === id));
  const area = document.getElementById('chat-area');
  area.innerHTML = '';
  const msgs = await (await fetch(`/session/${id}/messages`)).json();
  if (!msgs.length) { showWelcome(); return; }
  msgs.forEach(m => appendMessage(m.role, m.content));
  scrollBottom();
}

async function deleteSession(id) {
  if (!confirm('Excluir esta conversa?')) return;
  await fetch(`/session/${id}`, { method: 'DELETE' });
  if (currentSessionId === id) { currentSessionId = null; showWelcome(); }
  await loadSessions();
}

function newChat() {
  currentSessionId = null;
  document.querySelectorAll('.s-item').forEach(el => el.classList.remove('active'));
  showWelcome();
  inputEl.focus();
}

function showWelcome() {
  document.getElementById('chat-area').innerHTML = `
    <div id="welcome">
      <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="#10a37f" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      <h2>BitNet</h2>
      <p>LLM 1-bit da Microsoft rodando localmente. Como posso ajudar?</p>
    </div>`;
}

// ── Messages ─────────────────────────────────────────────────────────────
function appendMessage(role, content, streaming = false) {
  const w = document.getElementById('welcome');
  if (w) w.remove();

  const wrap = document.createElement('div');
  wrap.className = 'msg-wrap';
  const msg = document.createElement('div');
  msg.className = `message ${role}`;

  const av = document.createElement('div');
  av.className = 'msg-avatar';
  av.textContent = role === 'user' ? 'Eu' : 'AI';

  const body = document.createElement('div');
  body.className = 'msg-body';

  if (streaming) {
    body.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  } else if (role === 'assistant') {
    body.innerHTML = marked.parse(content);
    body.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
  } else {
    body.textContent = content;
  }

  msg.appendChild(av);
  msg.appendChild(body);
  wrap.appendChild(msg);
  document.getElementById('chat-area').appendChild(wrap);
  scrollBottom();
  return body;
}

function scrollBottom() {
  const a = document.getElementById('chat-area');
  a.scrollTop = a.scrollHeight;
}

// ── Send ─────────────────────────────────────────────────────────────────
async function send() {
  const text = inputEl.value.trim();
  if (!text || isLoading) return;

  inputEl.value = '';
  inputEl.style.height = 'auto';
  isLoading = true;
  sendBtn.disabled = true;

  appendMessage('user', text);
  const bubble = appendMessage('assistant', '', true);

  try {
    const res = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: currentSessionId }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '', full = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\\n');
      buf = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const ev = JSON.parse(line.slice(6));
          if (ev.type === 'start') {
            currentSessionId = ev.session_id;
            bubble.innerHTML = '';
          } else if (ev.type === 'delta') {
            full += ev.content;
            bubble.innerHTML = marked.parse(full);
            scrollBottom();
          } else if (ev.type === 'done') {
            bubble.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
            await loadSessions();
          }
        } catch (_) {}
      }
    }
    if (!full) bubble.innerHTML = '<em style="color:#8e8ea0">Sem resposta do modelo.</em>';
  } catch {
    bubble.innerHTML = '<em style="color:#ef4444">Erro ao conectar com o modelo. Verifique se o servidor está ativo.</em>';
  }

  isLoading = false;
  sendBtn.disabled = false;
  inputEl.focus();
}

// Init
loadSessions();
inputEl.focus();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Endpoints — Interface
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["sistema"], summary="Interface de chat web", include_in_schema=False)
async def index():
    return HTML

# ---------------------------------------------------------------------------
# Endpoints — Sessões
# ---------------------------------------------------------------------------

@app.get("/sessions", tags=["session"], summary="Listar todas as conversas")
async def list_sessions():
    """Retorna todas as sessões ordenadas pela mais recente."""
    return await asyncio.to_thread(_db_sessions_list)


@app.get("/session/{session_id}/messages", tags=["session"], summary="Mensagens de uma conversa")
async def get_messages(session_id: str):
    """Retorna todas as mensagens de uma sessão, em ordem cronológica."""
    return await asyncio.to_thread(_db_session_messages, session_id)


@app.patch("/session/{session_id}/title", tags=["session"], summary="Renomear conversa")
async def rename_session(session_id: str, body: RenameRequest):
    """Atualiza o título de uma conversa."""
    await asyncio.to_thread(_db_session_rename, session_id, body.title)
    return {"status": "ok", "session_id": session_id}


@app.delete("/session/{session_id}", tags=["session"], summary="Excluir conversa")
async def delete_session(session_id: str):
    """Remove permanentemente uma conversa e todas as suas mensagens."""
    await asyncio.to_thread(_db_session_delete, session_id)
    return {"status": "deleted", "session_id": session_id}

# ---------------------------------------------------------------------------
# Endpoints — Chat
# ---------------------------------------------------------------------------

@app.post("/chat", tags=["chat"], summary="Enviar mensagem (resposta completa)", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    Envia mensagem e aguarda a resposta **completa**.
    O histórico é salvo em SQLite e o `session_id` pode ser reutilizado.
    """
    sid = req.session_id or str(uuid.uuid4())
    title = req.message[:50].strip()

    await asyncio.to_thread(_db_session_create, sid, title)
    await asyncio.to_thread(_db_message_add, sid, "user", req.message)
    msgs = await asyncio.to_thread(_db_session_messages, sid)
    llm_msgs = [{"role": m["role"], "content": m["content"]} for m in msgs]

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{BITNET_BASE}/v1/chat/completions", json={"model": "bitnet", "messages": llm_msgs})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=r.text)

    content = r.json()["choices"][0]["message"]["content"]
    await asyncio.to_thread(_db_message_add, sid, "assistant", content)
    return {"response": content, "session_id": sid}


@app.post("/chat/stream", tags=["chat"], summary="Enviar mensagem (streaming SSE)", response_class=StreamingResponse)
async def chat_stream(req: ChatRequest):
    """
    Envia mensagem com resposta em **streaming SSE**.
    Eventos: `start` → `delta` (×N) → `done`.
    O histórico é persistido em SQLite ao final do stream.
    """
    sid = req.session_id or str(uuid.uuid4())
    title = req.message[:50].strip()

    await asyncio.to_thread(_db_session_create, sid, title)
    await asyncio.to_thread(_db_message_add, sid, "user", req.message)
    msgs = await asyncio.to_thread(_db_session_messages, sid)
    llm_msgs = [{"role": m["role"], "content": m["content"]} for m in msgs]

    async def generate():
        full = ""
        yield f"data: {json.dumps({'type': 'start', 'session_id': sid})}\n\n"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST", f"{BITNET_BASE}/v1/chat/completions",
                    json={"model": "bitnet", "messages": llm_msgs, "stream": True},
                ) as r:
                    async for line in r.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                            if delta:
                                full += delta
                                yield f"data: {json.dumps({'type': 'delta', 'content': delta})}\n\n"
                        except Exception:
                            pass
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            return

        await asyncio.to_thread(_db_message_add, sid, "assistant", full)
        yield f"data: {json.dumps({'type': 'done', 'session_id': sid})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

# ---------------------------------------------------------------------------
# Endpoints — Sistema
# ---------------------------------------------------------------------------

@app.get("/health", tags=["sistema"], summary="Status da API e do modelo", response_model=HealthResponse)
async def health():
    """Verifica se a API e o llama-server estão acessíveis."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{BITNET_BASE}/health")
            model_ok = r.status_code == 200
    except Exception:
        model_ok = False
    return {"api": "ok", "model": "ok" if model_ok else "unavailable"}


@app.get("/v1/chat/completions", include_in_schema=False)
async def chat_completions_browser():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


@app.api_route(
    "/v1/{path:path}",
    methods=["POST", "PUT", "DELETE", "OPTIONS"],
    tags=["sistema"],
    summary="Proxy OpenAI-compatible",
    description="Encaminha chamadas `/v1/*` ao llama-server interno. Compatível com qualquer cliente OpenAI.",
)
async def proxy_llama(path: str, request: Request):
    """
    Proxy transparente para o llama-server.

    ```python
    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:3002/v1", api_key="none")
    ```
    """
    body = await request.body()
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.request(
            method=request.method,
            url=f"{BITNET_BASE}/v1/{path}",
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            content=body,
            params=dict(request.query_params),
        )
    return Response(content=r.content, status_code=r.status_code,
                    headers=dict(r.headers), media_type=r.headers.get("content-type"))

# BitNet Docker

Container Docker para o [Microsoft BitNet](https://github.com/microsoft/BitNet) — framework de inferência para LLMs 1-bit — com todas as dependências pré-compiladas.

## Pré-requisitos

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac) ou Docker Engine (Linux)
- 4 GB de RAM livres (mínimo para o modelo 2B)
- ~8 GB de espaço em disco (imagem + modelo)

Para GPU NVIDIA: [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

---

## Início rápido

### 1. Build das imagens

```bash
docker compose --profile server build
```

> A primeira build leva **15–30 minutos** — baixa e compila Clang 18, LLVM e o BitNet com todos os submódulos.  
> Builds subsequentes são cacheadas e instantâneas.

### 2. Baixar o modelo

```bash
docker compose run --rm bitnet download-model
```

Baixa o **BitNet-b1.58-2B-4T** (~1.5 GB) para `./models/`. Para outro modelo:

```bash
docker compose run --rm bitnet download-model microsoft/BitNet-b1.58-2B-4T-gguf
```

### 3. Subir o servidor com interface de chat

```bash
docker compose --profile server up
```

| URL | Descrição |
|-----|-----------|
| `http://localhost:3002` | Interface de chat web |
| `http://localhost:3002/docs` | Swagger UI — teste os endpoints no navegador |
| `http://localhost:3002/redoc` | ReDoc — documentação de referência |
| `http://localhost:3002/v1/chat/completions` | API OpenAI-compatible (proxy para o llama-server) |

---

## Comandos disponíveis

| Comando | Descrição |
|---------|-----------|
| `download-model [repo]` | Baixa modelo do HuggingFace (padrão: BitNet-b1.58-2B-4T-gguf) |
| `list-models` | Lista modelos `.gguf` em `/models` |
| `infer -m <model> -p <prompt>` | Executa inferência direta |
| `server [-m <model>]` | Sobe o llama-server na porta 8080 |
| `benchmark -m <model>` | Benchmark de desempenho E2E |
| `bash` | Shell interativo dentro do container |
| `help` | Exibe ajuda |

### Parâmetros de inferência

```bash
docker compose run --rm bitnet infer \
  -m /models/BitNet-b1.58-2B-4T-gguf/ggml-model-i2_s.gguf \
  -p "Seu prompt aqui" \
  -n 200 \     # tokens a gerar (padrão: 128)
  -t 4 \       # threads de CPU
  -c 2048 \    # tamanho do contexto
  -temp 0.8 \  # temperatura (criatividade)
  -cnv         # modo conversa (chat template)
```

---

## API de Chat (porta 3002)

A `bitnet-api` é um wrapper FastAPI sobre o llama-server com histórico de sessão persistido em **SQLite** e streaming SSE. O banco de dados é gravado num volume Docker (`bitnet-data`) e sobrevive a reinicializações dos containers.

### Endpoints

| Endpoint | Método | Descrição |
|----------|--------|-----------|
| `GET /` | — | Interface de chat web (estilo ChatGPT, com histórico de conversas na sidebar) |
| `POST /chat` | JSON | Resposta completa (sem streaming) |
| `POST /chat/stream` | JSON | Streaming SSE token a token |
| `GET /sessions` | — | Lista todas as conversas salvas |
| `GET /session/{id}/messages` | — | Mensagens de uma conversa |
| `PATCH /session/{id}/title` | JSON | Renomeia uma conversa |
| `DELETE /session/{id}` | — | Exclui conversa e todo o histórico |
| `GET /health` | — | Status da API e do modelo |
| `POST /v1/chat/completions` | JSON | Proxy OpenAI-compatible para o llama-server |
| `GET /v1/chat/completions` | — | Redireciona para `/docs` (endpoint requer POST) |
| `GET/POST /v1/{path}` | — | Proxy transparente para qualquer rota do llama-server |

### Exemplos

**Linux/Mac:**
```bash
# Resposta completa
curl -s http://localhost:3002/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Explique o que é uma rede neural"}' | jq

# Reutilizando sessão (mantém contexto)
SESSION=$(curl -s http://localhost:3002/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Meu nome é João"}' | jq -r '.session_id')

curl -s http://localhost:3002/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"Qual é o meu nome?\",\"session_id\":\"$SESSION\"}" | jq
```

**Windows (PowerShell):**
```powershell
# Resposta completa
$body = @{ message = "Explique o que é uma rede neural" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:3002/chat" -Method Post -ContentType "application/json" -Body $body

# Reutilizando sessão (mantém contexto)
$r1 = @{ message = "Meu nome é João" } | ConvertTo-Json |
      ForEach-Object { Invoke-RestMethod -Uri "http://localhost:3002/chat" -Method Post -ContentType "application/json" -Body $_ }

$r2 = @{ message = "Qual é o meu nome?"; session_id = $r1.session_id } | ConvertTo-Json |
      ForEach-Object { Invoke-RestMethod -Uri "http://localhost:3002/chat" -Method Post -ContentType "application/json" -Body $_ }
$r2.response
```

### Formato do streaming SSE

`POST /chat/stream` retorna eventos `data: <JSON>\n\n`:

```
data: {"type":"start","session_id":"uuid"}
data: {"type":"delta","content":"Olá"}
data: {"type":"delta","content":", como posso ajudar?"}
data: {"type":"done","session_id":"uuid"}
```

| `type` | Descrição |
|--------|-----------|
| `start` | Início do stream — contém `session_id` |
| `delta` | Fragmento gerado — contém `content` |
| `done` | Fim do stream |
| `error` | Falha — contém `message` |

---

## API OpenAI-compatible (porta 3002)

O llama-server fica interno e todo acesso passa pelo proxy em `http://localhost:3002/v1`.

> **Atenção:** abrir `/v1/chat/completions` no navegador redireciona para `/docs`, pois o browser faz `GET` e esse endpoint só aceita `POST`.

**Linux/Mac:**
```bash
curl -s http://localhost:3002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"bitnet","messages":[{"role":"user","content":"Olá!"}]}' | jq
```

**Windows (PowerShell):**
```powershell
$body = @{
    model    = "bitnet"
    messages = @(@{ role = "user"; content = "Olá!" })
} | ConvertTo-Json -Compress

Invoke-RestMethod -Uri "http://localhost:3002/v1/chat/completions" `
  -Method Post -ContentType "application/json" -Body $body
```

**Python (cliente OpenAI):**
```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:3002/v1", api_key="none")
response = client.chat.completions.create(
    model="bitnet",
    messages=[{"role": "user", "content": "Olá!"}],
)
print(response.choices[0].message.content)
```

> `curl.exe` no PowerShell remove as aspas duplas do body. Use `Invoke-RestMethod` ou salve o body em arquivo: `curl.exe ... -d @body.json`.

---

## GPU (NVIDIA)

> Requer `nvidia-container-toolkit` instalado no host.

```bash
docker compose build bitnet-gpu
docker compose run --rm bitnet download-model
docker compose --profile gpu up bitnet-gpu
```

---

## Estrutura de arquivos

```
.
├── Dockerfile              # Imagem CPU (Ubuntu 22.04 + Clang 18)
├── Dockerfile.gpu          # Imagem GPU (CUDA 12.1)
├── docker-compose.yml      # Serviços: bitnet / bitnet-server / bitnet-api / bitnet-gpu
├── entrypoint.sh           # Roteamento de comandos no container
├── api/
│   ├── main.py             # FastAPI: chat, sessões SQLite, streaming SSE, Swagger
│   ├── requirements.txt
│   └── Dockerfile
├── models/                 # Modelos persistem aqui (volume montado no host)
└── README.md
```

> O banco SQLite (`bitnet.db`) é armazenado no volume Docker `bitnet-data` (gerenciado pelo Docker, não aparece no diretório do projeto) e sobrevive a reinicializações e rebuilds.

---

## Modelos suportados

| Modelo HuggingFace | Tamanho | Descrição |
|--------------------|---------|-----------|
| `microsoft/BitNet-b1.58-2B-4T-gguf` | ~1.5 GB | Modelo oficial Microsoft 2B (recomendado) |
| `microsoft/BitNet-b1.58-2B-4T` | ~4 GB | Versão HF (requer conversão) |
| `1bitLLM/bitnet_b1_58-large` | ~0.8 GB | Modelo menor |
| `1bitLLM/bitnet_b1_58-3B` | ~1.8 GB | Versão 3B |
| `HF1BitLLM/Llama3-8B-1.58-100B-tokens` | ~5 GB | Llama 3 8B 1-bit |

---

## Detalhes técnicos

| Componente | Detalhe |
|------------|---------|
| Base | Ubuntu 22.04 LTS |
| Compilador | Clang 18 (LLVM oficial) |
| Build | CMake + Make |
| Quantização | `i2_s` (x86 AVX2 + ARM) |
| GPU | CUDA 12.1 (Dockerfile.gpu) |
| Modelos | Volume persistente em `./models/` |
| Histórico de chat | SQLite via volume Docker `bitnet-data` |

### Por que Clang 18?

O BitNet usa kernels de lookup table (LUT) customizados que exigem extensões específicas do Clang. GCC não é suportado.

### Por que os modelos ficam fora da imagem?

Modelos BitNet ocupam 1–5 GB cada. Manter em volume no host permite reutilizar entre versões da imagem e não inflar o tamanho do container.

---

## Solução de problemas

**Build falha no passo cmake:**  
Use `docker compose build --no-cache bitnet` após mudanças no Dockerfile.

**`download-model` lento ou falha:**  
Configure um token do HuggingFace:
```bash
docker compose run --rm -e HUGGING_FACE_HUB_TOKEN=hf_seu_token bitnet download-model
```

**Porta já em uso (`port is already allocated`):**  
Pare todos os containers antes de subir:
```powershell
docker ps -q | ForEach-Object { docker stop $_ }
docker compose --profile server up --build
```

**Mudanças na API não aparecem após reiniciar:**  
Sempre use `--build` para reconstruir a imagem `bitnet-api` após alterações em `api/`:
```bash
docker compose --profile server up --build
```

**Inferência lenta:**  
Aumente threads com `-t $(nproc)` (usa todos os núcleos disponíveis).

**`container is not running` ao usar `docker exec`:**  
O container sai imediatamente sem TTY. Use `docker compose run` para comandos avulsos:
```bash
docker compose run --rm bitnet download-model
docker compose run --rm bitnet infer -m /models/... -p "seu prompt"
```
Para manter o container ativo:
```bash
docker compose run -d --name bitnet-running bitnet sleep infinity
docker exec -it bitnet-running bash
```

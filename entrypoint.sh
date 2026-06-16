#!/bin/bash
set -e

BITNET_DIR="/opt/BitNet"
MODELS_DIR="/models"

# ── Helper: listar modelos disponíveis ──────────────────────────────────────
list_models() {
    echo "Modelos disponíveis em ${MODELS_DIR}:"
    find "${MODELS_DIR}" -name "*.gguf" 2>/dev/null | head -20 || echo "  Nenhum modelo .gguf encontrado."
    echo ""
    echo "Para baixar um modelo, use:"
    echo "  docker exec <container> download-model"
    echo "  ou monte um diretório com modelos em /models"
}

# ── Helper: download do modelo padrão ───────────────────────────────────────
download_model() {
    local model="${1:-microsoft/BitNet-b1.58-2B-4T-gguf}"
    echo "Baixando modelo: ${model}"
    huggingface-cli download "${model}" --local-dir "${MODELS_DIR}/$(basename ${model})"
    echo "Modelo salvo em: ${MODELS_DIR}/$(basename ${model})"
}

# ── Atalhos de comando ───────────────────────────────────────────────────────
case "$1" in
    download-model)
        shift
        download_model "$@"
        ;;
    list-models)
        list_models
        ;;
    infer|run)
        shift
        exec python3 "${BITNET_DIR}/run_inference.py" "$@"
        ;;
    benchmark)
        shift
        exec python3 "${BITNET_DIR}/utils/e2e_benchmark.py" "$@"
        ;;
    server)
        shift
        # Detecta primeiro .gguf em /models se -m não for passado
        if [[ "$*" != *"-m"* ]]; then
            MODEL=$(find "${MODELS_DIR}" -name "*.gguf" | head -1)
            if [ -z "${MODEL}" ]; then
                echo "Erro: nenhum modelo encontrado em /models. Baixe um com 'download-model'."
                exit 1
            fi
            exec llama-server -m "${MODEL}" --host 0.0.0.0 --port 8080 "$@"
        else
            exec llama-server "$@"
        fi
        ;;
    help|--help|-h)
        echo ""
        echo "BitNet Docker — Comandos disponíveis:"
        echo ""
        echo "  download-model [repo]   Baixa modelo do HuggingFace (padrão: BitNet-b1.58-2B-4T)"
        echo "  list-models             Lista modelos .gguf em /models"
        echo "  infer  -m <model> -p <prompt> [opts]   Executa inferência"
        echo "  server [-m <model>]     Sobe servidor HTTP na porta 8080"
        echo "  benchmark -m <model>   Executa benchmark E2E"
        echo "  bash                    Shell interativo"
        echo ""
        echo "Exemplos:"
        echo "  docker run --rm -v ./models:/models bitnet download-model"
        echo "  docker run --rm -v ./models:/models bitnet infer -m /models/BitNet-b1.58-2B-4T/ggml-model-i2_s.gguf -p 'Olá, como vai?'"
        echo "  docker run --rm -v ./models:/models -p 8080:8080 bitnet server"
        ;;
    bash|sh|"")
        list_models
        exec bash
        ;;
    *)
        exec "$@"
        ;;
esac

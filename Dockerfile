# XGuide 校园个人 Agent — Docker 多阶段构建
# 目标：生产镜像尽量精简，开发镜像包含调试工具

# ── 阶段 1：基础环境 ──────────────────────────────────────────────────────────
# 基础镜像按 digest 钉死：同 tag 的远端内容随时间变化会破坏构建可复现性。
# 升级基础镜像：docker pull <image> 后用 image inspect 的 RepoDigest 更新此处。
FROM python:3.12-slim@sha256:507285ff17cb1d1756d3711c7543e82188f7a3752c79a9b3a4f9320640390300 AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# curl 用于健康检查；推理用 ONNX Runtime（无需 gcc/g++）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── 阶段 2：安装 Python 依赖 ──────────────────────────────────────────────────
FROM base AS dependencies

# 可选：pip 镜像源。CI（GitHub Actions）走默认官方源即可；国内本地构建可传
# --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple 加速，
# 否则容器内 pip 直连官方源可能远慢于宿主机（宿主机的 pip.ini 不会带入容器）。
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=$PIP_INDEX_URL

# 可选：模型下载镜像端点（如 https://hf-mirror.com），构建时 --build-arg HF_ENDPOINT=...
ARG HF_ENDPOINT=https://huggingface.co
ENV HF_ENDPOINT=$HF_ENDPOINT

# 预下载 ChromaDB 内置的 ONNX embedding 模型（~79MB），避免运行时下载超时
# （本地 bge 模型不可用时的回退向量空间）。
# 放在 COPY requirements/pip install 之前：它不依赖任何 Python 包，独立成层后
# 依赖清单变更不会连带触发这 79MB 的重新下载（S3 直连在国内网络下可能非常慢）。
RUN mkdir -p /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2 && \
    curl -L --retry 3 --retry-delay 5 -o /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx.tar.gz \
    https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz && \
    cd /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2 && \
    tar -xzf onnx.tar.gz && \
    rm onnx.tar.gz

# 全量传递锁：镜像构建可复现（事实源 requirements.txt，重生成命令见 requirements.lock 头部）
COPY requirements.lock .
RUN pip install --upgrade pip && \
    pip install -r requirements.lock

# 预下载本地向量模型（bge-small-zh-v1.5 Embedding ~95MB + bge-reranker-base ~570MB）
# 与运行期共用 mcp/embeddings.py 的下载逻辑，落在默认缓存目录（root 的 ~/.cache）
# 默认 strict：任一模型不可用即构建失败（保证产物模型齐备）。
# 受限网络（无 HF 访问）可传 --build-arg MODEL_PRELOAD=skip 跳过预装：
# 运行时自动降级 chroma 内置模型并 300s 冷却重试下载，网络恢复后可重构建恢复预装。
ARG MODEL_PRELOAD=strict
COPY mcp/embeddings.py /tmp/preload/embeddings.py
RUN if [ "$MODEL_PRELOAD" = "skip" ]; then \
        echo "[WARN] 已跳过模型预下载（MODEL_PRELOAD=skip），运行时将降级并自动重试"; \
        mkdir -p /root/.cache/echoguide_models; \
    else \
        python -c "import importlib.util as u; s = u.spec_from_file_location('emb', '/tmp/preload/embeddings.py'); m = u.module_from_spec(s); s.loader.exec_module(m); m.preload_models()"; \
    fi

# ── 阶段 3：生产镜像 ──────────────────────────────────────────────────────────
FROM base AS production

# 非 root 用户运行。先创建用户，后续 COPY 直接带 owner，避免 chown -R 复制出额外大层。
RUN useradd -m -u 1000 echoguide

# 从依赖阶段复制已安装的包
COPY --from=dependencies /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=dependencies /usr/local/bin /usr/local/bin
# 复制预下载的 ONNX 模型缓存（chroma 内置 + 本地 bge 向量模型）
COPY --from=dependencies --chown=echoguide:echoguide /root/.cache/chroma /home/echoguide/.cache/chroma
COPY --from=dependencies --chown=echoguide:echoguide /root/.cache/echoguide_models /home/echoguide/.cache/echoguide_models

# 复制应用代码
COPY --chown=echoguide:echoguide . .

# 创建必要目录，只调整运行期需要写入的目录权限，避免递归 chown 整个应用。
RUN mkdir -p /app/data/chroma /app/logs /app/config && \
    chown echoguide:echoguide /app/data /app/data/chroma /app/logs /app/config
USER echoguide

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── 阶段 4：开发镜像 ──────────────────────────────────────────────────────────
FROM dependencies AS development

COPY . .

RUN mkdir -p /app/data/chroma /app/logs /app/config /app/tests && \
    chmod -R 777 /app/data /app/logs

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

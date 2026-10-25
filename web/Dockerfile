FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    SDF_CONFIG_DIR=/config \
    SDF_DEFAULT_CONFIG=/opt/sdf-default-config

# 换清华 Debian 源，否则 apt 阶段能跑 40 分钟（实测 deb.debian.org ~50KB/s）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; s|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; s|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list; \
    fi

# ffmpeg 带 libx264 与 h264_nvenc（成片用 NVENC 快 3-5 倍）；fonts-noto-cjk 供烧字幕
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY web/requirements.txt .
RUN pip install -r requirements.txt

COPY pipeline /app/pipeline
COPY web/app.py /app/app.py
COPY web/static /app/static
# 配置模板（首次部署种子 + 升级用默认值；不覆盖运行时 /config）
# ⚠️ 必须用 config_defaults/ 而不是 config/：
#    rsync 对 config/ 是 --ignore-existing（保护线上改过的值），
#    所以远端 config/ 可能是旧版；模板必须走单独的目录才会被完整同步。
COPY config_defaults /opt/sdf-default-config
# 配置迁移/合并工具（升级用：docker exec sdf-api python /app/migrate_v04.py）
COPY scripts/migrate_v04.py /app/migrate_v04.py
COPY scripts/migrate_v05.py /app/migrate_v05.py
COPY scripts/migrate_v06.py /app/migrate_v06.py
COPY scripts/merge_config.py /app/merge_config.py
# 运维脚本：重渲对比 / Ref2VA 自测 / LLM 基准（容器重建不丢）
COPY scripts/rerender_compare.py /app/rerender_compare.py
COPY scripts/test_ref2va.py /app/test_ref2va.py
COPY scripts/llm_bench.py /app/llm_bench.py
COPY scripts/test_face_fidelity.py /app/test_face_fidelity.py
COPY scripts/test_firstframe_upload.py /app/test_firstframe_upload.py
COPY scripts/test_ref2va_keys.py /app/test_ref2va_keys.py

EXPOSE 8000
HEALTHCHECK NONE

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

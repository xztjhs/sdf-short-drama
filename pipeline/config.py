"""配置加载 —— 全部可线下修改，改完重启 api 即生效（无需重建镜像）。

设计原则：
  1. 所有可调项都在 config/ 下，代码里不硬编码
  2. 文件缺失/字段缺失时用内置默认值兜底，不炸
  3. 热重载：调用 reload() 即可，不必重启容器
  4. secrets 单独放 config/secrets.env（600），不进 git

目录约定（容器内 /config，宿主 /mnt/disks/nvme/docker/compose/sdf/config）：
  settings.yaml   运行参数（并发/超时/路径/日志）
  models.yaml     模型注册表（H3 / Wan2.2 及各变体）
  prompts.yaml    提示词模板（分镜/大纲/润色）
  routing.yaml    引擎路由规则
  engines.yaml    引擎渲染参数（步数/cfg/LoRA）
  secrets.env     API KEY 等（600）
"""
from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

CONFIG_DIR = Path(os.environ.get("SDF_CONFIG_DIR", "/config"))

# ---------------------------------------------------------------- 内置默认
DEFAULTS: dict[str, dict] = {
    "settings": {
        "app": {"name": "Short Drama Factory 短剧工厂", "version": "0.2.0"},
        "render": {
            "max_concurrent": 1,
            "shot_timeout_sec": 7200,
            "poll_interval_sec": 3,
            "free_vram_before_shot": True,
            "cooldown_between_shots_sec": 0,
            "max_retries": 3,
            "queue_autostart": True,
            "ref2va_enabled": True,
            "ref2va_image_size": "match",
            "chain_same_scene_only": True,
        },
        "paths": {
            "output_dir": "/output",
            "input_dir": "/input",
            "workflows_dir": "/workflows",
            "data_dir": "/data",
        },
        "storage": {
            "keep_shot_fragments": True,
            "cleanup_workdir_after_assemble": True,
        },
        "assemble": {
            "orientation": "portrait",
            "width": 720, "height": 1280, "fps": 24,
            "vcodec": "auto",
            "crf": 20, "preset": "medium", "threads": 16,
            "lufs": -16.0, "scale_mode": "pad",
            "burn_subtitles": True,
        },
        "ui": {"page_size": 20, "theme": "dark"},
        "logging": {"level": "INFO", "max_files": 5, "max_size_mb": 50},
    },
    "models": {"models": []},
    "prompts": {"prompts": {}},
    "routing": {"rules": [], "resolutions": {}, "cost": {}},
    "engines": {"engines": {}},
    "presets": {"styles": [], "platforms": [], "monitor": {}},
}

# 所有配置块（新增块只改这里 + DEFAULTS）
SECTIONS = ("settings", "models", "prompts", "routing", "engines", "presets")


class _Store:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._mtime: dict[str, float] = {}
        self.reload()

    # ---------------------------------------------------------- 读取
    def _load_yaml(self, name: str) -> dict:
        p = CONFIG_DIR / f"{name}.yaml"
        base = copy.deepcopy(DEFAULTS.get(name, {}))
        if not p.exists():
            return base
        if yaml is None:
            raise RuntimeError("缺少 pyyaml，无法加载 YAML 配置")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise RuntimeError(f"{p} 解析失败: {e}") from e
        if not isinstance(raw, dict):
            return base
        return _deep_merge(base, raw)

    def reload(self) -> None:
        """重新读所有配置文件。运行期调用即可生效。"""
        with self._lock:
            for name in SECTIONS:
                self._data[name] = self._load_yaml(name)
                p = CONFIG_DIR / f"{name}.yaml"
                self._mtime[name] = p.stat().st_mtime if p.exists() else 0.0

    def ensure_files(self) -> list[str]:
        """首次部署时把默认配置写成文件。

        优先从镜像内的 /opt/sdf-default-config 复制（带完整注释和实测值）；
        没有则用代码里的 DEFAULTS 兜底写入。
        """
        created = []
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        seed = Path(os.environ.get("SDF_DEFAULT_CONFIG", "/opt/sdf-default-config"))

        names = [f"{n}.yaml" for n in DEFAULTS] + ["secrets.env.example"]
        for fname in names:
            dst = CONFIG_DIR / fname
            if dst.exists():
                continue
            src = seed / fname
            if src.exists():
                dst.write_bytes(src.read_bytes())
                created.append(fname)

        # secrets.env：从模板生成（空值），保证文件存在且权限 600
        sec = CONFIG_DIR / "secrets.env"
        if not sec.exists():
            tpl = CONFIG_DIR / "secrets.env.example"
            header = "# 本文件含凭据，权限 600，不进 git\n"
            if tpl.exists():
                body = tpl.read_text(encoding="utf-8")
            else:
                body = "ICOMPIFY_API_KEY=
            sec.write_text(header + body, encoding="utf-8")
            try:
                sec.chmod(0o600)
            except OSError:
                pass
            created.append("secrets.env")

        # 兜底：仍有缺失的 yaml 用内置 DEFAULTS 写
        if yaml is not None:
            for name, default in DEFAULTS.items():
                p = CONFIG_DIR / f"{name}.yaml"
                if not p.exists():
                    p.write_text(
                        yaml.safe_dump(default, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
                    created.append(p.name)

        # 写完必须重载，否则内存快照还是启动时的空默认值
        if created:
            self.reload()
        return created

    # ---------------------------------------------------------- 访问
    def get(self, section: str, dotted: str = "", default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data.get(section, {})
            if not dotted:
                return node
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def section(self, name: str) -> dict:
        with self._lock:
            return self._data.get(name, {})

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def file_info(self) -> list[dict]:
        out = []
        for name in SECTIONS:
            p = CONFIG_DIR / f"{name}.yaml"
            out.append({
                "name": f"{name}.yaml",
                "path": str(p),
                "exists": p.exists(),
                "size": p.stat().st_size if p.exists() else 0,
                "mtime": p.stat().st_mtime if p.exists() else None,
            })
        sp = CONFIG_DIR / "secrets.env"
        out.append({
            "name": "secrets.env",
            "path": str(sp),
            "exists": sp.exists(),
            "size": sp.stat().st_size if sp.exists() else 0,
            "mtime": sp.stat().st_mtime if sp.exists() else None,
            "secret": True,
        })
        return out

    # ---------------------------------------------------------- 写
    def write_section(self, name: str, data: dict) -> str:
        """写入某个 section（管理界面用）。返回备份文件名。"""
        if name not in DEFAULTS:
            raise ValueError(f"未知配置块: {name}")
        if yaml is None:
            raise RuntimeError("缺少 pyyaml")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        p = CONFIG_DIR / f"{name}.yaml"
        backup = ""
        if p.exists():
            import time
            backup = f"{name}.yaml.bak.{time.strftime('%Y%m%d_%H%M%S')}"
            (CONFIG_DIR / backup).write_bytes(p.read_bytes())
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
        self.reload()
        return backup


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ---------------------------------------------------------------- secrets
def load_secrets() -> dict[str, str]:
    """读 config/secrets.env（KEY=VALUE 每行一个）。"""
    p = CONFIG_DIR / "secrets.env"
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_secret(key: str, value: str) -> None:
    """写入/更新一个 secret，保留其它行与注释。"""
    p = CONFIG_DIR / "secrets.env"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    found = False
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def mask_secret(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:4]}{'*' * (len(v) - 8)}{v[-4:]}"


cfg = _Store()

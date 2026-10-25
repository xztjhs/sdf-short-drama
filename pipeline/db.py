"""状态库 —— SQLite，单文件零依赖。

五段流程（对应 project.stage）：
  story      → 输入故事
  outline    → 剧情及大纲编制
  locked     → 锁定基础信息（角色/场景/道具/风格/禁用项）
  storyboard → 分镜头脚本
  rendering  → 渲染中
  done       → 成片完成

设计要点：
- 所有状态在 SQLite，进程重启可续跑
- 一个 shot = 一个渲染任务（可重试，保留历次尝试）
- characters 字段永远以 list 返回（内部存 JSON）
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any

DB_PATH = os.environ.get("SDF_DB_PATH", "/data/sdf.db")

STAGES = ("story", "outline", "locked", "storyboard", "rendering", "done")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS project (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    slug         TEXT,                 -- 英文名（自动生成，可手改），用于目录
    out_dir      TEXT,                 -- 实际产物目录名：YYYYMMDD-HHMM_slug
    synopsis     TEXT,
    style        TEXT,
    style_zh     TEXT,                 -- 中文风格描述（与 style 英文成对）
    aspect       TEXT DEFAULT '9:16',
    story        TEXT,                 -- 第1步：原始故事输入
    outline      TEXT,                 -- 第2步：剧集大纲 JSON
    locked_info  TEXT,                 -- 第3步：锁定基础信息 JSON
    stage        TEXT DEFAULT 'story', -- story|outline|locked|storyboard|rendering|done
    archived     INTEGER DEFAULT 0,    -- 1=已归档（列表默认隐藏，不删数据）
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

-- 全局角色库：跨项目复用同一角色的人设
CREATE TABLE IF NOT EXISTS character_lib (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,         -- 中文名
    name_en     TEXT,                  -- 英文名
    appearance  TEXT,                  -- 英文外貌锚点
    appearance_zh TEXT,                -- 中文描述
    ref_image   TEXT,                  -- 参考图路径
    voice_note  TEXT,
    tags        TEXT,                  -- 逗号分隔标签
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chrlib_name ON character_lib(name);

CREATE TABLE IF NOT EXISTS character (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    appearance  TEXT,
    ref_image   TEXT,
    voice_note  TEXT,
    locked      INTEGER DEFAULT 0,     -- 1=第3步锁定，不允许分镜改动
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS location (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    ref_image   TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS prop (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS episode (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    title       TEXT,
    hook        TEXT,
    conflict    TEXT,
    summary     TEXT,
    cliffhanger TEXT,
    script      TEXT,
    engine_mode TEXT,                  -- auto|h3|wan22（v0.4 该集渲染模式）
    scenes      TEXT,                  -- v0.6 场次卡 JSON：[{id,name,desc,light}]
    status      TEXT DEFAULT 'draft',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shot (
    id            TEXT PRIMARY KEY,
    episode_id    TEXT NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    duration      REAL NOT NULL DEFAULT 5.0,
    shot_size     TEXT,
    camera        TEXT,
    prompt        TEXT NOT NULL,
    negative      TEXT,
    dialogue      TEXT,
    sfx           TEXT,
    characters    TEXT,
    speaker       TEXT,                -- v0.6 台词说话人（解决 H3 猜错谁说话）
    scene_id      INTEGER,             -- v0.6 场次号（决定镜头衔接是否跨场）
    first_frame   TEXT,
    engine        TEXT,
    resolution    TEXT,
    steps         INTEGER,
    seed          INTEGER,
    status        TEXT DEFAULT 'pending',
    output_path   TEXT,
    error         TEXT,
    retries       INTEGER DEFAULT 0,
    duration_ms   INTEGER,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    status       TEXT DEFAULT 'running',
    episode_id   TEXT,
    shot_id      TEXT,
    started      REAL NOT NULL,
    finished     REAL,
    result       TEXT,
    error        TEXT,
    progress     TEXT                 -- JSON 数组，取最近 300 条
);

CREATE INDEX IF NOT EXISTS idx_task_started ON task(started DESC);
CREATE INDEX IF NOT EXISTS idx_task_ep ON task(episode_id);

CREATE TABLE IF NOT EXISTS job (
    id           TEXT PRIMARY KEY,
    shot_id      TEXT NOT NULL REFERENCES shot(id) ON DELETE CASCADE,
    prompt_id    TEXT,
    engine       TEXT,
    status       TEXT DEFAULT 'queued',
    started_at   REAL,
    finished_at  REAL,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_shot_ep   ON shot(episode_id, seq);
CREATE INDEX IF NOT EXISTS idx_shot_stat ON shot(status);
CREATE INDEX IF NOT EXISTS idx_job_shot  ON job(shot_id);
CREATE INDEX IF NOT EXISTS idx_ep_prj    ON episode(project_id, seq);

-- 成片产物登记（v0.4）：渲染成片各阶段的输出都在这里
-- kind: concat(拼接原件) / final(含字幕成片) / upscale(超分) / srt(字幕)
CREATE TABLE IF NOT EXISTS artifact (
    id           TEXT PRIMARY KEY,
    episode_id   TEXT NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    label        TEXT,
    path         TEXT NOT NULL,
    width        INTEGER,
    height       INTEGER,
    duration     REAL,
    size         INTEGER,
    target       TEXT,                 -- 超分档位 1080p/2k/4k
    status       TEXT DEFAULT 'done',
    meta         TEXT,                 -- JSON
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifact_ep ON artifact(episode_id, created_at DESC);

-- 跨集渲染队列（v0.4）：分镜做完就入队，后台单卡串行消费
CREATE TABLE IF NOT EXISTS render_queue (
    id           TEXT PRIMARY KEY,
    episode_id   TEXT NOT NULL,
    project_id   TEXT,
    seq          INTEGER,
    title        TEXT,
    engine_mode  TEXT,
    status       TEXT DEFAULT 'queued',   -- queued|running|done|failed|canceled
    priority     INTEGER DEFAULT 100,
    options      TEXT,                    -- JSON：quality_first / max_shots / upscale
    task_id      TEXT,                    -- 关联 task 表（前端轮询用它看日志）
    progress     TEXT,                    -- JSON：最近一次进度事件
    output       TEXT,                    -- 成片路径
    error        TEXT,
    started_at   REAL,
    finished_at  REAL,
    created_at   REAL NOT NULL,
    updated_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON render_queue(status, priority, created_at);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补新列（v0.1 → v0.2）。

    坑：v0.1 的 CREATE TABLE IF NOT EXISTS 不会改已存在的表，
    新增列必须逐列 ALTER，否则 INSERT 会报
    "table episode has no column named hook"。
    """
    def cols_of(table: str) -> set[str]:
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    # project
    cols = cols_of("project")
    for name, ddl in (
        ("story", "ALTER TABLE project ADD COLUMN story TEXT"),
        ("outline", "ALTER TABLE project ADD COLUMN outline TEXT"),
        ("locked_info", "ALTER TABLE project ADD COLUMN locked_info TEXT"),
        ("stage", "ALTER TABLE project ADD COLUMN stage TEXT DEFAULT 'story'"),
        ("archived", "ALTER TABLE project ADD COLUMN archived INTEGER DEFAULT 0"),
        ("slug", "ALTER TABLE project ADD COLUMN slug TEXT"),
        ("out_dir", "ALTER TABLE project ADD COLUMN out_dir TEXT"),
        ("style_zh", "ALTER TABLE project ADD COLUMN style_zh TEXT"),
    ):
        if name not in cols:
            conn.execute(ddl)

    # shot（v0.6 新增：说话人 / 场次）
    scols = cols_of("shot")
    for name, ddl in (
        ("speaker", "ALTER TABLE shot ADD COLUMN speaker TEXT"),
        ("scene_id", "ALTER TABLE shot ADD COLUMN scene_id INTEGER"),
    ):
        if name not in scols:
            conn.execute(ddl)

    # character
    ccols = cols_of("character")
    if "locked" not in ccols:
        conn.execute("ALTER TABLE character ADD COLUMN locked INTEGER DEFAULT 0")

    # episode（v0.2 新增四列）
    ecols = cols_of("episode")
    for name, ddl in (
        ("hook", "ALTER TABLE episode ADD COLUMN hook TEXT"),
        ("conflict", "ALTER TABLE episode ADD COLUMN conflict TEXT"),
        ("summary", "ALTER TABLE episode ADD COLUMN summary TEXT"),
        ("cliffhanger", "ALTER TABLE episode ADD COLUMN cliffhanger TEXT"),
        # v0.4：该集选定的渲染模式 auto|h3|wan22（分镜与渲染共用）
        ("engine_mode", "ALTER TABLE episode ADD COLUMN engine_mode TEXT"),
        # v0.6：场次卡（同一场景共用一段描述，避免每镜重建布景）
        ("scenes", "ALTER TABLE episode ADD COLUMN scenes TEXT"),
        # v0.6.4：手动组合成片方案（选镜 + take + 排序）
        ("compose", "ALTER TABLE episode ADD COLUMN compose TEXT"),
    ):
        if name not in ecols:
            conn.execute(ddl)


@contextmanager
def tx(path: str | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    s = uuid.uuid4().hex[:12]
    return f"{prefix}{s}" if prefix else s


def _j(v: Any) -> Any:
    """JSON 字段反序列化。"""
    if v in (None, ""):
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


# ---------------------------------------------------------------- projects

def _fallback_slug(title: str) -> str:
    """降级英文名：只保留 ASCII，保证路径安全。"""
    import re
    base = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "").strip()).strip("-").lower()
    return (base[:40] or "project")


def make_out_dir_name(slug: str, ts: float | None = None) -> str:
    """产物目录名：YYYYMMDD-HHMM_slug。"""
    import re
    import time as _t
    t = ts if ts is not None else now()
    stamp = _t.strftime("%Y%m%d-%H%M", _t.localtime(t))
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", (slug or "project")).strip("-").lower()
    return f"{stamp}_{safe or 'project'}"


def create_project(title: str, synopsis: str = "", style: str = "",
                   aspect: str = "9:16", story: str = "",
                   slug: str | None = None, style_zh: str = "") -> str:
    pid = new_id("prj_")
    t = now()
    slug_val = slug or _fallback_slug(title)
    with tx() as c:
        c.execute(
            "INSERT INTO project(id,title,slug,out_dir,synopsis,style,style_zh,"
            "aspect,story,stage,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, title, slug_val, make_out_dir_name(slug_val, t), synopsis,
             style, style_zh, aspect, story,
             "outline" if story else "story", t, t))
    return pid


def get_project_dir(pid: str) -> str | None:
    """产物目录名（旧数据没有 out_dir 时回退到 id）。"""
    with tx() as c:
        r = c.execute("SELECT out_dir FROM project WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        return r["out_dir"] or pid


def get_project(pid: str) -> dict | None:
    with tx() as c:
        r = c.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["outline"] = _j(d.get("outline"))
        d["locked_info"] = _j(d.get("locked_info"))
        return d


def list_projects(include_archived: bool = False) -> list[dict]:
    with tx() as c:
        where = "" if include_archived else "WHERE COALESCE(archived,0)=0"
        rows = c.execute(f"SELECT * FROM project {where} ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("outline", None)
            d.pop("locked_info", None)
            d["has_story"] = bool(d.pop("story", None))
            d["episode_count"] = c.execute(
                "SELECT COUNT(*) FROM episode WHERE project_id=?", (r["id"],)
            ).fetchone()[0]
            st = c.execute(
                "SELECT s.status, COUNT(*) n FROM shot s "
                "JOIN episode e ON s.episode_id=e.id "
                "WHERE e.project_id=? GROUP BY s.status", (r["id"],)
            ).fetchall()
            d["shot_stats"] = {x["status"]: x["n"] for x in st}
            d["shot_total"] = sum(d["shot_stats"].values())
            out.append(d)
        return out


def set_archived(pid: str, archived: bool) -> None:
    update_project(pid, archived=1 if archived else 0)


def delete_project(pid: str) -> None:
    """删项目（CASCADE 会清 episode/shot/character/location/prop/job）。"""
    with tx() as c:
        c.execute("DELETE FROM project WHERE id=?", (pid,))


def duplicate_project(pid: str, new_title: str | None = None) -> str | None:
    """复制项目（含角色/场景/道具/剧集/镜头），不带产物文件。

    用途：同一套人设/风格开新一季，不用重新锁定基础信息。
    """
    src = get_project(pid)
    if not src:
        return None
    nid = new_id("prj_")
    t = now()
    with tx() as c:
        c.execute(
            "INSERT INTO project(id,title,synopsis,style,aspect,story,outline,"
            "locked_info,stage,archived,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
            (nid, new_title or f"{src['title']} (副本)", src.get("synopsis"),
             src.get("style"), src.get("aspect"), src.get("story"),
             json.dumps(src["outline"], ensure_ascii=False) if src.get("outline") else None,
             json.dumps(src["locked_info"], ensure_ascii=False) if src.get("locked_info") else None,
             src.get("stage") or "story", t, t))

        # 角色/场景/道具
        for tbl, prefix in (("character", "chr_"), ("location", "loc_"), ("prop", "prp_")):
            rows = c.execute(f"SELECT * FROM {tbl} WHERE project_id=?", (pid,)).fetchall()
            for r in rows:
                d = dict(r)
                d.pop("id", None)
                d["project_id"] = nid
                if "created_at" in d:
                    d["created_at"] = t
                cols = list(d.keys())
                c.execute(
                    f"INSERT INTO {tbl}(id,{','.join(cols)}) "
                    f"VALUES({','.join('?' * (len(cols) + 1))})",
                    [new_id(prefix), *[d[k] for k in cols]])

        # 剧集 + 镜头
        eps = c.execute("SELECT * FROM episode WHERE project_id=? ORDER BY seq", (pid,)).fetchall()
        for e in eps:
            neid = new_id("ep_")
            ed = dict(e)
            ed.pop("id", None)
            ed["project_id"] = nid
            ed["created_at"] = t
            ed["updated_at"] = t
            cols = list(ed.keys())
            c.execute(
                f"INSERT INTO episode(id,{','.join(cols)}) "
                f"VALUES({','.join('?' * (len(cols) + 1))})",
                [neid, *[ed[k] for k in cols]])

            shots = c.execute("SELECT * FROM shot WHERE episode_id=? ORDER BY seq", (e["id"],)).fetchall()
            for s in shots:
                sd = dict(s)
                sd.pop("id", None)
                sd["episode_id"] = neid
                sd["status"] = "pending"      # 副本需重新渲染
                sd["output_path"] = None
                sd["error"] = None
                sd["duration_ms"] = None
                sd["retries"] = 0
                sd["created_at"] = t
                sd["updated_at"] = t
                cols = list(sd.keys())
                c.execute(
                    f"INSERT INTO shot(id,{','.join(cols)}) "
                    f"VALUES({','.join('?' * (len(cols) + 1))})",
                    [new_id("sh_"), *[sd[k] for k in cols]])
    return nid


def update_project(pid: str, **fields) -> None:
    if not fields:
        return
    for k in ("outline", "locked_info"):
        if k in fields and isinstance(fields[k], (dict, list)):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE project SET {cols}, updated_at=? WHERE id=?",
                  (*fields.values(), now(), pid))


def set_stage(pid: str, stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"未知阶段: {stage}，可用: {STAGES}")
    update_project(pid, stage=stage)


# -------------------------------------------------------------- characters

def add_character(project_id: str, name: str, appearance: str = "",
                  ref_image: str = "", voice_note: str = "",
                  locked: int = 0) -> str:
    cid = new_id("chr_")
    with tx() as c:
        c.execute(
            "INSERT INTO character(id,project_id,name,appearance,ref_image,"
            "voice_note,locked,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cid, project_id, name, appearance, ref_image, voice_note, locked, now()))
    return cid


def list_characters(project_id: str) -> list[dict]:
    with tx() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM character WHERE project_id=? ORDER BY created_at",
            (project_id,)).fetchall()]


def update_character(cid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE character SET {cols} WHERE id=?", (*fields.values(), cid))


def delete_character(cid: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM character WHERE id=?", (cid,))


def replace_characters(project_id: str, chars: list[dict], locked: int = 1) -> None:
    """第3步锁定：整体替换（按 name 去重）。"""
    with tx() as c:
        c.execute("DELETE FROM character WHERE project_id=?", (project_id,))
        for ch in chars:
            name = (ch.get("name") or "").strip()
            if not name:
                continue
            c.execute(
                "INSERT INTO character(id,project_id,name,appearance,ref_image,"
                "voice_note,locked,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (new_id("chr_"), project_id, name, ch.get("appearance", ""),
                 ch.get("ref_image", ""), ch.get("voice_note", ""), locked, now()))


# -------------------------------------------------------------- locations

def replace_locations(project_id: str, locs: list[dict]) -> None:
    with tx() as c:
        c.execute("DELETE FROM location WHERE project_id=?", (project_id,))
        for x in locs:
            if (x.get("name") or "").strip():
                c.execute(
                    "INSERT INTO location(id,project_id,name,description,ref_image,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (new_id("loc_"), project_id, x["name"],
                     x.get("description", ""), x.get("ref_image", ""), now()))


def list_locations(project_id: str) -> list[dict]:
    with tx() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM location WHERE project_id=? ORDER BY created_at",
            (project_id,)).fetchall()]


def replace_props(project_id: str, props: list[dict]) -> None:
    with tx() as c:
        c.execute("DELETE FROM prop WHERE project_id=?", (project_id,))
        for x in props:
            if (x.get("name") or "").strip():
                c.execute(
                    "INSERT INTO prop(id,project_id,name,description,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (new_id("prp_"), project_id, x["name"],
                     x.get("description", ""), now()))


def list_props(project_id: str) -> list[dict]:
    with tx() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM prop WHERE project_id=? ORDER BY created_at",
            (project_id,)).fetchall()]


# ----------------------------------------------------------------- episodes

def create_episode(project_id: str, seq: int, title: str = "", script: str = "",
                   hook: str = "", conflict: str = "", summary: str = "",
                   cliffhanger: str = "", engine_mode: str = "") -> str:
    eid = new_id("ep_")
    t = now()
    with tx() as c:
        c.execute(
            "INSERT INTO episode(id,project_id,seq,title,hook,conflict,summary,"
            "cliffhanger,script,engine_mode,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,?)",
            (eid, project_id, seq, title, hook, conflict, summary,
             cliffhanger, script, engine_mode or None, t, t))
    return eid


def list_episodes(project_id: str) -> list[dict]:
    with tx() as c:
        out = []
        for r in c.execute("SELECT * FROM episode WHERE project_id=? ORDER BY seq",
                           (project_id,)):
            d = dict(r)
            rows = c.execute(
                "SELECT status, COUNT(*) n FROM shot WHERE episode_id=? GROUP BY status",
                (r["id"],)).fetchall()
            d["shot_stats"] = {x["status"]: x["n"] for x in rows}
            d["shot_total"] = sum(d["shot_stats"].values())
            out.append(d)
        return out


def get_episode(eid: str) -> dict | None:
    with tx() as c:
        r = c.execute("SELECT * FROM episode WHERE id=?", (eid,)).fetchone()
        return dict(r) if r else None


def update_episode(eid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE episode SET {cols}, updated_at=? WHERE id=?",
                  (*fields.values(), now(), eid))


def set_episode_status(eid: str, status: str) -> None:
    update_episode(eid, status=status)


def set_episode_scenes(eid: str, scenes: list[dict]) -> None:
    """写入场次卡（v0.6）。

    scenes: [{"id": 1, "name": "加油站内部", "desc": "可视化的空间描述",
              "light": "固定光源描述", "notes": "连续性提醒"}]
    """
    update_episode(eid, scenes=json.dumps(scenes, ensure_ascii=False))


def get_episode_scenes(eid: str) -> list[dict]:
    ep = get_episode(eid) or {}
    raw = ep.get("scenes")
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def scene_card(eid: str, scene_id) -> dict:
    """按场次号取场景卡；找不到返回空 dict。"""
    try:
        sid = int(scene_id)
    except (TypeError, ValueError):
        return {}
    for sc in get_episode_scenes(eid):
        if int(sc.get("id") or 0) == sid:
            return sc
    return {}


def delete_episode(eid: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM episode WHERE id=?", (eid,))


def duplicate_episode(eid: str, seq: int | None = None) -> str | None:
    """复制一集（镜头一起带，产物不带）。"""
    src = get_episode(eid)
    if not src:
        return None
    with tx() as c:
        maxseq = c.execute("SELECT COALESCE(MAX(seq),0) FROM episode WHERE project_id=?",
                           (src["project_id"],)).fetchone()[0]
    nseq = seq if seq is not None else int(maxseq) + 1
    neid = new_id("ep_")
    t = now()
    with tx() as c:
        c.execute(
            "INSERT INTO episode(id,project_id,seq,title,hook,conflict,summary,"
            "cliffhanger,script,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'draft',?,?)",
            (neid, src["project_id"], nseq, f"{src.get('title') or '第' + str(nseq) + '集'} (副本)",
             src.get("hook"), src.get("conflict"), src.get("summary"),
             src.get("cliffhanger"), src.get("script"), t, t))
        shots = c.execute("SELECT * FROM shot WHERE episode_id=? ORDER BY seq", (eid,)).fetchall()
        for s in shots:
            sd = dict(s)
            sd.pop("id", None)
            sd["episode_id"] = neid
            sd["status"] = "pending"
            sd["output_path"] = None
            sd["error"] = None
            sd["duration_ms"] = None
            sd["retries"] = 0
            sd["created_at"] = t
            sd["updated_at"] = t
            cols = list(sd.keys())
            c.execute(
                f"INSERT INTO shot(id,{','.join(cols)}) "
                f"VALUES({','.join('?' * (len(cols) + 1))})",
                [new_id("sh_"), *[sd[k] for k in cols]])
    return neid


def reorder_episode(eid: str, seq: int) -> None:
    update_episode(eid, seq=seq)


def reorder_shot(sid: str, seq: int) -> None:
    update_shot(sid, seq=seq)


# ---------------------------------------------------------- 全局角色库
def lib_add(name: str, name_en: str = "", appearance: str = "",
            appearance_zh: str = "", ref_image: str = "",
            voice_note: str = "", tags: str = "") -> str:
    cid = new_id("clib_")
    t = now()
    with tx() as c:
        c.execute(
            "INSERT INTO character_lib(id,name,name_en,appearance,appearance_zh,"
            "ref_image,voice_note,tags,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cid, name, name_en, appearance, appearance_zh,
             ref_image, voice_note, tags, t, t))
    return cid


def lib_list(q: str = "") -> list[dict]:
    with tx() as c:
        if q:
            like = f"%{q}%"
            rows = c.execute(
                "SELECT * FROM character_lib WHERE name LIKE ? OR name_en LIKE ? "
                "OR tags LIKE ? ORDER BY updated_at DESC", (like, like, like)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM character_lib ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def lib_get(cid: str) -> dict | None:
    with tx() as c:
        r = c.execute("SELECT * FROM character_lib WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None


def lib_update(cid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE character_lib SET {cols}, updated_at=? WHERE id=?",
                  (*fields.values(), now(), cid))


def lib_delete(cid: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM character_lib WHERE id=?", (cid,))


def lib_import_to_project(pid: str, lib_ids: list[str]) -> int:
    """把全局角色导入项目（同名则跳过）。"""
    existing = {r["name"] for r in list_characters(pid)}
    n = 0
    for cid in lib_ids:
        lib = lib_get(cid)
        if not lib or lib["name"] in existing:
            continue
        add_character(pid, lib["name"], lib.get("appearance") or "",
                      lib.get("ref_image") or "", lib.get("voice_note") or "",
                      locked=0)
        existing.add(lib["name"])
        n += 1
    return n


def lib_from_project(pid: str, character_ids: list[str] | None = None) -> int:
    """把项目的角色存进全局库（同名不重复）。"""
    chars = list_characters(pid)
    if character_ids:
        chars = [c for c in chars if c["id"] in character_ids]
    exist = {r["name"] for r in lib_list()}
    n = 0
    for c in chars:
        if c["name"] in exist:
            continue
        lib_add(c["name"], appearance=c.get("appearance") or "",
                ref_image=c.get("ref_image") or "",
                voice_note=c.get("voice_note") or "")
        exist.add(c["name"])
        n += 1
    return n


# -------------------------------------------------------------------- shots

SHOT_FIELDS = ("duration", "shot_size", "camera", "prompt", "negative", "dialogue",
               "sfx", "characters", "speaker", "scene_id", "first_frame",
               "engine", "resolution", "steps", "seed", "status", "output_path",
               "error", "retries", "duration_ms")


def add_shot(episode_id: str, seq: int, prompt: str, **kw) -> str:
    sid = new_id("sh_")
    t = now()
    vals: dict[str, Any] = {k: kw.get(k) for k in SHOT_FIELDS}
    vals["prompt"] = prompt
    if vals.get("duration") is None:
        vals["duration"] = 5.0
    if vals.get("status") is None:
        vals["status"] = "pending"
    # resolution 故意不设默认：让 router 按引擎/质量档决定
    if isinstance(vals.get("characters"), (list, tuple)):
        vals["characters"] = json.dumps(list(vals["characters"]), ensure_ascii=False)
    cols = ["id", "episode_id", "seq", "created_at", "updated_at", *SHOT_FIELDS]
    data = [sid, episode_id, seq, t, t, *[vals[k] for k in SHOT_FIELDS]]
    with tx() as c:
        c.execute(f"INSERT INTO shot({','.join(cols)}) "
                  f"VALUES({','.join('?' * len(cols))})", data)
    return sid


def _row_to_shot(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    if d.get("characters"):
        try:
            d["characters"] = json.loads(d["characters"])
        except Exception:
            d["characters"] = []
    else:
        d["characters"] = []
    return d


def list_shots(episode_id: str) -> list[dict]:
    with tx() as c:
        rows = c.execute("SELECT * FROM shot WHERE episode_id=? ORDER BY seq",
                         (episode_id,)).fetchall()
        return [_row_to_shot(r) for r in rows]


def get_shot(sid: str) -> dict | None:
    with tx() as c:
        return _row_to_shot(
            c.execute("SELECT * FROM shot WHERE id=?", (sid,)).fetchone())


def update_shot(sid: str, **fields) -> None:
    if not fields:
        return
    if isinstance(fields.get("characters"), (list, tuple)):
        fields["characters"] = json.dumps(list(fields["characters"]),
                                          ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE shot SET {cols}, updated_at=? WHERE id=?",
                  (*fields.values(), now(), sid))


def delete_shot(sid: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM shot WHERE id=?", (sid,))


def delete_unrendered_shots(episode_id: str) -> int:
    """删掉该集所有还没渲染成功的镜头（pending/skipped/failed/running）。

    分镜重跑前必须调。原来只把 pending 标成 skipped，
    结果旧镜头留在库里，重跑一次就翻一倍（实测 E02 变 78 镜／应为 40）。
    done 的镜头保留：已经花了 GPU 时间，而且可能是用户手动重渲过的。
    """
    with tx() as c:
        cur = c.execute(
            "DELETE FROM shot WHERE episode_id=? AND status!='done'",
            (episode_id,))
        return cur.rowcount


def reset_running_shots(episode_id: str | None = None) -> int:
    """把卡在 running 的镜头改回 pending。

    API 重启时组合使用：worker 被 kill 后镜头会永远停在 running，
    前端看着像在渲，其实没人在跑，队列也不会重试（只捞 pending/failed）。
    """
    with tx() as c:
        if episode_id:
            cur = c.execute(
                "UPDATE shot SET status='pending', updated_at=? "
                "WHERE status='running' AND episode_id=?", (now(), episode_id))
        else:
            cur = c.execute(
                "UPDATE shot SET status='pending', updated_at=? "
                "WHERE status='running'", (now(),))
        return cur.rowcount


def add_shot_manual(episode_id: str, **kw) -> str:
    """手工新增一个空镜头（自动排到末尾）。"""
    with tx() as c:
        mx = c.execute(
            "SELECT COALESCE(MAX(seq),0) FROM shot WHERE episode_id=?", (episode_id,)
        ).fetchone()[0]
    return add_shot(episode_id, int(mx) + 1, kw.pop("prompt", "") or "",
                    **kw)


def delete_shot_and_compact(sid: str) -> bool:
    """删镜头并把后面的 seq 往前挤，避免出现 1,2,4,5 这种断号。"""
    shot = get_shot(sid)
    if not shot:
        return False
    eid, seq = shot["episode_id"], shot["seq"]
    with tx() as c:
        c.execute("DELETE FROM shot WHERE id=?", (sid,))
        c.execute("UPDATE shot SET seq=seq-1 WHERE episode_id=? AND seq>?",
                  (eid, seq))
    return True


def reset_shot_for_rerender(sid: str) -> None:
    update_shot(sid, status="pending", error=None, retries=0)


def next_pending_shot() -> dict | None:
    with tx() as c:
        r = c.execute(
            """SELECT s.* FROM shot s JOIN episode e ON s.episode_id=e.id
               WHERE s.status='pending' ORDER BY e.seq, s.seq LIMIT 1"""
        ).fetchone()
        return _row_to_shot(r)


# --------------------------------------------------------------------- jobs

def create_job(shot_id: str, engine: str) -> str:
    jid = new_id("job_")
    with tx() as c:
        c.execute("INSERT INTO job(id,shot_id,engine,status) VALUES(?,?,?,'queued')",
                  (jid, shot_id, engine))
    return jid


def update_job(jid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE job SET {cols} WHERE id=?", (*fields.values(), jid))


def list_jobs(limit: int = 50) -> list[dict]:
    with tx() as c:
        return [dict(r) for r in c.execute(
            "SELECT j.*, s.seq AS shot_seq, s.prompt AS shot_prompt "
            "FROM job j LEFT JOIN shot s ON j.shot_id=s.id "
            "ORDER BY j.started_at DESC LIMIT ?", (limit,)).fetchall()]


# -------------------------------------------------------------------- tasks
# 任务（渲染/成片）落库，API 重启不丢。
# 之前的实现只放内存 dict，重建容器后前端就看不到正在跑的任务，
# 但 ComfyUI 仍在算 —— 会出现「有任务在跑但界面空白」的诡异状态。

def create_task(tid: str, kind: str, episode_id: str | None = None,
                shot_id: str | None = None) -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO task(id,kind,status,episode_id,shot_id,"
                  "started,progress) VALUES(?,?,'running',?,?,?,'[]')",
                  (tid, kind, episode_id, shot_id, now()))


def append_task_progress(tid: str, ev: dict) -> None:
    import json as _j
    with tx() as c:
        r = c.execute("SELECT progress FROM task WHERE id=?", (tid,)).fetchone()
        if not r:
            return
        try:
            arr = _j.loads(r["progress"] or "[]")
        except Exception:
            arr = []
        arr.append(ev)
        c.execute("UPDATE task SET progress=? WHERE id=?",
                  (_j.dumps(arr[-300:], ensure_ascii=False), tid))


def finish_task(tid: str, status: str, result: Any = None,
                error: str | None = None) -> None:
    import json as _j
    with tx() as c:
        c.execute("UPDATE task SET status=?, finished=?, result=?, error=? "
                  "WHERE id=?",
                  (status, now(),
                   _j.dumps(result, ensure_ascii=False) if result is not None else None,
                   error, tid))


def get_task(tid: str) -> dict | None:
    import json as _j
    with tx() as c:
        r = c.execute("SELECT * FROM task WHERE id=?", (tid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("progress", "result"):
            if d.get(k):
                try:
                    d[k] = _j.loads(d[k])
                except Exception:
                    d[k] = [] if k == "progress" else None
        d.setdefault("progress", [])
        return d


def list_tasks(limit: int = 30) -> list[dict]:
    import json as _j
    with tx() as c:
        rows = c.execute("SELECT * FROM task ORDER BY started DESC LIMIT ?",
                         (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("progress", "result"):
                if d.get(k):
                    try:
                        d[k] = _j.loads(d[k])
                    except Exception:
                        d[k] = [] if k == "progress" else None
            d.setdefault("progress", [])
            out.append(d)
        return out


def mark_stale_tasks(timeout_sec: int = 7200) -> int:
    """把卡住的 running 任务标为 failed（API 重启后的收尾）。"""
    cutoff = now() - timeout_sec
    with tx() as c:
        cur = c.execute(
            "UPDATE task SET status='failed', finished=?, "
            "error='任务中断（服务重启或超时）' "
            "WHERE status='running' AND started < ?", (now(), cutoff))
        return cur.rowcount


# -------------------------------------------------------------------- stats

def stats(project_id: str | None = None) -> dict:
    with tx() as c:
        if project_id:
            where = "JOIN episode e ON s.episode_id=e.id WHERE e.project_id=?"
            args = (project_id,)
        else:
            where, args = "", ()
        rows = c.execute(
            f"SELECT s.status, COUNT(*) n, SUM(s.duration) d FROM shot s {where} "
            f"GROUP BY s.status", args).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total_dur = sum((r["d"] or 0) for r in rows)
        return {"by_status": by_status, "total": sum(by_status.values()),
                "total_duration_s": round(total_dur, 1)}


# ----------------------------------------------------------------- artifacts
# 成片链路产物（v0.4）：拼接原件 / 含字幕成片 / 超分版本 / SRT
# 为什么要落库：以前成片只回一个字符串路径，界面无法列出历史版本、
# 也无法区分「720p 原件」和「1080p 超分」，更没法下载对比。

ARTIFACT_KINDS = ("concat", "final", "upscale", "srt")


def add_artifact(episode_id: str, kind: str, path: str, *,
                 label: str = "", width: int | None = None,
                 height: int | None = None, duration: float | None = None,
                 size: int | None = None, target: str = "",
                 meta: dict | None = None) -> str:
    aid = new_id("art_")
    with tx() as c:
        c.execute(
            "INSERT INTO artifact(id,episode_id,kind,label,path,width,height,"
            "duration,size,target,status,meta,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'done',?,?)",
            (aid, episode_id, kind, label, path, width, height, duration,
             size, target or None,
             json.dumps(meta, ensure_ascii=False) if meta else None, now()))
    return aid


def list_artifacts(episode_id: str, kind: str = "") -> list[dict]:
    with tx() as c:
        if kind:
            rows = c.execute(
                "SELECT * FROM artifact WHERE episode_id=? AND kind=? "
                "ORDER BY created_at DESC", (episode_id, kind)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM artifact WHERE episode_id=? "
                "ORDER BY created_at DESC", (episode_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("meta"):
                try:
                    d["meta"] = json.loads(d["meta"])
                except Exception:
                    d["meta"] = None
            out.append(d)
        return out


def latest_artifact(episode_id: str, kind: str = "final") -> dict | None:
    rows = list_artifacts(episode_id, kind)
    return rows[0] if rows else None


def delete_artifact(aid: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM artifact WHERE id=?", (aid,))


def prune_artifacts(episode_id: str, kind: str, keep: int = 3) -> int:
    """同类产物只保留最近 keep 个（磁盘上不删，只清登记）。"""
    rows = list_artifacts(episode_id, kind)
    n = 0
    for r in rows[keep:]:
        delete_artifact(r["id"])
        n += 1
    return n


# -------------------------------------------------------------------- queue
# 跨集渲染队列（v0.4）：
#   一集分镜做完 → 入队 → 后台单卡串行消费，人可以继续做下一集的分镜。
#   队列状态落库，API 重启后能恢复；暂停 = 消费器跳过，不丢任务。

QUEUE_STATUSES = ("queued", "running", "done", "failed", "canceled", "paused")


def queue_add(episode_id: str, *, project_id: str = "", seq: int = 0,
              title: str = "", engine_mode: str = "auto",
              options: dict | None = None, priority: int = 100) -> str:
    qid = new_id("q_")
    with tx() as c:
        c.execute(
            "INSERT INTO render_queue(id,episode_id,project_id,seq,title,"
            "engine_mode,status,priority,options,created_at) "
            "VALUES(?,?,?,?,?,?, 'queued', ?,?,?)",
            (qid, episode_id, project_id, seq, title, engine_mode,
             priority, json.dumps(options or {}, ensure_ascii=False), now()))
    return qid


def queue_list(status: str = "", limit: int = 100) -> list[dict]:
    with tx() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM render_queue WHERE status=? "
                "ORDER BY priority, created_at LIMIT ?", (status, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM render_queue ORDER BY "
                "CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                "priority, created_at LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("options"):
                try:
                    d["options"] = json.loads(d["options"])
                except Exception:
                    d["options"] = {}
            out.append(d)
        return out


def queue_get(qid: str) -> dict | None:
    with tx() as c:
        r = c.execute("SELECT * FROM render_queue WHERE id=?", (qid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        if d.get("options"):
            try:
                d["options"] = json.loads(d["options"])
            except Exception:
                d["options"] = {}
        return d


def queue_next() -> dict | None:
    with tx() as c:
        r = c.execute(
            "SELECT * FROM render_queue WHERE status='queued' "
            "ORDER BY priority, created_at LIMIT 1").fetchone()
        if not r:
            return None
        d = dict(r)
        if d.get("options"):
            try:
                d["options"] = json.loads(d["options"])
            except Exception:
                d["options"] = {}
        return d


def queue_update(qid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE render_queue SET {cols}, updated_at=? WHERE id=?",
                  (*fields.values(), now(), qid))


def queue_clear_finished() -> int:
    with tx() as c:
        cur = c.execute("DELETE FROM render_queue WHERE status IN "
                        "('done','canceled')")
        return cur.rowcount


def list_artifacts_by_id(aid: str) -> list[dict]:
    with tx() as c:
        rows = c.execute("SELECT * FROM artifact WHERE id=?", (aid,)).fetchall()
        return [dict(r) for r in rows]


def artifacts_summary(project_id: str | None = None) -> dict:
    """产物统计（后台总览用）：final / upscale 数量与总大小。"""
    with tx() as c:
        if project_id:
            rows = c.execute(
                "SELECT a.kind, COUNT(*) n, SUM(a.size) sz FROM artifact a "
                "JOIN episode e ON a.episode_id=e.id WHERE e.project_id=? "
                "GROUP BY a.kind", (project_id,)).fetchall()
        else:
            rows = c.execute(
                "SELECT kind, COUNT(*) n, SUM(size) sz FROM artifact "
                "GROUP BY kind").fetchall()
        return {r["kind"]: {"count": r["n"], "bytes": r["sz"] or 0} for r in rows}


# ---------------------------------------------------------------- takes
# 多轮渲染产物并存：同一镜头重渲 N 次，产物为 sh_{id}_h3_v1.mp4 / _v2.mp4 / _v3.mp4 …
# 组合成片时，允许为每镜挑选最满意的 take（v1/v2/v3...），并可调序。
#
# 当前 output_path 始终指向「最新一次成功渲染」的产物。
# 但重渲后旧产物仍在磁盘（sh_xxx_h3_v1.mp4、_v2.mp4 …），
# 用户手动改选时，直接把 output_path 改指到所选 take 即可，无需改表结构。

def list_takes(episode_id: str) -> list[dict]:
    """列出该集每一镜的所有可用 take（多轮渲染产物）。

    每个 take 含：path / take 序号(v1,v2,...) / duration / size / 渲染完成时间。
    返回结构：
    [
      {"seq": 1, "shot_id": "sh_xxx", "status": "done", "dialogue": "...",
       "selected_take": "v1",           # 当前 output_path 对应的 take
       "takes": [ {...}, ... ]},        # 该镜全部 take，按 take 序号升序
      ...
    ]
    """
    import glob as _glob
    from pipeline import assemble as _asm

    shots = list_shots(episode_id)
    if not shots:
        return []

    # 产物目录与 shot 在同一目录：从任意一个 shot 的 output_path 推出根目录
    root = None
    for s in shots:
        p = s.get("output_path")
        if p:
            root = os.path.dirname(p)
            break
    # 兜底：没有 output_path 时，用 episode 表数据推不出目录，返回空 takes
    # （每镜 takes 为空列表，前端显示「无可用 take」）
    out = []
    for s in shots:
        sid = s["id"]
        takes = []
        if root:
            # {sid}_*_{engine}_v{n}.mp4 —— 匹配多轮渲染产物（含跨引擎重渲）
            pat = os.path.join(root, f"{sid}_*_v*.mp4")
            for fp in sorted(_glob.glob(pat)):
                try:
                    info = _asm.info(fp)
                except Exception:
                    continue
                fname = os.path.basename(fp)
                # take 序号：文件名尾部 _v{n}
                import re as _re
                m = _re.search(r"_v(\d+)\.mp4$", fname)
                if not m:
                    continue
                take_n = int(m.group(1))
                takes.append({
                    "path": fp,
                    "take": f"v{take_n}",
                    "take_n": take_n,
                    "engine_tag": fname.split("_")[2] if len(fname.split("_")) > 2 else "",
                    "duration": round(info.get("duration") or 0, 2),
                    "size": info.get("size") or os.path.getsize(fp),
                    "mtime": os.path.getmtime(fp),
                })
        takes.sort(key=lambda x: x["take_n"])
        cur = s.get("output_path") or ""
        # 当前选中的 take 序号（output_path 指向的那个）
        selected_take = ""
        for tk in takes:
            if os.path.basename(cur) == os.path.basename(tk["path"]):
                selected_take = tk["take"]
                break
        out.append({
            "seq": s["seq"],
            "shot_id": sid,
            "status": s["status"],
            "prompt": (s.get("prompt") or "")[:60],
            "dialogue": s.get("dialogue") or "",
            "duration": s.get("duration"),
            "selected_take": selected_take,
            "output_path": cur,
            "takes": takes,
        })
    return out


def select_take(shot_id: str, take_path: str) -> bool:
    """把某镜的 output_path 改指到指定的 take 产物。

    前提：take_path 必须是该镜头自己的产物（sh_{shot_id}_ 开头），
    防止把 A 镜的产物指给 B 镜。
    """
    import re as _re
    p = os.path.basename(take_path)
    # 产物命名：{sid}_{engine}_v{n}.mp4（sid 本身带 sh_ 前缀）
    if not p.startswith(f"{shot_id}_"):
        return False
    if not _re.search(r"_v\d+\.mp4$", p):
        return False
    if not os.path.exists(take_path):
        return False
    update_shot(shot_id, output_path=take_path)
    return True


def reorder_shots(episode_id: str, new_seq_map: dict) -> bool:
    """按前端给定的映射重排镜头顺序。

    new_seq_map: {shot_id: new_seq}
    """
    with tx() as c:
        for sid, ns in new_seq_map.items():
            c.execute("UPDATE shot SET seq=?, updated_at=? WHERE id=?",
                      (int(ns), now(), sid))
    return True

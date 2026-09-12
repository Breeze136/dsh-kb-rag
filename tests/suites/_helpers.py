"""suite 公共助手（下划线开头 → 不被 run.py 当作用例收集）。"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "kb_engine.py"


def load_engine(name="kbe_under_test"):
    """按文件路径加载引擎模块（不走 sys.path，避免与已安装包混淆）。"""
    spec = importlib.util.spec_from_file_location(name, ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Engine:
    """走引擎的 serve 协议（每行一个 JSON 请求/响应），与插件真实用法一致。"""

    def __init__(self, env_extra=None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        if env_extra:
            env.update(env_extra)
        self.p = subprocess.Popen([sys.executable, str(ENGINE), "serve"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)

    def call(self, command, payload=None):
        self.p.stdin.write((json.dumps({"id": 1, "command": command,
                                        "payload": payload or {}}) + "\n").encode("utf-8"))
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("引擎进程没有响应（可能已退出）")
        out = json.loads(line.decode("utf-8", "replace"))
        if not out.get("ok"):
            raise RuntimeError("%s: %s" % (command, out.get("error")))
        return out["response"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:                                         # noqa: BLE001
            self.p.kill()


SAMPLE_DOCS = {
    "graphene.txt": (
        "Graphene growth by chemical vapour deposition on copper foils\n\n"
        "Abstract\n\nMonolayer graphene domains nucleate on copper and coalesce into a "
        "continuous film; Raman mapping confirms the layer number across the wafer.\n\n"
        "Methods\n\nCopper foils were annealed at 1050 C in hydrogen before growth, and "
        "methane was introduced as the carbon source for 30 minutes.\n\n"
        "Results\n\nDomain coalescence reduces the sheet resistance of the transferred film.\n"),
    "walls.txt": (
        "Conductive domain walls in ferroelectric thin films\n\n"
        "Abstract\n\nConductive domain walls in bismuth ferrite are probed by conductive atomic "
        "force microscopy at room temperature in ambient conditions.\n\n"
        "Methods\n\nSingle crystals were cleaved and contacted with gold-coated probes before "
        "the current maps were acquired at several bias voltages.\n"),
}


def write_docs(dirpath: Path, docs=None):
    """写几篇合成的样例文档（题材统一用石墨烯/铁电，符合仓库的示例约定）。"""
    dirpath.mkdir(parents=True, exist_ok=True)
    out = []
    for name, text in (docs or SAMPLE_DOCS).items():
        p = dirpath / name
        p.write_text(text, encoding="utf-8")
        out.append(p)
    return out


def q(db_file: Path, sql, args=()):
    """只读查询（测试里读库一律走只读 URI）。"""
    db = sqlite3.connect("file:%s?mode=ro" % db_file.as_posix(), uri=True)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()

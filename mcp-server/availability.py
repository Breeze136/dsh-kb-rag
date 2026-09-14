#!/usr/bin/env python3
"""MCP 侧可用性探测与功能隔离（stdlib-only，不依赖 mcp SDK）。

背景：kb-rag 的两条交付面能力集并不相同。DSH 插件半边拥有会话级状态、宿主提示注入、
`/kb` 命令与 GUI 卡片，这些在 **MCP 协议内结构性不存在** —— 不是"还没做"，而是没有承载面。
把它们照抄进 MCP 只会得到静默失效或误导性的参数，所以本模块把两类东西分清楚：

  1. **结构性不可用（STRUCTURAL_GAPS）** —— 与部署环境无关，永远不可能在 MCP 侧成立。
     只做记录与文档，不注册对应工具，也不提供空壳参数。
  2. **环境性不可用（capability quarantine）** —— 本机缺 Python 包 / 缺 Node / 缺 Zotero /
     声明离线时，对应工具**不注册**（硬隔离），避免 agent 拿到一个必然报错的工具。

隔离结果可用环境变量覆盖：
  * ``KB_MCP_TOOLS``    白名单（逗号或空格分隔），只注册列出的工具
  * ``KB_MCP_EXCLUDE``  黑名单，排除列出的工具
  * ``KB_MCP_NO_PROBE`` =1 时跳过环境探测，只应用上面两个列表

被隔离的工具不会出现在 MCP 工具列表里；它们的名字与原因由 ``kb_mcp_status`` 工具报告，
因此"工具不见了"永远能查到原因，而不是无声蒸发。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------- 结构性差异

#: MCP 协议内没有承载面的能力。每一项都写明"DSH 侧靠什么实现"与"为什么 MCP 侧给不了"。
STRUCTURAL_GAPS = (
    {
        "capability": "kb_scope",
        "ds": "工具 + 会话级状态（scope/depth/strict/diligence/save）",
        "mcp": "MCP 无会话概念，一次调用即一次独立请求；没有任何跨调用的状态承载面",
    },
    {
        "capability": "strict / 严格模式",
        "ds": "宿主侧把约束注入模型提示并改写结果层提示文本",
        "mcp": "MCP 服务不参与提示构造，只返回字符串结果，无法约束调用方模型的作答范围",
    },
    {
        "capability": "scope=both / web 联网兜底",
        "ds": "宿主编排 kb 检索与 web_search 两条来源",
        "mcp": "MCP 服务不能调用宿主的 web 检索工具，只能返回库内结果",
    },
    {
        "capability": "diligence=thorough 深挖循环",
        "ds": "宿主按调用预算解除上限并引导多轮检索",
        "mcp": "循环由调用方 agent 决定，服务侧无法强制",
    },
    {
        "capability": "会话开场范围询问 / state.json 默认值",
        "ds": "会话生命周期事件 + 工作区 .kb-rag/state.json",
        "mcp": "MCP 服务无会话事件，也没有约定的状态文件位置",
    },
    {
        "capability": "三档关闭 + /kb 命令",
        "ds": "commands 服务注册的用户命令与工具撤销",
        "mcp": "MCP 无人类命令通道；关停应通过 KB_MCP_EXCLUDE 在注册期完成",
    },
    {
        "capability": "结果层提示规则 / 节流记账",
        "ds": "按规则改写工具结果并记账",
        "mcp": "MCP 结果只有一份文本，没有分层提示面",
    },
    {
        "capability": "来源卡片 / 会话指示条",
        "ds": "客户端半边注册 toolview 与 header 插槽",
        "mcp": "MCP 无 UI",
    },
    {
        "capability": "旧解析器数据询问 / 自动刷新",
        "ds": "会话内检测分块版本并在提示层询问",
        "mcp": "只能由调用方显式传 metadata_only / rebuild",
    },
)

#: 引擎命令 → 本模块的探测键。工具不在这里时视为无环境前置条件。
TOOL_REQUIREMENTS = {
    "kb_ingest": ("pymupdf",),
    "kb_status": (),
    "kb_zotero": ("pymupdf", "zotero"),
    "kb_search": (),
    "kb_rag": (),
    "kb_stats": (),
    "kb_dedup": (),
    "kb_clear": (),
    "kb_fetch": ("network",),
    "kb_mcp_status": (),
}

#: 所有可能注册的工具名（诊断工具按固定顺序输出用）。
ALL_TOOLS = tuple(TOOL_REQUIREMENTS)

#: 缺失时的修复指引，直接进隔离原因，避免"看见红字不知道怎么办"。
REMEDIES = {
    "pymupdf": "pip install 'PyMuPDF>=1.24'（PDF 解析：入库/迁移需要）",
    "faiss": "pip install 'faiss-cpu>=1.8'（向量检索：缺失会退化为关键词匹配）",
    "sentence_transformers": "pip install 'sentence-transformers>=3.0'（嵌入模型：缺失则无法生成向量）",
    "node": "安装 Node.js（kb_fetch 的首选下载通道；缺失时引擎会退回 Python 通道）",
    "zotero": "本机未找到 zotero.sqlite；用 KB_MCP_ZOTERO_DB 指定路径，或调用时传 zotero_db",
    "network": "已声明离线（KB_RAG_OFFLINE=1）；联网下载在离线环境下不可用",
}


def _find_spec(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _zotero_candidates():
    """本机可能存在的 zotero.sqlite 位置（与引擎侧默认搜索顺序一致）。"""
    home = Path.home()
    cands = [home / "Zotero" / "zotero.sqlite", home / "Documents" / "Zotero" / "zotero.sqlite"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append(Path(appdata) / "Zotero" / "Zotero" / "zotero.sqlite")
    return cands


def probe_environment(engine_available=True):
    """探测本机环境能力。只做廉价的 stat / find_spec，不导入重库、不发网络请求。

    ``engine_available`` 由调用方给出（引擎文件是否存在），探测本身不复制该判断。
    """
    caps = {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "engine": bool(engine_available),
        "pymupdf": _find_spec("fitz") or _find_spec("pymupdf"),
        "faiss": _find_spec("faiss"),
        "sentence_transformers": _find_spec("sentence_transformers"),
        "numpy": _find_spec("numpy"),
        "node": shutil.which("node") is not None,
        "offline": os.environ.get("KB_RAG_OFFLINE", "").strip() in ("1", "true", "yes"),
    }
    caps["network"] = not caps["offline"]
    explicit = os.environ.get("KB_MCP_ZOTERO_DB", "").strip()
    if explicit:
        caps["zotero"] = Path(explicit).is_file()
        caps["zotero_db"] = explicit if caps["zotero"] else None
    else:
        found = next((p for p in _zotero_candidates() if p.is_file()), None)
        caps["zotero"] = found is not None
        caps["zotero_db"] = str(found) if found else None
    return caps


def _parse_list(value):
    raw = (value or "").replace(",", " ").split()
    return [x.strip() for x in raw if x.strip()]


def _unknown(names, known):
    return sorted(n for n in names if n not in known)


def quarantine(caps):
    """算出每个工具的隔离结论。

    返回 ``(enabled, quarantined, notes)``：
      * enabled      可注册的工具名（保持 ALL_TOOLS 的顺序）
      * quarantined  {工具名: 原因}，工具不会被注册
      * notes        非致命的能力缺口（工具仍可用，但会退化），供 kb_mcp_status 展示
    """
    notes = []
    if not caps.get("faiss"):
        notes.append("缺 faiss：向量检索不可用，kb_search/kb_rag 会退化为关键词匹配（%s）" % REMEDIES["faiss"])
    if not caps.get("sentence_transformers"):
        notes.append("缺 sentence-transformers：无法生成向量，入库后只能走关键词路（%s）"
                     % REMEDIES["sentence_transformers"])
    if not caps.get("node"):
        notes.append("缺 node：kb_fetch 走 Python 下载通道（%s）" % REMEDIES["node"])
    if caps.get("offline"):
        notes.append("KB_RAG_OFFLINE=1：联网相关能力已被声明为不可用")

    quarantined = {}
    no_probe = os.environ.get("KB_MCP_NO_PROBE", "").strip() in ("1", "true", "yes")
    if not caps.get("engine"):
        # 引擎文件缺失是绝对条件：KB_MCP_NO_PROBE 也不能把必然报错的工具放回来。
        engine_reason = "引擎文件缺失：mcp-server/../kb_engine.py 不存在，所有引擎工具不可用"
        for name in ALL_TOOLS:
            if name != "kb_mcp_status":
                quarantined[name] = engine_reason
    elif not no_probe:
        for name in ALL_TOOLS:
            if name == "kb_mcp_status":
                continue
            missing = [req for req in TOOL_REQUIREMENTS[name] if not caps.get(req)]
            if missing:
                quarantined[name] = "；".join(
                    "%s 不可用 —— %s" % (m, REMEDIES.get(m, "环境不满足")) for m in missing)

    # 显式列表最后应用：白名单收窄、黑名单剔除。两者都**不能**覆盖上面的能力隔离 ——
    # 白名单只能让工具更少，不能把一个必然报错的工具放回来。
    allow = _parse_list(os.environ.get("KB_MCP_TOOLS"))
    deny = _parse_list(os.environ.get("KB_MCP_EXCLUDE"))
    unknown = _unknown(allow, ALL_TOOLS) + _unknown(deny, ALL_TOOLS)
    if unknown:
        notes.append("KB_MCP_TOOLS/KB_MCP_EXCLUDE 里有未知工具名（已忽略）：%s" % ", ".join(sorted(set(unknown))))
    if allow:
        for n in ALL_TOOLS:
            if n not in allow:
                quarantined.setdefault(n, "不在 KB_MCP_TOOLS 白名单内")
            elif n in quarantined:
                quarantined[n] += "（虽在 KB_MCP_TOOLS 白名单内，但能力不满足，仍未注册）"
    if deny:
        for n in deny:
            if n in ALL_TOOLS:
                quarantined[n] = "被 KB_MCP_EXCLUDE 显式排除"
    return [n for n in ALL_TOOLS if n not in quarantined], quarantined, notes

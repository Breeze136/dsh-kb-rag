# 卸载 kb-rag

卸载分成几层，按需执行。**核心原则：kb-rag 从不碰你的原始数据，删除也只需删它自己生成的索引与缓存。**

## 安全边界（务必先读）

kb-rag 的读写边界：

- **只读**：你的 PDF 文献文件夹、Zotero 数据库（`zotero.sqlite`）与附件（`storage/`）——**从不写入、从不删除**。
- **只写**：它自己的索引目录（`kb.sqlite` 及所在 `.kb` / `~/.kb-rag` 文件夹）与本地模型缓存。

所以删库、卸载插件，都不会动你的 PDF 或 Zotero 一个字。原始文献始终安全。

## 第 1 层：停用插件（数据全保留）

**DSH：**

```powershell
dsh plugin --profile web remove dsh-kb-rag
```

若该命令不可用，手动编辑 `C:\Users\<你>\.dsh\profiles\web\package.json`：
删除 `dependencies` 里的 `"dsh-kb-rag"`、`dsh.profile.bundles` 数组里的 `"dsh-kb-rag"`，然后重启 DSH。

**MCP（Kimi Code / DeepSeek / Zcode / Claude Desktop 等）：**
在客户端的 MCP 配置里删掉 `kb-rag` 这一项即可。

## 第 2 层：删知识库索引（可选，是衍生数据）

删除对应的 `kb.sqlite` 或整个索引目录。这些是"从 PDF 抽出的分块 + 向量"，删了只是丢索引；原 PDF 还在原地，随时可 `kb_ingest` 重建。

默认位置：

| 形态 | 默认索引目录 |
| --- | --- |
| DSH 插件 | `<会话工作区>/.kb` |
| MCP | `~/.kb-rag`（可用 `KB_RAG_ROOT` 覆盖） |

示例（Windows PowerShell）：

```powershell
# MCP 默认库
Remove-Item -Recurse -Force "$env:USERPROFILE\.kb-rag"
# DSH 某工作区库
Remove-Item -Recurse -Force "C:\path\to\workspace\.kb"
```

## 第 3 层：删模型缓存（可选，省约 1.2GB）

只删 kb-rag 用的两个模型；**不要删整个 huggingface 目录**（其他工具可能共用）：

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.cache\huggingface\hub\models--BAAI--bge-small-zh-v1.5"
Remove-Item -Recurse -Force "$env:USERPROFILE\.cache\huggingface\hub\models--BAAI--bge-reranker-base"
```

## 第 4 层：卸 Python 依赖（可选，谨慎）

这些包可能被其他工具共用：

```powershell
pip uninstall pymupdf faiss-cpu sentence-transformers
```

## 绝不删除的清单（对照）

- ❌ 你的原始 PDF / 文献文件夹
- ❌ `Zotero/`（zotero.sqlite + storage）
- ❌ DSH 会话、其他插件、其他工作区

## 彻底卸载的最小操作

停用（第 1 层）+ 删索引（第 2 层）即可，两步都碰不到原文。模型缓存与 Python 依赖可视磁盘空间决定是否清理。

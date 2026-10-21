# 1.6.8 迁移说明（老用户）

1.6.8 起，kb-rag 只支持 DSH 0.2 及以上，并且只面向 **DSH 桌面端**。web profile 不再是受支持的安装目标。

先看自己属于哪种情况。

## A. 桌面端已经装了 1.6.7

不要直接点安装 1.6.8。插件面板会拿已安装的 1.6.7 清单做兼容检查，报 `1.6.7 ... 不兼容`，而卡片上显示的是 1.6.8。

1. 打开 **设置 → 插件**；
2. 卸载现有的 `dsh-kb-rag`；
3. 完全退出 DSH，再打开；
4. 重新搜索 `kb-rag`，安装 **1.6.8**；
5. 重启 DSH，新建会话。

验证：

```powershell
(Get-Content "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-kb-rag\package.json" -Raw |
  ConvertFrom-Json).version
```

应输出 `1.6.8`。新会话里应有 `kb_ingest`、`kb_search`、`kb_rag`、`kb_stats` 等 10 个工具。

如果面板还报 1.6.7，按 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 清一次面板缓存再装。

## B. 还在用 DSH web

web profile 不再支持。装 DSH 桌面端，按 A 安装插件。

知识库本身不迁移：它就在会话工作区的 `.kb` 下。桌面端打开**同一个项目文件夹**就能看到原库。如果桌面端用了新的工作区，把旧工作区的 `.kb` 复制过去，或调用时传 `kb_root` 指向旧路径。

确认桌面端一切正常后，把 web profile 的旧副本删掉：

```bash
dsh plugin --profile web remove dsh-kb-rag
```

如果还有脚本或 CI 依赖 web profile，可以先保留，但新功能只在桌面端验证。

## C. 用 MCP 的（Claude Desktop / Cursor / Kimi 等）

不受影响。MCP 服务器和 DSH 插件是两条独立路径，继续按 [mcp-server/README.md](../mcp-server/README.md) 配置即可。

## 数据会怎样

- schema 仍是 **v4**，`PARSER_REV` 仍是 **5**；
- 不需要重灌文库；
- Zotero 路径、`.kb` 数据库结构不变；
- 变的只是插件宿主：从 web profile 换到桌面端。

模型缓存在用户目录，桌面端会复用，不需要重新下载 1.2 GB。

## 出问题怎么问

先按 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 查。提问时带上：

- DSH 版本（`dsh --version` 或桌面端“关于”）；
- 插件版本；
- 操作系统；
- 完整报错原文，不要转述；
- `kb_stats` 输出（如果插件能加载）；
- profile `package.json` 里的 `dependencies` 和 `dsh.profile.bundles`。

不要贴 `.npmrc`、token 或完整用户目录。

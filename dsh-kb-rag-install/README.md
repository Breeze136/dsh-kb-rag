# dsh-kb-rag-install — 一行命令安装 dsh-kb-rag

```bash
npx dsh-kb-rag-install
```

就这一条命令，自动完成：

1. **Python 依赖**（PyMuPDF / numpy / faiss / sentence-transformers / torch，pip 镜像可加速）
2. **引擎冒烟测试**（kb_engine.py 自检）
3. **Node / pnpm 检查**（缺 pnpm 自动装，失败自动回退 corepack）
4. **DSH 插件安装激活**（profile 未指定时自动检测 `~/.dsh/profiles/`，只有一个直接用）
5. **模型预下载**（bge-small-zh + bge-reranker-base，直连失败自动切 hf-mirror.com 镜像重试）

不想预下载模型（等首次检索时再下）：`npx dsh-kb-rag-install --no-models`。
指定 DSH profile：`npx dsh-kb-rag-install --profile web`。
只演练不安装：`npx dsh-kb-rag-install --dry-run`。

装完重启 DSH、开新会话即可使用 9 个 `kb_*` 工具。

> 本包只是安装入口（零逻辑）；主体是 [dsh-kb-rag](https://www.npmjs.com/package/dsh-kb-rag)，
> 功能与文档见其仓库 https://github.com/Breeze136/dsh-kb-rag 。

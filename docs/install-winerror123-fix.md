# dsh-kb-rag `install.ps1` 修复报告：中文用户名导致 WinError 123

**受影响版本**：1.6.2（npm 最新版）及 `main` 分支
**受影响平台**：Windows + Windows PowerShell 5.1 + 用户名含非 ASCII 字符
**修复成本**：1 行代码
**建议**：合并后发 1.6.3

---

## 一、摘要

`install.ps1` 在**用户名含中文**的 Windows 上必然失败在第 3 步引擎冒烟测试：

```
== 3/5 引擎冒烟测试 (kb_engine.py stats)
[XX] {"ok": false, "error": "OSError: [WinError 123] 文件名、目录名或卷标语法不正确。:
     'C:\\Users\\??\\AppData\\Local\\Temp\\kbrag-smoke-dcb891cf'"}
[XX] 引擎冒烟测试失败
```

路径里的 `??` 不是终端显示问题，**引擎收到的字符串里就是两个问号**。

根因不在引擎，而在 PowerShell 5.1 的管道编码。修复方式是在 `install.ps1` 顶部加一行：

```powershell
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
```

---

## 二、复现

环境：Windows、Python 3.10.9 (Miniconda3)、用户名 `岳通`（任何非 ASCII 用户名均可）

```powershell
npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"
```

或直接跑脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -SkipPip -SkipNode -SkipDsh
```

第 3 步必失败，退出码 1。

**注意这只在含非 ASCII 字符的 `%TEMP%` 下复现**。把 `TEMP` 指向纯 ASCII 路径（如 `C:\kbragtmp`）
即可绕过 —— 这大概也是它一直没被发现的原因。

---

## 三、根因

### 3.1 触发链

`install.mjs` 用 `spawnSync("powershell", ...)` 拉起的是 **Windows PowerShell 5.1**，
而它的 `$OutputEncoding` 默认值是 `System.Text.ASCIIEncoding`：

```powershell
PS> powershell -NoProfile -Command '$OutputEncoding.GetType().FullName'
System.Text.ASCIIEncoding
```

`install.ps1` 第 3 步：

```powershell
$smokeDir = Join-Path ([System.IO.Path]::GetTempPath()) ("kbrag-smoke-" + ...)
$req = '{"kb_root":"' + ($smokeDir -replace '\\', '/') + '"}'
$out = $req | & $Py $Engine stats 2>&1 | Out-String     # ← 整个脚本唯一的管道
```

1. `GetTempPath()` 返回 `C:\Users\岳通\AppData\Local\Temp\`，路径含中文；
2. 拼成 JSON 后**经 stdin 管道**送给 `python kb_engine.py stats`；
3. 管道写给原生进程 stdin 的内容按 **ASCII** 编码，**所有非 ASCII 字符静默变成 `?`**；
4. 引擎按 `sys.stdin.buffer.read().decode("utf-8")` 严格解析，拿到的就是字面 `??`；
5. Windows 上 `?` 是非法文件名字符，`mkdir` 抛 `OSError [WinError 123]`。

### 3.2 字节级证据

```powershell
$OutputEncoding 为 ASCII 时，python 从 stdin 读到：
b'{"kb_root":"C:/Users/??/AppData/Local/Temp/kbrag-smoke-dcb891cf"}\r\n'
```

注意：`??` 是**两个字节 0x3F 0x3F**，不是编码错误后的替换字符 —— 是发送端就已经写坏了。

### 3.3 引擎本身没有问题

把 `kb_root` 直接设成中文目录，完全正常：

```powershell
PS> python kb_engine.py stats  <<< '{"kb_root":"D:/workspace/文献库-smoke"}'
{"ok": true, "docs": 0, ...}      # SQLite 正常建在中文目录里
```

**引擎完全支持中文路径。坏的只有 PowerShell → Python 这一次管道。**

同理，插件**运行期**也没有这个问题：`lib/index.js` 的 `handle.stdin.write(...)` 走 Node 流，
默认 UTF-8。所以这个 bug 只影响**安装阶段**。

---

## 四、修复

在 `scripts/install.ps1` 中，`$ErrorActionPreference = "Continue"`（第 18 行）之后插入 5 行：

```powershell
# [kb-rag-fix] Windows PowerShell 5.1 的 $OutputEncoding 默认是 ASCII：路径里的非 ASCII 字符
# （例如中文用户名 C:\Users\<用户>\AppData\Local\Temp）写进原生进程的 stdin 时会变成 "?"，
# 引擎因此拿到非法路径，报 OSError [WinError 123]。这里统一强制 UTF-8（不带 BOM：
# 引擎用 raw.decode("utf-8") 解析 stdin），覆盖本脚本所有通向 python 的管道。
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
```

### 四个要点

| 要点 | 原因 |
|---|---|
| 放在**脚本顶部**，而不是那一行前面 | `$OutputEncoding` 是「管道写向原生进程」的总开关，放顶部能顺带防住以后新增的管道 |
| 必须是 **UTF-8 不带 BOM** | 引擎用 `raw.decode("utf-8")` 解析 stdin；带 BOM 会让 `json.loads` 直接失败（`Unexpected UTF-8 BOM`） |
| 对 PowerShell 7 无害 | PS7 默认已是 UTF-8 |
| 不污染外部环境 | `powershell -File` 每次都是独立会话 |

### 完整 patch（基于当前 `main`，git 格式）

```diff
diff --git a/npm-package/scripts/install.ps1 b/npm-package/scripts/install.ps1
--- a/npm-package/scripts/install.ps1
+++ b/npm-package/scripts/install.ps1
@@ -16,6 +16,11 @@
 )
 
 $ErrorActionPreference = "Continue"
+# [kb-rag-fix] Windows PowerShell 5.1 的 $OutputEncoding 默认是 ASCII：路径里的非 ASCII 字符
+# （例如中文用户名 C:\Users\<用户>\AppData\Local\Temp）写进原生进程的 stdin 时会变成 "?"，
+# 引擎因此拿到非法路径，报 OSError [WinError 123]。这里统一强制 UTF-8（不带 BOM：
+# 引擎用 raw.decode("utf-8") 解析 stdin），覆盖本脚本所有通向 python 的管道。
+$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
 $PyProbeModules = @("fitz", "numpy", "faiss", "sentence_transformers", "torch")
 $PkgOf = @{ fitz = "PyMuPDF"; faiss = "faiss-cpu"; sentence_transformers = "sentence-transformers"; docx = "python-docx" }
 if (-not $Mirror -and $env:PIP_INDEX_URL) { $Mirror = $env:PIP_INDEX_URL }
```

> 补丁本身**没有任何其他改动**，净增 5 行。
> ⚠️ 合并时请保持该文件原有的 **UTF-8 BOM** —— PS 5.1 读无 BOM 的 UTF-8 会按系统 ANSI
> 代码页解析，文件里的中文注释会变乱码并可能报语法错。

---

## 五、实测对照

全部在**全新 `powershell -NoProfile` 子进程**里执行（即 `install.mjs` 真正拉起的那种），
`TEMP`/`TMP` 指向真实中文目录：

| 场景 | 结果 |
|---|---|
| 原版 1.6.2 + 中文临时目录 | ❌ 退出码 1<br>`OSError [WinError 123] ... 'D:\\workspace\\????\\kbrag-smoke-9b9bf473'` |
| **修复版** + 中文临时目录 | ✅ 退出码 0<br>`[OK] engine v3.0.0 自检通过` |
| 原版 1.6.2 + 纯 ASCII 临时目录 | ✅ 退出码 0（即当前的临时绕行办法） |

修复版完整输出：

```
[OK] found: python (3.10)
== 3/5 引擎冒烟测试 (kb_engine.py stats)
[OK] engine v3.0.0 自检通过
[OK] embed: BAAI/bge-small-zh-v1.5 已缓存
[OK] rerank: BAAI/bge-reranker-base 已缓存
```

另外已确认无害的部分：`Get-HfCacheRoot`、`USERPROFILE` 这些带中文的路径只参与
PowerShell 内部的 `Test-Path`，不经过管道，不受影响；`scripts/install.sh` 也没有这个问题
（bash 直接把字节写进管道，不做编码转换）。

---

## 六、影响面

**所有用户名含中文（或其他非 ASCII 字符）的 Windows 用户**都会在安装时撞上这一步。
中文 Windows 用户名在国内极其普遍（`C:\Users\张三`、`C:\Users\岳通`…），
所以实际受影响人群不小。用户侧的临时绕行办法是：

```powershell
$env:TEMP = 'C:\kbragtmp'; $env:TMP = 'C:\kbragtmp'
```

---

## 七、建议顺带补一行 README 排错表

| 报错 / 现象 | 原因 | 处理 |
|---|---|---|
| `WinError 123 ... C:\Users\??\...` | 用户名含非 ASCII 字符时，PowerShell 5.1 的 `$OutputEncoding` 默认是 ASCII，会把路径里的中文写成 `?`（1.6.2 及以前） | 升级到修复版；或临时 `$env:TEMP='C:\kbragtmp'` 后重跑 |

---

## 八、附：文件信息

| 项 | 值 |
|---|---|
| 目标文件 | `npm-package/scripts/install.ps1` |
| 上游 `main` 基线 | 11961 bytes / UTF-8 BOM / LF / 253 行 |
| 修复后 | 12445 bytes / UTF-8 BOM / LF / 258 行 |
| 净增 | 5 行（4 行注释 + 1 行代码） |
| npm 发布版差异 | 发布包内该文件为 **CRLF**（main 为 LF），文本内容一致 |

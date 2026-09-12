// kb-rag DSH dynamic plugin — Host half (v1.6.7)
// 用法：把本文件内容作为 cordis_define 的 code.host（纯函数体，直接粘贴）。
// 依赖：工作区根目录存在 kb_engine.py；Python 环境装有 PyMuPDF/faiss-cpu/sentence-transformers。
    // >>> kb-guidance-mirror（由 tools/sync-host-guidance.mjs 生成，勿手改）
    // 源：npm-package/lib/guidance.js（唯一事实来源）。改提示请改那边，再跑 sync 脚本。
    const KB_GUIDANCE = {
      "discipline": "调用纪律（重要，违反会显著拖慢回答）：\n1. 一次提问最多调用本工具 3 次，每次必须换实质策略（中文→英文术语 / 放宽 filters / 换同义术语），不要反复改写同一句话。\n2. 返回 verdict=无关（或结果分数低于阈值）时：库内确实没有 → 如实说明\"库内无资料\"，并按 scope 设置转 web_search；不要再换词连试。\n3. 返回 verdict=弱相关时：最多升一次 depth=deep；仍弱则按第 2 条处理。\n4. 用户说\"先联网/快点/不用查库\"→ 用 scope=web 或直接 web_search；用户说\"库里有没有/只查库\"→ scope=kb，只查一次。\n5. 中文提问先转写成英文术语再检索（库内正文以英文为主，BM25 对中文空转）；转写一次即可。",
      "thorough": "**深挖模式已开启**（用户明确要求彻底查找；调用次数不设上限，优先把事情查透）：\n1. 逐术语、逐角度检索：同一问题可以换多组英文术语、放宽或更换 filters，直到覆盖主题各个侧面。\n2. 库内只有一两篇相关文献时，**不要把\"就这么点\"当成结论**：从结果的 citations 里取 DOI，\n   用 kb_fetch({ identifiers: [doi], ingest: true }) 下载并**直接入库**，再对新增文献继续检索。\n3. 循引文与 related 横向展开：新入库的文献再查一次引文关联，把它们的参考文献也纳入候选。\n4. 每轮把「新增了什么 / 还缺什么」简短告诉用户，直到收敛（没有新文献、没有新结论）或用户喊停。\n5. 增量入库按 sha256 自动跳过已入库文件，重复调用安全；大批量会自动转后台，用 kb_status 轮询。\n6. 只有在**确实把所有角度都检索完**之后，才可以说\"库内无相关资料\"。",
      "defaults": "默认：深度=quick（亚秒级）。需要跨文献综合/精排时显式传 depth=deep（每次多约 1.9 s）。",
      "floor": 0.1,
      "maxCalls": 3
    }
    // 检索纪律（描述层）：默认档给调用上限，深挖档给补库循环。
    function kbDisciplineText(diligence) {
      const pointer = '注：用户明确要求彻底查找时（\'仔细找/慢慢来/别省时间/把相关文献都找齐\'），'
        + '先 kb_scope({ diligence: "thorough" })（或用 /kb thorough）——该模式下调用上限解除，'
        + '改为「反复检索 → kb_fetch(ingest=true) 补库 → 引文关联 → 增量入库 → 再查」的循环。'
      return diligence === 'thorough'
        ? (KB_GUIDANCE.thorough + '\n' + KB_GUIDANCE.defaults)
        : (KB_GUIDANCE.discipline + '\n' + KB_GUIDANCE.defaults + '\n' + pointer)
    }
    // 结果层：无命中 / 弱相关 / 向量降级三种情形（深挖档把"叫停"换成"继续补库"）。
    function kbResultNotes(value, diligence) {
      const out = []
      const list = (value && (value.results || value.evidence)) || []
      const maxScore = list.length > 0
        ? Math.max.apply(null, list.map(function (r) { return Number(r && r.score) || 0 })) : null
      const thorough = diligence === 'thorough'
      const noHit = Boolean(value && (value.no_hit === true || value.verdict === '无关'
        || (list.length === 0 && value.ok === true)))
      if (noHit) {
        out.push(thorough
          ? '深挖模式：本轮没命中，**不要收尾**。① 换术语或放宽 filters 再检索；② 从已命中结果的 citations 取 DOI，用 kb_fetch({ identifiers: [doi], ingest: true }) 补库后重查；③ 用 related 列表横向扩展。'
          : ('⚠ 库内无相关资料' + (maxScore !== null ? '（最高分 ' + maxScore.toFixed(2) + '，低于阈值 ' + KB_GUIDANCE.floor + '）' : '') + '：不要再换词重试；如实说明库内没有，并按 scope 转 web_search。'))
      } else if (value && value.verdict === '弱相关' && !thorough) {
        out.push('提示：本次结果相关性偏弱（最高分 ' + (maxScore === null ? '?' : maxScore.toFixed(2)) + '）。最多再升一次 depth=deep；仍弱就按「库内无资料」处理。')
      }
      if (value && (value.mode_used === 'keyword' || value.embedding_error || value.vectors_missing > 0)) {
        out.push('注意：向量链路当前不可用' + (value.embedding_error ? '（' + String(value.embedding_error).slice(0, 120) + '）' : '')
          + '，本次检索已退化为纯关键词。请在回答中说明，并提示用户重跑 kb_ingest 可补齐缺失向量。')
      }
      if (value && value.kb_rag_disabled === true) {
        out.push('kb-rag 当前处于关闭状态，本次调用未检索。请直接使用 web_search，或提示用户用 /kb on 开启。')
      }
      return out
    }
    // <<< kb-guidance-mirror
return {
  name: 'kb-rag',
  // subprocess 必须声明在 inject 里：动态插件沙箱的 ctx.get(name) 是"可选查询"，服务还没注册时
  // 返回 undefined，下面的兜底就会静默丢工具；声明后 cordis 会 park 本插件直到服务就绪（issue #1）。
  inject: ['timer', 'subprocess'],
  apply(ctx) {
    const subprocess = ctx.get('subprocess')
    const sandboxPolicy = ctx.get('sandboxPolicy')
    if (subprocess === undefined) {
      console.error('[kb-rag] subprocess service unavailable despite inject; tools not registered')
      return
    }

    let daemon = null
    let spawning = null
    let scopePref = 'kb'
    let scopeDepth = 'deep'
    let scopeStrict = false
    let scopeAsked = false
    let netEnv = 'unknown'
    let netAsked = false
    const userQuestions = ctx.get('userQuestions')

    // ---- 下载前网络环境:代理检测(env 变量;本机代理端口由引擎探测) ----
    function envProxyDetect() {
      const keys = ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']
      const set = []
      try {
        keys.forEach(function (k) { const v = process.env[k]; if (v) set.push(k + '=' + v) })
      } catch (e) { /* process 不可用时忽略 */ }
      return set
    }

    // 下载前询问网络环境(非阻塞,仅首次)
    function askNetworkOnce(agent) {
      if (netAsked || userQuestions === undefined) return
      netAsked = true
      const request = {
        questions: [{
          id: 'kb-net',
          header: '下载网络环境',
          question: 'kb_fetch 下载前确认：当前网络环境？(付费墙期刊的订阅版 PDF 只有校园网/机构 IP 才能直接下)',
          options: [
            { label: '校园网/机构网络', description: '可下出版商订阅版 PDF，将优先尝试出版商正式版' },
            { label: '家庭网络', description: '以 OA 开放获取为主，付费墙文献会提示手动下载' },
            { label: '不确定', description: '两者都试：先出版商正式版，失败自动转 OA' },
          ],
        }],
      }
      if (agent !== undefined) request.agent = agent
      Promise.race([
        userQuestions.ask(request).then(function (answer) {
          const picked = answer && answer.answers && answer.answers[0] && answer.answers[0].selected && answer.answers[0].selected[0]
          if (typeof picked === 'string') {
            if (picked.indexOf('校园网') === 0) netEnv = 'campus'
            else if (picked.indexOf('家庭') === 0) netEnv = 'home'
            else netEnv = 'unknown'
          }
          console.log('[kb-rag] download network env:', netEnv)
        }).catch(function (e) {
          console.error('[kb-rag] network question failed:', String(e))
        }),
        ctx.timeout(120000),
      ])
    }

    const SCOPE_NOTE = {
      kb: '范围：封闭知识库。仅基于库内文献作答；如需开放网络检索，用 kb_scope 切换范围。',
      both: '范围：知识库+全网。除本库内结果外，请再调用 web_search 检索开放网络，合并作答并分别标注来源。',
      web: '范围：仅全网。本次仅给出库内命中供参考；请以 web_search 结果为准作答。',
    }
    const STRICT_NOTE = '严格模式：答案仅允许基于本次检索返回的 evidence/results 内容；禁止补充库外知识、常识外延或未出现在证据中的文献与数据；证据不足时直接说明"根据现有资料无法回答"。'

    // 入库数据版本：先问引擎要"旧解析器入库"的文档数（stats.stale_docs）与陈旧类型
    // （stats.stale_kind：'chunk' 会改变分块/向量，必须全量重灌；'meta' 秒级刷元数据即可）。
    // 取不到（旧引擎/无库/调用失败）就静默跳过这条问题，绝不影响首次工具调用。
    function staleCountOf(kbRoot, exec) {
      return runEngine('stats', { kb_root: kbRoot }, exec).then(function (resp) {
        const n = resp ? Number(resp.stale_docs) : 0
        if (!Number.isFinite(n) || n <= 0) return { n: 0, kind: 'none' }
        return { n: n, kind: resp && resp.stale_kind === 'meta' ? 'meta' : 'chunk' }
      }).catch(function (e) {
        console.error('[kb-rag] stale check skipped:', String(e))
        return { n: 0, kind: 'none' }
      })
    }

    // 刷新库内旧数据：后台维护动作（不是工具调用，不渲染给模型），失败只写宿主日志。
    function refreshStale(kbRoot, exec, metaOnly) {
      const payload = metaOnly
        ? { kb_root: kbRoot, rebuild: true, metadata_only: true }
        : { kb_root: kbRoot, rebuild: true, async_if_large: true }
      runEngine('ingest', payload, exec).then(function (resp) {
        const jobId = resp && resp.job_id ? String(resp.job_id) : ''
        if (jobId) {
          console.log('[kb-rag] 全量重灌已转后台：job_id=' + jobId + '；用 kb_status(job_id="' + jobId + '") 轮询进度，宿主调用超时不会中断后台任务')
          return
        }
        const totals = (resp && resp.totals) || {}
        if (metaOnly) {
          console.log('[kb-rag] 元数据刷新完成：meta_updated=' + (totals.meta_updated || 0) + ' / 失败 ' + (totals.errors || 0))
        } else {
          console.log('[kb-rag] 重灌完成（引擎未转后台）：新增 ' + (totals.added || 0) + ' / 更新 ' + (totals.updated || 0) + ' / 失败 ' + (totals.errors || 0))
        }
      }).catch(function (e) {
        console.error('[kb-rag] stale refresh failed:', String(e))
      })
    }

    function askScopeOnce(agent, exec, kbRoot) {
      if (scopeAsked || userQuestions === undefined) return
      scopeAsked = true
      const root = typeof kbRoot === 'string' && kbRoot.length > 0 ? kbRoot : workspaceOf(exec) + '/.kb'
      staleCountOf(root, exec).then(function (staleInfo) {
        const stale = staleInfo && staleInfo.n ? staleInfo.n : 0
        const staleKind = staleInfo && staleInfo.kind ? staleInfo.kind : 'none'
        const request = {
          questions: [{
            id: 'kb-scope',
            header: '查询范围',
            question: '知识库查询的默认范围？',
            options: [
              { label: '仅封闭知识库（推荐）', description: '只检索本地文献库，结论只来自库内文献' },
              { label: '知识库+全网', description: '库内检索为主，开放网络（web_search）补充' },
              { label: '仅全网', description: '只用开放网络检索，不用知识库' },
            ],
          }, {
            id: 'kb-depth',
            header: '检索深度',
            question: '检索与作答的深度？',
            options: [
              { label: '快速检索', description: '混合召回直出，跳过精排与引文扩展，亚秒级响应，适合事实性查询与单点数据检索' },
              { label: '深度检索（推荐）', description: '重排序 + 引文关联 + 相关文献全链路，跨文献综合论述，适合领域调研与综述性问题' },
            ],
          }],
        }
        if (stale > 0) {
          // stale_kind='chunk' 时**不提供**"只刷新元数据"：分块/向量的改动刷元数据不生效，
          // 给用户一个无效选项比不给更糟（他会以为修好了）。
          const chunkLevel = staleKind === 'chunk'
          const opts = [
            { label: '暂不处理', description: '保持现状，随时可用 kb_ingest 的 metadata_only/rebuild 手动刷新' },
          ]
          if (!chunkLevel) {
            opts.push({ label: '只刷新元数据（推荐）', description: '秒级完成，仅重抽标题/作者/DOI，不重切块、不重嵌入' })
          }
          opts.push({
            label: chunkLevel ? '全量重灌（必须）' : '全量重灌（较慢）',
            description: chunkLevel
              ? '本次改动会改变分块与向量，只刷元数据不生效；重新解析并重新嵌入全部文档，期间转后台，可用 kb_status 查进度'
              : '重新解析并重新嵌入全部文档，期间会转后台，可用 kb_status 查进度',
          })
          request.questions.push({
            id: 'kb-stale',
            header: '入库数据版本',
            question: '库内有 ' + stale + ' 篇文档是用旧版解析器入库的（引擎的解析改进不会自动作用于已有数据）。是否刷新？',
            options: opts,
          })
        }
        if (agent !== undefined) request.agent = agent
        return Promise.race([
          userQuestions.ask(request).then(function (answer) {
            const picked = answer && answer.answers && answer.answers[0] && answer.answers[0].selected && answer.answers[0].selected[0]
            if (typeof picked === 'string' && picked.indexOf('仅封闭') === 0) scopePref = 'kb'
            else if (typeof picked === 'string' && picked.indexOf('知识库+全网') === 0) scopePref = 'both'
            else if (typeof picked === 'string' && picked.indexOf('仅全网') === 0) scopePref = 'web'
            const pickedDepth = answer && answer.answers && answer.answers[1] && answer.answers[1].selected && answer.answers[1].selected[0]
            if (typeof pickedDepth === 'string') {
              if (pickedDepth.indexOf('深度检索') === 0) scopeDepth = 'deep'
              else if (pickedDepth.indexOf('快速检索') === 0) scopeDepth = 'quick'
            }
            const pickedStale = answer && answer.answers && answer.answers[2] && answer.answers[2].selected && answer.answers[2].selected[0]
            if (typeof pickedStale === 'string' && pickedStale.indexOf('只刷新元数据') === 0) refreshStale(root, exec, true)
            else if (typeof pickedStale === 'string' && pickedStale.indexOf('全量重灌') === 0) refreshStale(root, exec, false)
            else if (typeof pickedStale === 'string') console.log('[kb-rag] 旧数据暂不刷新（需要时用 kb_ingest 的 metadata_only / rebuild）')
            console.log('[kb-rag] query scope:', scopePref, 'depth:', scopeDepth)
          }).catch(function (e) {
            console.error('[kb-rag] scope question failed:', String(e))
          }),
          ctx.timeout(120000),
        ])
      }).catch(function (e) {
        console.error('[kb-rag] scope question failed:', String(e))
      })
    }

    function scopeWrapped(exec, engineCall, strict, kbRoot) {
      askScopeOnce(exec && exec.agent, exec, kbRoot)
      return engineCall.then(function (resp) {
        resp.scope = scopePref
        resp.scope_note = SCOPE_NOTE[scopePref]
        resp.depth_note = scopeDepth === 'quick' ? '快速检索' : '深度检索'
        resp.strict = strict === true
        if (strict === true) resp.strict_note = STRICT_NOTE
        return resp
      })
    }

    function workspaceOf(exec) {
      try {
        const cwd = exec && exec.agent && exec.agent.session && exec.agent.session.header ? exec.agent.session.header.cwd : undefined
        if (typeof cwd === 'string' && cwd.length > 0) return cwd
      } catch (e) { /* fall through */ }
      if (sandboxPolicy !== undefined && typeof sandboxPolicy.workspaceRoot === 'string' && sandboxPolicy.workspaceRoot.length > 0) {
        return sandboxPolicy.workspaceRoot
      }
      return '.'
    }

    const sleep = (ms) => ctx.timeout(ms)

    async function spawnDaemon(root, exec) {
      const enginePath = root + '/kb_engine.py'
      let python = 'python'
      try {
        python = await subprocess.resolveExecutable('python', undefined, exec.signal)
      } catch (e) {
        console.error('[kb-rag] resolveExecutable python failed, using bare name:', String(e))
      }
      const handle = subprocess.spawn({
        argv: [python, enginePath, 'serve'],
        cwd: root,
        stdio: {
          stdin: 'pipe',
          stdout: { maxBytes: 32 * 1024 * 1024, spill: { maxBytes: 128 * 1024 * 1024 } },
          stderr: { maxBytes: 2 * 1024 * 1024 },
        },
        graceMs: 5000,
      })
      const d = { root, handle, offset: 0, queue: Promise.resolve(), seq: 0, dead: false }
      handle.done.then(function (out) {
        d.dead = true
        if (daemon !== d) return
        if (out.exitCode !== 0) {
          const err = handle.collected.stderr !== undefined ? handle.collected.stderr.readFrom(0).text : ''
          console.error('[kb-rag] engine daemon exited', out.exitCode, String(err).slice(0, 300))
        }
      })
      return d
    }

    async function perform(d, command, payload, exec) {
      if (d.dead) throw new Error('kb engine daemon is down; retry the call')
      const id = ++d.seq
      try {
        d.handle.stdin.write(JSON.stringify({ id, command, payload }) + '\n')
      } catch (e) {
        d.dead = true
        throw new Error('kb engine daemon write failed: ' + String(e && e.message || e))
      }
      const longCommand = command === 'ingest' || command === 'zotero'
      const deadline = Date.now() + (longCommand ? 1800000 : 150000)
      while (true) {
        if (d.handle.collected.stdout === undefined) throw new Error('kb engine daemon has no stdout reader')
        const read = d.handle.collected.stdout.readFrom(d.offset)
        d.offset = read.nextOffset
        if (read.text.length > 0) {
          for (const line of read.text.split('\n')) {
            const t = line.trim()
            if (t.length === 0) continue
            let parsed = null
            try { parsed = JSON.parse(t) } catch (e) { parsed = null }
            if (parsed === null || parsed.id !== id) continue
            if (parsed.ok !== true) throw new Error('kb engine error: ' + String(parsed.error || 'unknown').slice(0, 800))
            const resp = parsed.response
            if (resp === undefined || resp === null || resp.ok !== true) {
              throw new Error('kb engine error: ' + String((resp && resp.error) || 'unknown').slice(0, 800))
            }
            return resp
          }
          continue
        }
        if (read.lossy) throw new Error('kb engine output truncated' + (read.spillPath ? ' (spill ' + read.spillPath + ')' : ''))
        if (d.dead) throw new Error('kb engine daemon exited before answering')
        if (exec && exec.signal && exec.signal.aborted) throw new Error('tool call aborted')
        if (Date.now() > deadline) throw new Error('kb engine daemon timed out')
        await sleep(10)
      }
    }

    async function runEngine(command, payload, exec) {
      const root = workspaceOf(exec)
      if (daemon !== null && daemon.root !== root) {
        const old = daemon
        daemon = null
        try { old.handle.terminate() } catch (e) { /* ignore */ }
      }
      if (daemon === null || daemon.dead) {
        if (spawning === null) {
          spawning = spawnDaemon(root, exec).then(function (d) { daemon = d; spawning = null; return d }, function (e) { spawning = null; throw e })
        }
        await spawning
      }
      const d = daemon
      const call = d.queue.then(function () { return perform(d, command, payload, exec) })
      d.queue = call.then(function () {}, function () {})
      return call
    }

    ctx.effect(() => () => {
      if (daemon !== null) {
        try { daemon.handle.terminate() } catch (e) { /* ignore */ }
        daemon = null
      }
    })

    // 启动时自动检测 Python 依赖（pymupdf / faiss / sentence-transformers / torch），
    // 缺失时在宿主日志里打印安装命令；不阻塞插件加载。
    function checkPythonDeps() {
      let python = 'python'
      const probe = subprocess.resolveExecutable('python').catch(() => { /* keep bare name */ }).then((resolved) => {
        if (typeof resolved === 'string' && resolved.length > 0) python = resolved
        let handle
        try {
          handle = subprocess.spawn({
            argv: [python, '-c', 'import fitz, faiss, sentence_transformers, torch'],
            cwd: workspaceOf(undefined) || '.',
            stdio: {
              stdin: 'ignore',
              stdout: { maxBytes: 4096 },
              stderr: { maxBytes: 16384 },
            },
            graceMs: 3000,
          })
        } catch (e) {
          console.error('[kb-rag] dependency check failed to spawn:', String(e && e.message || e))
          return
        }
        handle.done.then((out) => {
          if (out.exitCode === 0) {
            console.log('[kb-rag] Python dependencies OK')
            return
          }
          const err = handle.collected.stderr !== undefined ? handle.collected.stderr.readFrom(0).text : ''
          const missing = []
          if (String(err).includes('fitz')) missing.push('pymupdf')
          if (String(err).includes('faiss')) missing.push('faiss-cpu')
          if (String(err).includes('sentence_transformers')) missing.push('sentence-transformers')
          if (String(err).includes('torch')) missing.push('torch')
          console.error('[kb-rag] Python dependencies missing: ' + (missing.length > 0 ? missing.join(', ') : 'unknown module'))
          console.error('[kb-rag] Install with: pip install ' + (missing.length > 0 ? missing.join(' ') : 'pymupdf faiss-cpu sentence-transformers'))
          console.error('[kb-rag] ' + String(err).trim().split('\n').slice(-2).join(' | ').slice(0, 500))
        })
      })
      probe.catch(() => { /* ignore */ })
    }
    checkPythonDeps()

    const kbRootOf = (args, exec) => typeof args.kb_root === 'string' && args.kb_root.length > 0 ? args.kb_root : workspaceOf(exec) + '/.kb'
    const renderJson = (_args, value) => [{ type: 'text', text: JSON.stringify(value) }]

    // 大批量入库：引擎已转后台，返回的是 job 句柄而不是入库结果——不能按入库结果渲染。
    const renderIngestAsync = (_args, value) => {
      if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
      const jobId = value.job_id ? String(value.job_id) : ''
      const lines = []
      lines.push('**已转入后台处理**' + (jobId.length > 0 ? ' · job_id ' + jobId : ''))
      if (typeof value.pending_files === 'number') lines.push('待处理文件 ' + value.pending_files + ' 篇')
      if (value.note) lines.push(String(value.note))
      // 引擎的 note 通常已经带了 kb_status 指引，此时不要再重复一遍
      const noteHasHint = typeof value.note === 'string' && value.note.indexOf('kb_status') >= 0
      if (!noteHasHint) {
        lines.push('')
        lines.push('用 kb_status(job_id="' + jobId + '") 轮询进度；宿主调用超时不会中断后台任务，结果在完成时返回 totals。')
      }
      return [{ type: 'text', text: lines.join('\n') }]
    }

    // 元数据刷新（metadata_only）：只更新 docs 的元数据字段，totals 里是 meta_updated 系列，
    // 没有 added/updated/chunks/vectors——按入库结果渲染会显示成"什么都没做"，必须单独渲染。
    const renderMetaRefresh = (_args, value) => {
      const totals = value.totals || {}
      const files = Array.isArray(value.files) ? value.files : []
      const lines = []
      lines.push('**元数据刷新完成** · 已刷新 ' + (totals.meta_updated || 0) + ' 篇'
        + '（其中 ' + (totals.meta_changed || 0) + ' 篇内容有变化）')
      const extra = []
      if (totals.changed) extra.push('跳过（文件内容已变，需正常入库）' + totals.changed + ' 篇')
      if (totals.not_indexed) extra.push('未入库 ' + totals.not_indexed + ' 篇')
      if (totals.errors) extra.push('失败 ' + totals.errors + ' 篇')
      if (extra.length > 0) lines.push(extra.join(' · '))
      const totalMs = typeof value.ms === 'number' ? value.ms : 0
      lines.push('总耗时 ' + (totalMs >= 1000 ? (totalMs / 1000).toFixed(1) + 's' : totalMs + 'ms')
        + ' · 未重切块、未重嵌入')
      if (files.length > 0) {
        lines.push('')
        lines.push('**最近刷新（滚动）**')
        files.slice(-8).forEach(function (f) {
          const nm = String(f.path || '').split(/[\\/]/).pop()
          const bits = [f.status || '']
          if (f.changed === true) bits.push('有变化')
          if (f.doi) bits.push('doi ' + String(f.doi))
          if (typeof f.ms === 'number') bits.push(f.ms + 'ms')
          lines.push('· ' + nm + ' · ' + bits.filter(Boolean).join(' · '))
        })
      }
      return [{ type: 'text', text: lines.join('\n') }]
    }

    // 入库/Zotero 迁移：紧凑滚动视图——总览一行 + 最近 N 条（文件名 + 耗时），不甩大 JSON。
    const renderIngest = (_args, value) => {
      if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
      if (value.background === true || (value.job_id !== undefined && value.status === 'running' && value.totals === undefined)) {
        return renderIngestAsync(_args, value)
      }
      if (value.mode === 'metadata_only') return renderMetaRefresh(_args, value)
      // Zotero 预演（dry_run）：引擎返回 candidates + 全部候选清单，totals 全 0——
      // 按入库结果渲染会显示成"入库完成 新增 0…"，看起来像什么都没干。
      if (value.dry_run === true) {
        const cands = Array.isArray(value.files) ? value.files : []
        const n = typeof value.candidates === 'number' ? value.candidates : cands.length
        const dry = []
        dry.push('**Zotero 预演**（dry_run，未写入库）· 候选 ' + n + ' 篇')
        if (value.zotero_db) dry.push('zotero.sqlite：' + String(value.zotero_db))
        const missing = cands.filter(function (f) { return f.status === 'missing' }).length
        if (missing > 0) dry.push('附件缺失（正常跳过）' + missing + ' 篇')
        if (cands.length > 0) {
          dry.push('')
          dry.push('**候选（前 8 条）**')
          cands.slice(0, 8).forEach(function (f) {
            const nm = String(f.path || '').split(/[\\/]/).pop()
            const bits = [f.year ? String(f.year) : null, f.status || null].filter(Boolean).join(' · ')
            dry.push('· ' + nm + (bits.length > 0 ? ' · ' + bits : ''))
          })
          if (n > cands.length) dry.push('…另 ' + (n - cands.length) + ' 篇')
        }
        dry.push('')
        dry.push('去掉 dry_run 即执行真实迁移；大批量会自动转后台，用 kb_status 轮询。')
        return [{ type: 'text', text: dry.join('\n') }]
      }
      const totals = value.totals || {}
      const files = Array.isArray(value.files) ? value.files : []
      const lines = []
      lines.push('**入库完成** · 新增 ' + (totals.added || 0) + ' / 更新 ' + (totals.updated || 0)
        + ' / 跳过 ' + (totals.skipped || 0) + ' / 重复 ' + (totals.duplicates || 0)
        + ' / 失败 ' + (totals.errors || 0))
      const totalMs = typeof value.ms === 'number' ? value.ms : 0
      lines.push('总耗时 ' + (totalMs >= 1000 ? (totalMs / 1000).toFixed(1) + 's' : totalMs + 'ms')
        + (value.embedding ? ' · ' + value.embedding : '')
        + (typeof totals.chunks === 'number' ? ' · ' + totals.chunks + ' 块 / ' + (totals.vectors || 0) + ' 向量' : ''))
      // 「N 块 / 0 向量」必须给原因：以前只有 embedding=null，用户看不出向量根本没建（issue #2）
      if (value.embedding_error) {
        lines.push('⚠ 未建向量（嵌入模型不可用：' + String(value.embedding_error).slice(0, 160)
          + '）—— 本次只建了关键词索引，检索会降级为纯关键词。修好环境后重跑 kb_ingest 即可补齐缺失向量（无需全量重建）。')
      }
      if (files.length > 0) {
        lines.push('')
        lines.push('**最近入库（滚动）**')
        const tail = files.slice(-8).reverse()
        tail.forEach(function (f) {
          const name = String(f.path || '').split(/[\\/]/).pop()
          const icon = f.status === 'added' ? '✓' : (f.status === 'skipped' ? '·' : (f.status === 'duplicate' ? '≈' : (f.status === 'error' || f.status === 'missing' ? '✗' : '·')))
          const ms = typeof f.ms === 'number' ? f.ms : 0
          // 失败/缺失要给出原因：只显示"✗ 文件"会让用户完全不知道下一步该做什么
          const why = (f.status === 'error' || f.status === 'missing')
            ? (f.error ? ' · ' + String(f.error).slice(0, 160) : '')
            : (f.note && f.status === 'changed' ? ' · ' + String(f.note).slice(0, 120) : '')
          lines.push(icon + ' ' + name + ' · ' + ms + 'ms' + why)
        })
        const totalN = typeof value.files_total === 'number' ? value.files_total : files.length
        if (files.length > tail.length) lines.push('（共 ' + totalN + ' 个文件，仅显示最近 ' + tail.length + ' 条；完整统计见 kb_stats）')
      }
      if (value.note) lines.push(String(value.note))
      return [{ type: 'text', text: lines.join('\n') }]
    }

    const renderStats = (_args, value) => {
      if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
      const lines = []
      lines.push('**知识库统计** · ' + (value.docs || 0) + ' 文档 / ' + (value.chunks || 0) + ' 块 / ' + (value.vectors || 0) + ' 向量')
      if (value.db) lines.push('数据库：' + value.db)
      // 向量链路状态：缺失向量是"检索退化成纯关键词"的直接信号，以前完全没渲染（issue #2）
      if (typeof value.embedding === 'string' && value.embedding.length > 0) lines.push('嵌入模型：' + value.embedding)
      if (value.embedding_error) lines.push('⚠ 嵌入模型加载失败：' + String(value.embedding_error).slice(0, 160))
      // 计算设备：装了 GPU 却没吃上、或 OOM 已回退 CPU —— 这两种情况以前完全看不出来
      const dev = value.device !== null && typeof value.device === 'object' ? value.device : null
      if (dev !== null) {
        const dbits = []
        if (dev.embed_device) dbits.push('嵌入=' + dev.embed_device)
        if (dev.rerank_device) dbits.push('精排=' + dev.rerank_device)
        if (dev.gpu) dbits.push(dev.gpu)
        if (dev.cuda_available === false) dbits.push('CUDA 不可用（torch ' + (dev.torch || '?') + '）→ 装 CUDA 版 torch 可加速')
        else if (dev.cuda_available === true && !dev.embed_device) dbits.push('CUDA 可用（模型未加载）')
        if (dev.note) dbits.push(dev.note)
        if (dbits.length > 0) lines.push('计算设备：' + dbits.join(' · '))
      }
      const health = value.health && typeof value.health === 'object' ? value.health : null
      if (health !== null && (Number(health.missing_vecs) > 0 || Number(health.orphan_chunks) > 0)) {
        lines.push('⚠ 索引不完整：缺失向量 ' + (health.missing_vecs || 0) + ' 块 · 孤儿分块 ' + (health.orphan_chunks || 0)
          + ' —— 缺失向量的分块不参与向量检索；重跑 kb_ingest 可补齐，或用 kb_ingest(rebuild=true) 全量重灌。')
      }
      // 整篇不可检索：这些文档在搜索里等于不存在，以前完全没有信号（References 判定误吞正文的后果）
      if (health !== null && Number(health.docs_without_retrievable_chunks) > 0) {
        const bsample = Array.isArray(health.blind_sample) && health.blind_sample.length > 0
          ? '（示例：' + health.blind_sample.slice(0, 3).join('、') + '）' : ''
        lines.push('⚠ ' + health.docs_without_retrievable_chunks + ' 篇文档没有任何可检索分块' + bsample
          + ' —— 这些文档**查不到**（通常是解析时整篇被误判为参考文献）；需 kb_ingest(rebuild=true) 全量重灌。')
      }
      const recent = Array.isArray(value.recent) ? value.recent : []
      if (recent.length > 0) {
        lines.push('')
        lines.push('**最近入库**')
        recent.slice(0, 10).forEach(function (r) {
          lines.push('- ' + String(r.file || '').split(/[\\/]/).pop() + ' · ' + (r.year || '-') + ' · ' + (r.chunks || 0) + ' 块')
        })
        if (recent.length > 10) lines.push('（共 ' + recent.length + ' 条，仅显示最近 10 条）')
      }
      return [{ type: 'text', text: lines.join('\n') }]
    }

    // 后台任务轮询：running 给进度，done 给 totals + 最近文件，error/not_found 说明原因。
    const renderStatus = (_args, value) => {
      if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
      const jobId = value.job_id ? String(value.job_id) : ''
      const status = value.status ? String(value.status) : 'unknown'
      const head = (title) => title + (jobId.length > 0 ? ' · job_id ' + jobId : '')
      const lines = []
      if (status === 'running') {
        lines.push(head('**后台任务进行中**'))
        const p = value.progress && typeof value.progress === 'object' ? value.progress : {}
        lines.push('已处理 ' + (p.processed || 0) + ' 篇 · 错误 ' + (p.errors || 0) + ' · 分块 ' + (p.chunks || 0))
        if (typeof value.note === 'string' && value.note.length > 0) lines.push(String(value.note))
        return [{ type: 'text', text: lines.join('\n') }]
      }
      if (status === 'done') {
        lines.push(head('**后台任务完成**'))
        const result = value.result && typeof value.result === 'object' ? value.result : {}
        const totals = result.totals && typeof result.totals === 'object' ? result.totals : {}
        const totLabels = [['added', '新增'], ['updated', '更新'], ['skipped', '跳过'], ['errors', '失败'], ['duplicates', '重复'], ['chunks', '分块'], ['vectors', '向量']]
        const parts = []
        totLabels.forEach(function (kv) {
          const n = totals[kv[0]] || 0
          if (n) parts.push(kv[1] + ' ' + n)
        })
        lines.push(parts.length > 0 ? parts.join(' · ') : '无变化（统计见 kb_stats）')
        const files = Array.isArray(result.files) ? result.files : []
        if (files.length > 0) {
          lines.push('')
          lines.push('**最近处理**')
          files.slice(-5).reverse().forEach(function (f) {
            lines.push('- ' + String(f.path || '').split(/[\\/]/).pop() + (f.status ? ' · ' + String(f.status) : ''))
          })
          const totalN = typeof result.files_total === 'number' ? result.files_total : files.length
          if (totalN > files.length) lines.push('（共 ' + totalN + ' 个文件，仅显示最近 ' + files.length + ' 条）')
        }
        return [{ type: 'text', text: lines.join('\n') }]
      }
      if (status === 'error') {
        lines.push(head('**后台任务失败**'))
        const result = value.result && typeof value.result === 'object' ? value.result : {}
        const err = value.error || result.error
        lines.push(err ? String(err) : '引擎未返回错误详情，请检查宿主日志后再重试。')
        return [{ type: 'text', text: lines.join('\n') }]
      }
      lines.push(head('**未找到该任务**'))
      lines.push('job_id 未知，或任务记录已被清理（任务完成后结果文件保留，kb_clear 会一并清空）。')
      if (typeof value.note === 'string' && value.note.length > 0) lines.push(String(value.note))
      return [{ type: 'text', text: lines.join('\n') }]
    }

    const renderFetch = (_args, value) => {
      if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
      const lines = []
      lines.push('**下载完成** · ' + (value.downloaded || 0) + ' / ' + (value.total || 0) + ' 篇')
      if (value.network) {
        const envLabel = value.network.env === 'campus' ? '校园网/机构网络' : value.network.env === 'home' ? '家庭网络' : '未确认'
        lines.push('网络环境：' + envLabel)
        const p = value.network.proxy || {}
        const proxyMsgs = []
        if (Array.isArray(p.env) && p.env.length) proxyMsgs.push('环境变量代理')
        if (Array.isArray(p.localPorts) && p.localPorts.length) proxyMsgs.push('本机代理端口:' + p.localPorts.join(','))
        if (p.system === true) proxyMsgs.push('系统代理')
        if (proxyMsgs.length) lines.push('注意：检测到代理(' + proxyMsgs.join('; ') + ')——代理可能干扰下载(TLS/反爬)，如失败请关闭代理后重试')
      }
      if (value.target) lines.push('保存到：' + value.target)
      const files = Array.isArray(value.files) ? value.files : []
      const fails = []
      files.forEach(function (f) {
        const name = f.path ? String(f.path).split(/[\\/]/).pop() : String(f.id || '')
        if (f.status === 'downloaded') {
          lines.push('✓ ' + name)
        } else {
          fails.push(f)
          lines.push('✗ ' + name + (f.error ? ' · ' + String(f.error).slice(0, 200) : ''))
        }
      })
      if (fails.length > 0) {
        lines.push('')
        lines.push('**未能自动下载 ' + fails.length + ' 篇** —— 失败原因已标注在上方（含打开链接），请在浏览器中打开对应 DOI 手动下载，再用 kb_ingest 入库（或 Zotero 抓取后同步）')
      }
      if (value.note) { lines.push(''); lines.push(String(value.note)) }
      return [{ type: 'text', text: lines.join('\n') }]
    }

    const renderSources = (_args, value) => {
      if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
      const items = Array.isArray(value.evidence) ? value.evidence : (Array.isArray(value.results) ? value.results : [])
      // 相关性地板判为"无关"时：结果为空是**结论**而非故障 —— 必须把理由与"库内最接近的几篇"
      // 一起说出来，否则 agent 会以为检索失败、换词穷举。
      if (items.length === 0 && value.no_hit === true) {
        const nlines = []
        nlines.push('**库内无相关资料**（精排最高分 ' + (value.max_score === null || value.max_score === undefined ? '?' : value.max_score)
          + ' < 地板 ' + (value.floor === null || value.floor === undefined ? '?' : value.floor) + '）')
        nlines.push('请如实说明库里没有相关资料，并按 scope 设置转 web_search；不要换词反复重试。')
        const close = Array.isArray(value.closest) ? value.closest : []
        if (close.length > 0) {
          nlines.push('')
          nlines.push('库内最接近的 ' + close.length + ' 篇（供你判断是否真的无关，不要当答案引用）：')
          close.forEach(function (c, i) {
            nlines.push('  ' + (i + 1) + '. ' + String(c.title || '(无标题)') + (c.year ? ' · ' + c.year : '')
              + (c.section ? ' · §' + c.section : ''))
          })
        }
        return [{ type: 'text', text: nlines.join('\n') }]
      }
      if (items.length === 0) return [{ type: 'text', text: JSON.stringify(value) }]
      const refRange = function (cs) {
        const ns = cs.map(function (c) { return c && c.n }).filter(function (n) { return n !== null && n !== undefined }).map(Number).sort(function (a, b) { return a - b })
        const parts = []
        let start = null, prev = null
        ns.forEach(function (x) {
          if (start === null) { start = prev = x }
          else if (x === prev + 1) { prev = x }
          else { parts.push(start === prev ? String(start) : start + '–' + prev); start = prev = x }
        })
        if (start !== null) parts.push(start === prev ? String(start) : start + '–' + prev)
        return parts.join(', ')
      }
      // score 仅在精排后显示（bge 余弦相似度可校准；RRF 融合分无绝对含义，显示反而误导）
      const scoreNote = value.reranker ? ' · score ' : ''
      const quick = value.depth === 'quick'
      const lines = []
      lines.push('**知识库来源 Top-' + items.length + '**' + (quick ? '（快速检索）' : (value.depth === 'deep' ? '（深度检索）' : '')))
      // 实际使用的检索路径（引擎会因 mode 参数或向量不可用而降级）：写死"混合检索"会误导
      const MODE_LABEL = { hybrid: '混合检索', keyword: '关键词检索', vector: '向量检索' }
      lines.push((MODE_LABEL[value.mode_used] || '混合检索') + (value.reranker ? ' · 精排 ' + value.reranker.split(' ')[0] : '') + (value.cached === true ? ' · 缓存命中' : '') + (typeof value.ms === 'number' ? ' · ' + value.ms + 'ms' : '') + (value.strict === true ? ' · 严格模式' : '') + (value.dup_collapsed > 0 ? ' · 已折叠 ' + value.dup_collapsed + ' 份同论文副本' : ''))
      // 引擎的语言提示（中文查询 + 几乎全英文库）：原样转达，提醒用英文术语重查
      if (typeof value.lang_note === 'string' && value.lang_note.length > 0) {
        lines.push('提示：' + value.lang_note)
      }
      // 引擎的降级原因（如"向量索引缺失，降级为纯关键词"）以前被丢弃，用户不知道检索为何变成关键词（issue #2）
      if (typeof value.note === 'string' && value.note.indexOf('降级') >= 0) {
        lines.push('提示：' + String(value.note).replace(/命中 \d+ 块，返回 Top-\d+/, '').trim())
      }
      // 弱相关：给一次明确的升级机会（深查），而不是让 agent 自由发挥式重试
      if (value.verdict === '弱相关') {
        lines.push('提示：本次结果相关性偏弱（精排最高分 ' + (value.max_score === undefined ? '?' : value.max_score)
          + ' < ' + (value.floor_weak === undefined ? '0.35' : value.floor_weak) + '）。最多再升一次 depth=deep；仍弱就按「库内无资料」处理。')
      }
      items.forEach(function (r, i) {
        const title = String(r.title || r.file || '')
        const doi = typeof r.doi === 'string' && r.doi.length > 0 ? r.doi : null
        const t = doi !== null ? '[' + title + '](https://doi.org/' + doi + ')' : title
        const rest = [
          typeof r.authors === 'string' && r.authors.length > 0 ? String(r.authors).split(';').map(function (s) { return s.trim() }).filter(Boolean).slice(0, 3).join('; ') : null,
          r.year,
          r.journal,
          r.section ? ('§' + r.section) : null,
        ].filter(Boolean).join(' · ')
        lines.push('')
        lines.push((i + 1) + '. ' + t + (rest.length > 0 ? ' — ' + rest : ''))
        lines.push('> ' + String(r.snippet || '').slice(0, quick ? 200 : 280).replace(/\n/g, ' '))
        if (quick) {
          // 快速检索：不带图注/引文链/搜索串；无 DOI 时补文件名供引用
          if (doi === null && r.file) lines.push('无 DOI · 文件：' + String(r.file))
          return
        }
        if (typeof r.figure === 'string' && r.figure.length > 0) {
          lines.push('↳ 图注坐标: ' + String(r.figure).slice(0, 220))
        }
        // 引文关联：本证据的参考文献条目；库内命中（[库内]）优先展示，未命中折叠到汇总行
        if (Array.isArray(r.citations) && r.citations.length > 0) {
          const hits = r.citations.filter(function (c) { return c && c.lib && typeof c.lib === 'object' })
          const others = r.citations.filter(function (c) { return !(c && c.lib && typeof c.lib === 'object') })
          lines.push(hits.length > 0
            ? '↳ 引文补充（本证据的参考文献；[库内]=已在库内，可检索引用）'
            : '↳ 引文补充（本证据的参考文献，供补库/深读）')
          hits.slice(0, 5).concat(others.slice(0, 3)).forEach(function (c) {
            lines.push('  · [Ref ' + c.n + '] ' + String(c.text || '').slice(0, 150))
            if (c.lib) {
              const ldoi = typeof c.lib.doi === 'string' && c.lib.doi.length > 0 ? c.lib.doi : null
              const lt = ldoi !== null ? '[' + String(c.lib.title || '') + '](https://doi.org/' + ldoi + ')' : String(c.lib.title || '')
              const lmeta = [
                typeof c.lib.authors === 'string' && c.lib.authors.length > 0 ? String(c.lib.authors).split(';').map(function (s) { return s.trim() }).filter(Boolean).slice(0, 2).join('; ') : null,
                c.lib.year,
                c.lib.journal,
              ].filter(Boolean).join(' · ')
              let tail = '（即本证据的 Ref ' + c.n + '，可检索引用）'
              if (typeof c.lib.zotero_key === 'string' && c.lib.zotero_key.length > 0) {
                tail += ' · [Zotero 打开](zotero://open-pdf/library/items/' + c.lib.zotero_key + ')'
              }
              lines.push('    [库内] ' + lt + (lmeta.length > 0 ? '（' + lmeta + '）' : '') + tail)
            }
          })
          const rest = hits.slice(5).concat(others.slice(3))
          if (rest.length > 0) {
            lines.push('  ↳ 另有 ' + rest.length + ' 条引文未展开（Ref ' + refRange(rest) + '），补库时可按编号定位')
          }
        }
        if (doi !== null) {
          lines.push('[DOI ' + doi + '](https://doi.org/' + doi + ')' + (scoreNote !== '' ? scoreNote + r.score : ''))
        } else {
          lines.push('无 DOI' + (scoreNote !== '' ? scoreNote + r.score : '') + ' · 文件：' + String(r.file || ''))
          if (typeof r.search === 'string' && r.search.length > 0) {
            lines.push('↳ 搜索串（Scholar 可复制）: ' + String(r.search).slice(0, 200))
          }
        }
        if (typeof r.path === 'string' && r.path.length > 0) {
          lines.push(r.path)
        }
        if (typeof r.zotero_key === 'string' && r.zotero_key.length > 0) {
          lines.push('[在 Zotero 中打开 PDF](zotero://open-pdf/library/items/' + r.zotero_key + ')')
        }
      })
      if (quick) {
        lines.push('')
        lines.push('（快速检索：直接输出查到的信息即可，一两句话答完，无需展开分析；需要深入背景时用 depth=deep 重查）')
        return [{ type: 'text', text: lines.join('\n') }]
      }
      if (Array.isArray(value.related) && value.related.length > 0) {
        lines.push('')
        lines.push('**关联文献（可作补充建议）**')
        value.related.forEach(function (r) {
          const doi = typeof r.doi === 'string' && r.doi.length > 0 ? r.doi : null
          const t = doi !== null
            ? '[' + String(r.title || r.file || '') + '](https://doi.org/' + doi + ')'
            : String(r.title || r.file || '')
          const meta = [
            typeof r.authors === 'string' && r.authors.length > 0 ? String(r.authors).split(';').map(function (s) { return s.trim() }).filter(Boolean).slice(0, 2).join('; ') : null,
            r.year,
            r.journal,
          ].filter(Boolean).join(' · ')
          lines.push('- ' + t + (meta.length > 0 ? ' — ' + meta : '') + '（' + String(r.reason || '内容相关') + '）')
        })
      }
      // 结果层提示（镜像块，源：npm-package/lib/guidance.js）：
      // 无命中 / 弱相关 / 向量降级 / 已关闭 —— 静态插件那一半由 withNotes() 注入，
      // 这里手写调用同一个生成块，避免两半漂移。
      const notes = kbResultNotes(value, (value && value.__diligence) || 'normal')
      if (notes.length > 0) {
        lines.push('')
        notes.forEach(function (n) { lines.push(n) })
      }
      return [{ type: 'text', text: lines.join('\n') }]
    }

    const filterSchema = {
      type: 'object',
      additionalProperties: false,
      description: '可选元数据预过滤。',
      properties: {
        authors: { type: 'string', description: '作者子串匹配（如 Zhang）。' },
        title: { type: 'string', description: '标题子串匹配。' },
        journal: { type: 'string', description: '期刊子串匹配。注意：期刊字段目前只由 Zotero 迁移填充（publicationTitle/journalAbbreviation）；用 kb_ingest 建起来的库里该字段为 NULL，用它过滤通常零命中——想限定来源请改用 authors/year/title。' },
        kind: { type: 'string', description: '文件类型：pdf/txt/md/docx。' },
        section: { type: 'string', description: '章节子串匹配（如 Methods、Results、方法）。' },
        year: { oneOf: [{ type: 'integer', description: '精确年份（如 2024）。' }, { type: 'string', description: '年份比较式（如 ">=2020"）。' }], description: '年份过滤。' },
      },
    }

    const kbIngest = harness.defineTool({
      name: 'kb_ingest',
      description: '把本地文档（PDF/TXT/MD/DOCX）导入 DSH 知识库并建立索引（轻量 RAG 工作流的入库步骤）。支持单个文件或目录（递归扫描并只处理 PDF/TXT/MD/DOCX）；按章节切分并抽取元数据（标题/作者/年份/DOI）；同时用本地 bge-small 模型生成向量（数据持久化在工作区/.kb）。已入库且内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库时标记为 duplicate 跳过（增量）。paths 用工作区内的相对路径或绝对路径。入库后用 kb_search 检索、kb_rag 问答、kb_stats 看统计。重复调用安全。metadata_only=true 只刷新元数据（秒级，不重切块/不重嵌入，适合引擎升级后让老库的标题/作者/DOI 生效）；rebuild=true 原地重灌库内全部已入库文档（不会因传目录而重复入库）；大批量会自动转后台并返回 job_id，用 kb_status 轮询。',
      parameters: {
        // paths 与 rebuild 二选一：rebuild=true 时引擎按库内现有路径重灌，不需要 paths。
        // 不标 required，缺两项时由引擎给出明确错误（"paths is required（或用 rebuild=true…）"）。
        paths: { type: 'array', items: { type: 'string' }, description: '要入库的文件或目录路径列表；rebuild=true 时可省略。' },
        kb_root: { type: 'string', description: '知识库目录（默认：工作区下的 .kb）。' },
        force: { type: 'boolean', description: 'true 时强制重新解析并重新编码向量（默认 false）。' },
        metadata_only: { type: 'boolean', description: 'true 时只刷新元数据（重抽标题/作者/年份/期刊/DOI，秒级；不重切块、不重嵌入；内容已变的文件不动）。' },
        rebuild: { type: 'boolean', description: 'true 时原地重灌库内全部已入库文档（路径取自库内，可省略 paths）。大批量会自动转后台并返回 job_id。' },
      },
      output: { schema: { type: 'json' }, render: renderIngest },
      timeoutMs: 1800000,
      execute(args, exec) {
        return runEngine('ingest', {
          paths: args.paths,
          kb_root: kbRootOf(args, exec),
          force: args.force === true,
          metadata_only: args.metadata_only === true,
          rebuild: args.rebuild === true,
          async_if_large: true,
        }, exec)
      },
    })

    // 描述层纪律（镜像块生成）：与静态插件侧 guidedDescription 等价 —— host.js 不能 import
    // npm 包的模块，所以文本由 tools/sync-host-guidance.mjs 从 guidance.js 生成。
    function withDiscipline(spec) {
      return Object.assign({}, spec, {
        description: String(spec.description || '') + '\n\n' + kbDisciplineText('normal'),
      })
    }

    const kbSearch = harness.defineTool(withDiscipline({
      name: 'kb_search',
      description: '在知识库中做混合检索（关键词 BM25 + 向量余弦，RRF 融合，×章节权重），返回最相关片段及精确来源（文件/标题/作者/年份/期刊/DOI/章节）。想在已入库文档中查找事实、数据或术语时优先于直接读文件（更省 token）。depth 双模式：quick（默认）=快速检索，混合召回直出、跳过精排与引文扩展，亚秒级响应，适合事实性查询；工具返回后立即作答，不展开背景与延伸分析；deep=深度检索，bge-reranker 精排 + 引文链 + 关联文献（适合领域调研与综述性问题）。query 用**英文术语串**——库内正文以英文为主，中文问句会让 BM25 关键词路空转、只靠向量侧跨语言匹配，命中明显更差；写法为 3–12 个词，结构「材料/体系 + 方法/工艺 + 性质/表征」（如 "graphene CVD copper single crystal nucleation suppression"），不要用整句问句，年份/期刊/作者请放 filters，需要中文文献时用用户原话另发一条中文查询；引擎按原样检索，不会替你翻译；mode 可选 keyword/vector/hybrid（默认 hybrid）；filters 支持 authors/year/section/title/journal/kind 元数据预过滤（year 可用 ">=2020" 形式）；其中 journal 目前只由 Zotero 迁移填充，kb_ingest 入库的文档该字段为 NULL，用它过滤通常零命中。查询范围由会话开始时的范围询问或 kb_scope 工具控制；返回的 scope/scope_note 指明当前范围。strict 可选（true=严格模式：答案仅基于本次结果，禁止库外知识/常识外延；默认继承 kb_scope 设置）。回答用户时必须标注来源：引用要写成 markdown 链接格式 [作者, 年份, 期刊](https://doi.org/DOI)（用来源字段里的 doi，保证用户能点击打开）；若该来源无 DOI，引用写成 [作者, 年份, 文件名]（方括号内只放 PDF 文件名，不要使用任何 HTML 标签；文件名过长时可截断到约 60 字符）。无命中时先检查是否已入库（kb_stats）。相同查询命中缓存，零重计算。',
      parameters: {
        query: { type: 'string', required: true, description: '检索词，**英文优先**：3–12 个英文术语，结构「材料/体系 + 方法/工艺 + 性质/表征」（如 "graphene CVD copper single crystal nucleation suppression"）；限定条件放 filters；引擎按原样检索、不翻译。需要中文文献时用中文原话另发一条查询。' },
        depth: { type: 'string', enum: ['quick', 'deep'], description: 'quick=快速检索（默认：无精排/引文链/关联文献，响应最快，适合查个信息）；deep=深度检索（精排+引文链+关联文献，适合领域调研与综述性问题）。默认继承 kb_scope 的会话 depth 设置。' },
        top_k: { type: 'integer', description: '返回结果数（默认 quick 3 / deep 5，上限 10）。' },
        snippet: { type: 'integer', description: '片段长度字符数（默认 quick 300 / deep 400）。' },
        mode: { type: 'string', enum: ['keyword', 'vector', 'hybrid'], description: '检索模式（默认 hybrid）。' },
        rerank: { type: 'boolean', description: '是否启用 bge-reranker-base 精排（默认 quick 关 / deep 开）。' },
        related: { type: 'boolean', description: 'true 时附带 related 关联文献列表（默认 quick 关 / deep 开，供补充建议引用）。' },
        strict: { type: 'boolean', description: '严格模式：true 时答案仅基于本次检索结果，禁止补充库外知识/常识外延（默认继承 kb_scope 的 strict 设置）。' },
        kb_root: { type: 'string', description: '知识库目录（默认：工作区下的 .kb）。' },
        filters: filterSchema,
      },
      output: { schema: { type: 'json' }, render: renderSources },
      execute(args, exec) {
        const strict = args.strict === undefined ? scopeStrict : args.strict === true
        const call = runEngine('search', {
          query: args.query,
          depth: args.depth === undefined ? scopeDepth : args.depth,
          top_k: args.top_k,
          snippet: args.snippet,
          mode: args.mode,
          rerank: args.rerank,
          related: args.related,
          filters: args.filters,
          kb_root: kbRootOf(args, exec),
        }, exec)
        return scopeWrapped(exec, call, strict, kbRootOf(args, exec))
      },
    }))

    const kbRag = harness.defineTool(withDiscipline({
      name: 'kb_rag',
      description: '在知识库中检索证据片段供当前模型直接作答：基于 evidence 回答问题，每个事实后标注引用编号 [n]（对应 evidence 下标）。引用一定要写成可点击的 markdown 链接：[作者, 年份, 期刊](https://doi.org/DOI)（用 evidence 条目的 doi 字段）；若 doi 为 null，引用写成 [作者, 年份, 文件名]（方括号内只放 PDF 文件名，不要使用任何 HTML 标签；文件名过长时可截断到约 60 字符）。depth 双模式：deep（默认）=深度检索，重排序 + 引文关联 + 相关文献全链路，回答可综合多篇展开论述（适合领域调研）；quick=快速检索，仅基于少量证据直接作答，不展开论述。strict 可选（true=严格模式：仅基于 evidence 作答，禁止补充库外知识/常识外延或未出现在 evidence 中的文献数据，证据不足直接说明无法回答；默认继承 kb_scope 设置，当前默认 false）。资料不足时明确回答"根据现有资料无法回答"；多源冲突时分别列出并说明来源。答案末尾的补充建议按来源分三列（哪列为空就整列省略）：①「库内可查（循引文找到）」——citations 里标 [库内] 的文献，必须写出关系链"《被引文献》(作者, 年份) 被 [证据编号] 的引文 Ref n 引用，已在库内可直接提问"；②「建议补库（循引文发现）」——citations 未命中库内的条目，注明被 Ref n 引用、尚不在库内，可用 Ref 编号定位下载；③「相关文献」——related 列表（同作者/同期刊/主题相似的库内文献，元数据相似）。每条推荐的理由必须写明属于哪种，引文关联的必须带关系链，不得混列；若库内缺少关键资料，明确指出应补充哪些文献/主题（用户重视此提示）。这是知识库 RAG 问答的唯一入口；查询范围由会话开始时的范围询问或 kb_scope 工具控制。',
      parameters: {
        query: { type: 'string', required: true, description: '要回答的问题——请先把它转写成**英文检索词**再传入（3–12 词，术语优先，不要整句中文问句）：库内正文以英文为主，引擎按原样检索、不替你翻译。' },
        depth: { type: 'string', enum: ['quick', 'deep'], description: 'deep=深度检索（默认：精排+引文链+关联文献，回答展开背景，适合不熟悉领域）；quick=快速检索（少量证据直接给答案，不展开）。默认继承 kb_scope 的会话 depth 设置。' },
        top_k: { type: 'integer', description: '证据条数（默认 quick 2 / deep 3，上限 10）。' },
        rerank: { type: 'boolean', description: '是否启用精排（默认 quick 关 / deep 开）。' },
        related: { type: 'boolean', description: 'true 时附带 related 关联文献列表供补充建议引用（默认 quick 关 / deep 开）。' },
        strict: { type: 'boolean', description: '严格模式：true 时仅基于 evidence 作答，禁止库外知识补充（默认继承 kb_scope 的 strict 设置）。' },
        kb_root: { type: 'string', description: '知识库目录（默认：工作区下的 .kb）。' },
        filters: filterSchema,
      },
      output: { schema: { type: 'json' }, render: renderSources },
      execute(args, exec) {
        const strict = args.strict === undefined ? scopeStrict : args.strict === true
        const call = runEngine('rag', {
          query: args.query,
          depth: args.depth === undefined ? scopeDepth : args.depth,
          top_k: args.top_k,
          rerank: args.rerank,
          related: args.related,
          filters: args.filters,
          kb_root: kbRootOf(args, exec),
        }, exec)
        return scopeWrapped(exec, call, strict, kbRootOf(args, exec))
      },
    }))

    const kbZotero = harness.defineTool({
      name: 'kb_zotero',
      description: '把本地 Zotero 文献库中带 PDF 附件的文献批量迁移到知识库（轻量 RAG 工作流的 Zotero 接口）。读取 zotero.sqlite（默认自动定位 ~/Zotero、~/Documents/Zotero、%APPDATA% 配置；找不到时用 zotero_db 显式指定），解析每篇文献的元数据（标题/作者/年份/期刊/DOI）与 PDF 附件路径（storage 目录），逐篇解析入库并生成向量；已入库附件自动跳过，重复内容标记 duplicate 跳过（增量，可反复运行）。附件文件本体缺失的条目标记为 missing 并跳过（不尝试下载）。dry_run=true 时只列候选不写入；limit 限制迁移条数。',
      parameters: {
        zotero_db: { type: 'string', description: 'zotero.sqlite 显式路径（默认自动定位）。' },
        kb_root: { type: 'string', description: '知识库目录（默认：工作区下的 .kb）。' },
        limit: { type: 'integer', description: '迁移条数上限（默认全部）。' },
        force: { type: 'boolean', description: 'true 时强制重新解析已入库附件（默认 false）。' },
        dry_run: { type: 'boolean', description: 'true 时只列候选文献，不导入（默认 false）。' },
      },
      output: { schema: { type: 'json' }, render: renderIngest },
      timeoutMs: 1800000,
      execute(args, exec) {
        return runEngine('zotero', {
          zotero_db: args.zotero_db,
          kb_root: kbRootOf(args, exec),
          limit: args.limit,
          force: args.force === true,
          dry_run: args.dry_run === true,
        }, exec)
      },
    })

    const kbDedup = harness.defineTool({
      name: 'kb_dedup',
      description: '清理知识库中的重复文档：删除 sha256 与早期文档相同的后来入库项（保留最早 id）并同步清除其分块/向量/缓存。返回 removed 与当前总数。反复调用安全。',
      parameters: {
        kb_root: { type: 'string', description: '知识库目录（默认：工作区下的 .kb）。' },
      },
      output: { schema: { type: 'json' }, render: renderJson },
      execute(args, exec) {
        return runEngine('dedup', { kb_root: kbRootOf(args, exec) }, exec)
      },
    })

    const kbClear = harness.defineTool({
      name: 'kb_clear',
      description: '清空知识库中的全部文献与索引（文档/分块/向量/缓存全部删除，不可恢复；数据库文件保留结构）。必须显式传 confirm: true 才会执行（否则拒绝）。清空后可重新 kb_ingest 或 kb_zotero 重建。',
      parameters: {
        kb_root: { type: 'string', description: '知识库目录（默认：工作区下的 .kb）。' },
        confirm: { type: 'boolean', required: true, description: '必须显式传 true 确认清空全部文献。' },
      },
      output: { schema: { type: 'json' }, render: renderJson },
      execute(args, exec) {
        return runEngine('clear', { kb_root: kbRootOf(args, exec), confirm: args.confirm === true }, exec)
      },
    })

    const kbFetch = harness.defineTool({
      name: 'kb_fetch',
      description: '按 DOI / arXiv ID 把论文 PDF 下载到本地目录（默认 ~/.kb-rag/downloads，可用 target_dir 覆盖）。按标准元标签与公开 API 解析地址，顺序为：arXiv 直连 → 出版商正式版（落地页 citation_pdf_url；在校园网/机构订阅网络下可直接取得订阅版 PDF，无需额外配置）→ 落地页内常见 pdf 链接 → 开放获取兜底（Unpaywall / Crossref）。只做常规抓取，不绕过付费墙、不访问 Sci-Hub、不伪造凭据。下载后不会自动进 Zotero——需用户手动在 Zotero 里「文件→添加文件」或拖入该目录 PDF 入库。',
      parameters: {
        identifiers: { type: 'array', required: true, items: { type: 'string' }, description: 'DOI 或 arXiv ID 列表（如 10.5555/12345679 或 arXiv:2401.00001）。' },
        target_dir: { type: 'string', description: '下载目录（默认 ~/.kb-rag/downloads）。' },
      },
      output: { schema: { type: 'json' }, render: renderFetch },
      timeoutMs: 300000,
      execute(args, exec) {
        askNetworkOnce(exec && exec.agent)
        const network = { env: netEnv, proxy: { env: envProxyDetect() } }
        return runEngine('fetch', { identifiers: args.identifiers, target_dir: args.target_dir, network: network }, exec)
      },
    })

    const kbScope = harness.defineTool({
      name: 'kb_scope',
      description: '设置/查看知识库查询范围、回答深度与严格模式（会话开始时也会询问一次范围）：scope：kb=仅封闭知识库；both=知识库+全网（kb 检索 + web_search 补充）；web=仅全网。depth 可选：quick=快速检索（亚秒级响应，直出结果）；deep=深度检索（重排序+引文关联全链路，跨文献综合论述）。strict 可选：true=严格模式（答案仅基于库内证据，禁止库外知识/常识外延）；false=关闭（默认 false）。用户说"封闭库/全网/都要/严格只按库内/快速检索/深度检索"等要求时，调本工具设定后再检索。',
      parameters: {
        scope: { type: 'string', enum: ['kb', 'both', 'web'], description: 'kb=仅封闭库；both=知识库+全网；web=仅全网。不传则只查看当前设置。' },
        depth: { type: 'string', enum: ['quick', 'deep'], description: '可选：同时设置检索深度。quick=快速检索；deep=深度检索。' },
        strict: { type: 'boolean', description: '可选：同时设置严格模式。true=仅基于库内证据作答；false=关闭（默认）。' },
      },
      output: { schema: { type: 'json' }, render: renderJson },
      execute(args, exec) {
        // scope 是可选的：不传时本工具只返回当前会话设置（描述里承诺了"设置/查看"）
        if (args.scope !== undefined) scopePref = args.scope
        if (args.depth !== undefined) scopeDepth = args.depth === 'deep' ? 'deep' : 'quick'
        if (args.strict !== undefined) scopeStrict = args.strict === true
        console.log('[kb-rag] query scope:', scopePref, 'depth:', scopeDepth, 'strict:', scopeStrict)
        return Promise.resolve({ ok: true, scope: scopePref, depth: scopeDepth, strict: scopeStrict, scope_note: SCOPE_NOTE[scopePref], depth_note: scopeDepth === 'quick' ? '快速检索：kb_search 默认快速检索，kb_rag 可显式 depth=quick' : '深度检索：kb_search/kb_rag 均默认深度检索', strict_note: scopeStrict ? STRICT_NOTE : undefined })
      },
    })

    const kbStats = harness.defineTool({
      name: 'kb_stats',
      description: '查看知识库统计：文档数、分块数、向量数、最近入库列表及数据库位置。用于检查哪些文档已入库、索引状态；检索无命中时先调它确认库里有什么。',
      parameters: {
        kb_root: { type: 'string', description: '知识库目录（默认：工作区下的 .kb）。' },
      },
      output: { schema: { type: 'json' }, render: renderStats },
      execute(args, exec) {
        return runEngine('stats', { kb_root: kbRootOf(args, exec) }, exec)
      },
    })

    const kbStatus = harness.defineTool({
      name: 'kb_status',
      description: '查询后台任务进度或结果。大批量入库（kb_ingest）会自动转后台并返回 job_id，用本工具轮询：running 时给出已处理篇数/错误数/分块数，done 时给出 totals 与最近 20 条文件，error/not_found 时说明原因。宿主调用超时不会中断后台任务。',
      parameters: {
        job_id: { type: 'string', required: true, description: '后台任务 id（kb_ingest 转后台时返回的 job_id，12 位十六进制）。' },
        kb_root: { type: 'string', description: '知识库目录（默认：工作区下的 .kb）。' },
      },
      output: { schema: { type: 'json' }, render: renderStatus },
      execute(args, exec) {
        return runEngine('status', { job_id: args.job_id, kb_root: kbRootOf(args, exec) }, exec)
      },
    })

    ctx.effect(() => harness.registerTool(ctx, kbIngest))
    ctx.effect(() => harness.registerTool(ctx, kbSearch))
    ctx.effect(() => harness.registerTool(ctx, kbRag))
    ctx.effect(() => harness.registerTool(ctx, kbZotero))
    ctx.effect(() => harness.registerTool(ctx, kbDedup))
    ctx.effect(() => harness.registerTool(ctx, kbClear))
    ctx.effect(() => harness.registerTool(ctx, kbFetch))
    ctx.effect(() => harness.registerTool(ctx, kbScope))
    ctx.effect(() => harness.registerTool(ctx, kbStats))
    ctx.effect(() => harness.registerTool(ctx, kbStatus))
    console.log('[kb-rag] tools registered (v1.6.7): kb_ingest / kb_search / kb_rag / kb_zotero / kb_dedup / kb_clear / kb_fetch / kb_scope / kb_stats / kb_status')
  },
}

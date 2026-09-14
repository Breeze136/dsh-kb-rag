// kb-rag DSH dynamic plugin — Client half (v1.6.7)
// 用法：把本文件内容作为 cordis_define 的 code.client（纯函数体，直接粘贴）。
// 形态依据：沙箱把 `React` 作为闭包符号注入（`new Function('React', …)`），无 JSX、无 TS；
//          服务走 `ctx.get(...)`（可选查询）或 inject 声明后的属性访问。
//
// 提供两件事（与 npm 包那一半 lib/client.js 同一实现，只是模块形态不同）：
//   ① kb_search / kb_rag 的**来源卡片**（tool.call.toolview 插槽）：
//      优先用宿主给的 presentationMeta（结构化 sources/verdict/closest），
//      没有就退回解析结果文本里的 markdown 链接。
//   ② 会话标题栏的 **kb 指示条**（conversation.session.header.actions 插槽）：
//      显示 kb-rag 已启用，并把 /kb 可用命令写进 title（客户端读宿主实时状态需要额外通道，暂不臆造）。
// 不渲染卡片视图的界面不受影响——核心可点击来源由 Host 输出的 markdown 链接承担。
return {
  name: 'kb-rag-sources',
  inject: ['slots'],
  apply(ctx) {
    const slots = ctx.get('slots')
    if (slots === undefined) {
      console.error('[kb-rag] 客户端 slots 服务不可用：来源卡片与会话指示条未注册（宿主输出的来源链接不受影响）')
      return
    }

    const accent = 'var(--ds-color-accent, #4a9eff)'
    const muted = 'var(--ds-color-text-muted, #999)'
    const border = 'var(--ds-color-border, rgba(128,128,128,0.4))'
    const styles = {
      box: { margin: '8px 0', fontSize: 12, lineHeight: 1.5, fontFamily: 'inherit' },
      head: { fontWeight: 700, fontSize: 12, color: accent, marginBottom: 6 },
      meta: { color: muted, fontSize: 11, marginBottom: 4 },
      card: { border: '1px solid ' + border, borderLeft: '3px solid ' + accent, borderRadius: 6, padding: '8px 10px', marginBottom: 8, background: 'var(--ds-color-surface, transparent)' },
      src: { fontWeight: 600, fontSize: 12, wordBreak: 'break-word' },
      rest: { color: muted, fontSize: 11, marginTop: 2 },
      link: { color: accent, fontSize: 11, textDecoration: 'underline', cursor: 'pointer' },
      warn: { color: 'var(--ds-color-danger, #c33)', fontSize: 12 },
      empty: { color: muted, fontSize: 12 },
      chip: { fontSize: 11, padding: '2px 6px', borderRadius: 999, border: '1px solid ' + border, color: muted, cursor: 'default' },
    }

    // 结构化来源：宿主 output.presentationMeta 投影出来的 payload（顶层调用才有）
    function metaSources(block) {
      const meta = block && block.meta
      if (meta === null || typeof meta !== 'object') return null
      return Array.isArray(meta.sources) ? meta : null
    }

    // 退回路径：从结果文本里解析来源链接。宿主渲染的格式是「1. [标题](https://doi.org/...)」，
    // 旧写法按「[1] [标题](...)」匹配，**永远匹配不上** → 没有 presentationMeta 时卡片恒显示"无命中"。
    function textSources(block) {
      const content = Array.isArray(block && block.content) ? block.content : []
      const text = content.map(function (c) { return (c && c.type === 'text') ? String(c.text || '') : '' }).join('\n')
      const re = /(\d{1,3})\s*\.?\s*\[([^\]]{2,240})\]\(([^)\s]+)\)/g
      const items = []
      let m = null
      while ((m = re.exec(text)) !== null) items.push({ idx: m[1], title: m[2], href: m[3] })
      return { sources: items, text: text }
    }

    function SourceList(props) {
      const block = props && props.block
      if (block === undefined || block === null) return React.createElement('div', null)
      if (block.type === 'tool-call') {
        let query = ''
        try {
          const args = typeof block.arguments === 'string' ? JSON.parse(block.arguments || '{}') : (block.arguments || {})
          query = args.query || ''
        } catch (e) { /* ignore */ }
        return React.createElement('div', { style: styles.box },
          React.createElement('div', { style: styles.meta }, '检索中：' + String(query || '').slice(0, 120)))
      }
      if (block.type !== 'tool-result') return React.createElement('div', null)
      if (block.isError === true) {
        return React.createElement('div', { style: styles.warn }, '知识库检索失败')
      }
      const structured = metaSources(block)
      const parsed = structured === null ? textSources(block) : null
      const sources = []
      if (structured !== null) {
        structured.sources.forEach(function (s) {
          sources.push({
            idx: String(s.idx === undefined ? sources.length + 1 : s.idx),
            title: String(s.title || s.file || '(无标题)'),
            href: s.doi ? ('https://doi.org/' + s.doi) : null,
            rest: [s.authors ? String(s.authors).split(';')[0] : null, s.year,
                   s.section ? ('§' + s.section) : null].filter(Boolean).join(' · '),
          })
        })
      } else if (parsed !== null) {
        parsed.sources.forEach(function (s) {
          sources.push({ idx: s.idx, title: s.title, href: s.href, rest: null })
        })
      }

      const children = []
      const verdict = structured !== null ? structured.verdict : null
      const noHit = structured !== null ? structured.no_hit === true : null
      if (noHit === true) {
        children.push(React.createElement('div', { style: styles.warn },
          '库内无相关资料' + (structured !== null && structured.max_score !== null && structured.max_score !== undefined
            ? '（精排最高分 ' + structured.max_score + '）' : '')))
      }
      const closest = structured !== null && Array.isArray(structured.closest) ? structured.closest : []
      if (noHit === true && closest.length > 0) {
        children.push(React.createElement('div', { style: styles.meta }, '库内最接近：'))
        closest.slice(0, 5).forEach(function (c, i) {
          children.push(React.createElement('div', { key: 'c' + i, style: styles.rest },
            '· ' + String(c.title || '') + (c.year ? ' · ' + c.year : '')))
        })
      }
      sources.forEach(function (s) {
        children.push(React.createElement('div', { key: 's' + s.idx, style: styles.card },
          React.createElement('div', { style: styles.src },
            '[' + s.idx + '] ',
            s.href
              ? React.createElement('a', { href: s.href, target: '_blank', rel: 'noreferrer', style: styles.link }, s.title)
              : s.title),
          s.rest ? React.createElement('div', { style: styles.rest }, s.rest) : null))
      })
      if (verdict === '弱相关') {
        children.push(React.createElement('div', { style: styles.meta }, '相关性偏弱：建议升级 depth=deep 再查一次'))
      }
      if (children.length === 0) {
        children.push(React.createElement('div', { style: styles.empty }, '无命中'))
      }
      return React.createElement('div', { style: styles.box },
        React.createElement('div', { style: styles.head }, '知识库来源'),
        children)
    }

    // 会话标题栏指示条：显示本会话的**实时**状态（范围/深度/工具数）。
    //
    // 数据走 Package 私有通道 host.call ⇄ 宿主 harness.handle('kb-state')。
    // 该通道**只存在于动态沙箱**（harness 是沙箱全局）：npm 静态半边没有它，所以这里必须
    // 做能力检测并退化为静态 "kb" —— 同一份源码在两种形态下都能跑。
    // 退化时的字面量刻意做了区分，便于一眼看出卡在哪一步：
    //   "kb"                       通道不存在（静态半边，或宿主没注册 handler）
    //   "kb !"                     调用了但失败
    //   "kb · ?"                   通道通了但拿不到 sessionId（指示条没收到 props）
    //   "kb · kb · deep · 10/10"   正常
    function KbChip(props) {
      const sid = (props !== null && props !== undefined && typeof props.sessionId === 'string')
        ? props.sessionId : ''
      const [st, setSt] = React.useState(null)
      const [bad, setBad] = React.useState(false)
      const [busy, setBusy] = React.useState(false)
      const hasChannel = (typeof host !== 'undefined' && host !== null && typeof host.call === 'function')
      // 点击 → 唤起宿主侧与"会话开场"同一套询问（范围/深度/陈旧数据），
      // 返回值就是询问后的新状态，直接拿来刷新胶囊，不需要再拉一次。
      const onClick = function () {
        if (!hasChannel || busy === true) return
        setBusy(true)
        Promise.resolve(host.call('kb-menu', { sessionId: sid })).then(function (v) {
          if (v !== null && typeof v === 'object') { setSt(v); setBad(false) }
          setBusy(false)
        }, function () { setBad(true); setBusy(false) })
      }
      React.useEffect(function () {
        let alive = true
        if (!hasChannel) return function () { alive = false }
        Promise.resolve(host.call('kb-state', { sessionId: sid })).then(function (v) {
          if (!alive) return
          if (v !== null && typeof v === 'object') setSt(v)
          else setBad(true)
        }, function () { if (alive) setBad(true) })
        return function () { alive = false }
      }, [sid])

      let label = 'kb-rag'
      let detail = '（状态通道不可用）'
      if (bad) {
        label = 'kb-rag !'
        detail = '（取状态失败）'
      } else if (st !== null) {
        const tools = (typeof st.tools === 'number' ? st.tools : '?') + '/10'
        if (st.enabled === false) {
          // 关闭态必须仍然是"可点的入口"：文案直接写出动作，颜色也区分开
          label = 'kb-rag 已关闭 · 点击开启'
          detail = '已关闭（不检索、也不拉引擎）。点这个胶囊即可重新开启'
        } else if (sid === '') {
          label = 'kb-rag ?'
          detail = '（拿不到 sessionId，显示的是默认值）'
        } else {
          // 范围只在**非默认**时出现：默认的 kb 与产品名 kb-rag 语义重复，
          // 并列显示会让人以为"kb"是另一层东西（旧写法 `kb · kb · deep` 就是这么被误读的）。
          const scopePart = (st.scope === 'kb') ? '' : (String(st.scope) + ' ')
          label = 'kb-rag ' + scopePart + st.depth + ' ' + tools
          detail = '范围 ' + st.scope + ' · 深度 ' + st.depth + ' · 纪律 ' + st.diligence + ' · 工具 ' + tools
        }
      }
      const clickable = hasChannel
      // 关闭态用警示色，让"这里可以点回来"一眼可见
      const closed = (bad !== true && st !== null && st.enabled === false)
      const closedColor = 'var(--ds-color-danger, #c33)'
      const chipStyle = clickable
        ? { fontSize: 11, padding: '2px 6px', borderRadius: 999,
            border: '1px solid ' + (closed ? closedColor : border),
            color: closed ? closedColor : muted,
            cursor: 'pointer' }
        : styles.chip
      return React.createElement('div', {
        style: chipStyle,
        onClick: clickable ? onClick : undefined,
        title: 'kb-rag ' + detail
          + (clickable ? '。**点击可改范围/深度**' : '')
          + '。命令：/kb status · /kb kb|both|web · /kb quick|deep · '
          + '/kb thorough|normal · /kb off [hard|search] · /kb on · /kb policy',
      }, busy === true ? label + ' …' : label)
    }

    slots.inject('tool.call.toolview', () => slots.register(
      { name: 'tool.call.toolview', key: 'kb_rag' },
      (props) => React.createElement(SourceList, props),
    ))
    slots.inject('tool.call.toolview', () => slots.register(
      { name: 'tool.call.toolview', key: 'kb_search' },
      (props) => React.createElement(SourceList, props),
    ))
    slots.inject('conversation.session.header.actions', () => slots.register(
      { name: 'conversation.session.header.actions', id: 'kb-rag-chip', order: 90, label: 'kb' },
      (props) => React.createElement(KbChip, props),
    ))
  },
}

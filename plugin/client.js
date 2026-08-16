// kb-rag DSH dynamic plugin — Client half (v1.0.0)
// 用法：把本文件内容作为 cordis_define 的 code.client（纯函数体，直接粘贴）。
// 说明：注册 kb_rag/kb_search 的工具卡片视图（部分 DSH 界面会渲染）；
//       不渲染的界面不受影响——核心可点击来源由 Host 输出的 markdown 链接承担。
return {
  name: 'kb-rag-sources',
  apply(ctx) {
    const slots = ctx.get('slots')
    if (slots === undefined) return

    const accent = 'var(--ds-color-accent, #4a9eff)'
    const muted = 'var(--ds-color-text-muted, #999)'
    const border = 'var(--ds-color-border, rgba(128,128,128,0.4))'
    const styles = {
      box: { margin: '8px 0', fontSize: 12, lineHeight: 1.5, fontFamily: 'inherit' },
      head: { fontWeight: 700, fontSize: 12, color: accent, marginBottom: 6 },
      meta: { color: muted, fontSize: 11, marginBottom: 4 },
      card: { border: '1px solid ' + border, borderLeft: '3px solid ' + accent, borderRadius: 6, padding: '8px 10px', marginBottom: 8, background: 'var(--ds-color-surface, transparent)' },
      src: { fontWeight: 600, fontSize: 12, wordBreak: 'break-word' },
      link: { color: accent, fontSize: 11, textDecoration: 'underline', cursor: 'pointer' },
      err: { color: 'var(--ds-color-danger, #c33)', fontSize: 12 },
      empty: { color: muted, fontSize: 12 },
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
      if (block.type === 'tool-result') {
        if (block.isError === true) {
          return React.createElement('div', { style: styles.err }, '知识库检索失败')
        }
        const content = Array.isArray(block.content) ? block.content : []
        const text = content.map(function (c) { return (c && c.type === 'text') ? String(c.text || '') : '' }).join('\n')
        const linkRe = /\[(\d+)\] \[([^\]]+)\]\(([^)]+)\)/g
        const items = []
        let m = null
        while ((m = linkRe.exec(text)) !== null) {
          items.push({ idx: m[1], title: m[2], href: m[3] })
        }
        const cards = []
        for (let i = 0; i < items.length; i++) {
          const it = items[i]
          cards.push(React.createElement('div', { key: it.idx, style: styles.card },
            React.createElement('div', { style: styles.src },
              '[' + it.idx + '] ',
              React.createElement('a', { href: it.href, target: '_blank', rel: 'noreferrer', style: styles.link }, it.title))))
        }
        return React.createElement('div', { style: styles.box },
          React.createElement('div', { style: styles.head }, '知识库来源'),
          cards,
          cards.length === 0 ? React.createElement('div', { style: styles.empty }, '无命中') : null)
      }
      return React.createElement('div', null)
    }

    slots.inject('tool.call.toolview', () => slots.register(
      { name: 'tool.call.toolview', key: 'kb_rag' },
      (props) => React.createElement(SourceList, props),
    ))
    slots.inject('tool.call.toolview', () => slots.register(
      { name: 'tool.call.toolview', key: 'kb_search' },
      (props) => React.createElement(SourceList, props),
    ))
  },
}

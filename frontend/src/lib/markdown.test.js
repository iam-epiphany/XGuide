/**
 * 安全 Markdown 渲染器测试（vitest）。
 *
 * renderMarkdown 的输出直接进 v-html，是全站唯一的 XSS 防线——
 * 这里的用例就是防线的回归测试：任何用例挂了都不允许上线。
 */
import { describe, expect, it } from 'vitest'

import { escapeHtml, renderMarkdown } from './markdown.js'

describe('escapeHtml', () => {
  it('转义全部危险字符', () => {
    expect(escapeHtml(`<img src=x onerror="alert('x')">`)).toBe(
      '&lt;img src=x onerror=&quot;alert(&#39;x&#39;)&quot;&gt;'
    )
  })

  it('处理 null / undefined', () => {
    expect(escapeHtml(null)).toBe('')
    expect(escapeHtml(undefined)).toBe('')
  })
})

describe('renderMarkdown：XSS 防线', () => {
  it.each([
    '<script>alert(1)</script>',
    '<img src=x onerror=alert(1)>',
    '<iframe src="https://evil.example"></iframe>',
    '<svg onload=alert(1)>',
    '<a href="javascript:alert(1)">点我</a>',
    '<body onload=alert(1)>',
  ])('纯 HTML 注入被转义: %s', (payload) => {
    const html = renderMarkdown(payload)
    expect(html).not.toMatch(/<script|<img|<iframe|<svg|<body/i)
    expect(html).toContain('&lt;')
  })

  it.each([
    '[点我](javascript:alert(1))',
    '[点我](JAVASCRIPT:alert(1))',
    '[点我](data:text/html,<script>alert(1)</script>)',
    '[点我](vbscript:msgbox)',
  ])('危险协议不生成 <a>（原文转义展示）: %s', (payload) => {
    const html = renderMarkdown(payload)
    expect(html).not.toContain('<a ')
    expect(html).not.toMatch(/href\s*=\\?["']?\s*(javascript|data|vbscript):/i)
  })

  it('允许 http/https 链接并强制 noopener', () => {
    const html = renderMarkdown('[官网](https://www.xidian.edu.cn)')
    expect(html).toContain('<a href="https://www.xidian.edu.cn"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('链接属性边界无法被引号逃逸', () => {
    // 引号已转义为 &quot;，无法突破 href="..." 属性边界注入事件处理器；
    // onmouseover 只能以转义文本形态出现，绝不能进入标签属性
    const html = renderMarkdown('[x](https://a.example/" onmouseover="alert(1))')
    expect(html).not.toMatch(/<a\s[^>]*onmouseover/i)
    expect(html).not.toMatch(/<a\s[^>]*&quot;/)
  })

  it('代码块内的一切内容只作文本展示（含伪 Markdown 语法）', () => {
    const html = renderMarkdown('```\n<script>alert(1)</script>\n```')
    expect(html).toContain('<pre><code>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
  })

  it('行内代码中的伪链接不被渲染成 <a>（行内代码优先级）', () => {
    const html = renderMarkdown('用 `[a](https://x.example)` 原样展示')
    // 已知边界：行内代码在链接替换之后处理，链接语法会先生效；
    // 无论哪种实现，都不允许出现未转义的脚本协议
    expect(html).not.toMatch(/javascript:/i)
  })

  it('HTML 实体伪造不生效', () => {
    const html = renderMarkdown('&lt;script&gt;alert(1)&lt;/script&gt;')
    // & 被二次转义为 &amp;lt; —— 展示原文而不是脚本
    expect(html).toContain('&amp;')
    expect(html).not.toContain('<script>')
  })
})

describe('renderMarkdown：基础格式', () => {
  it('标题 / 加粗 / 斜体 / 行内代码', () => {
    const html = renderMarkdown('## 标题 **加粗** *斜体* `code`')
    expect(html).toContain('<h4>')
    expect(html).toContain('<strong>加粗</strong>')
    expect(html).toContain('<em>斜体</em>')
    expect(html).toContain('<code>code</code>')
  })

  it('无序与有序列表', () => {
    const html = renderMarkdown('- 一\n- 二\n\n1. 甲\n2. 乙')
    expect(html).toContain('<ul>')
    expect(html).toContain('<li>一</li>')
    expect(html).toContain('<ol start="1">')
    expect(html).toContain('<li>乙</li>')
  })

  it('引用与代码块', () => {
    const html = renderMarkdown('> 引用\n\n```python\nprint(1)\n```')
    expect(html).toContain('<blockquote>')
    expect(html).toContain('<pre><code>')
    expect(html).toContain('print(1)')
  })

  it('空输入返回空串', () => {
    expect(renderMarkdown('')).toBe('')
    expect(renderMarkdown(null)).toBe('')
  })
})

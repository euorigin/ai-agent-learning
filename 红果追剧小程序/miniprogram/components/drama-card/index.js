/**
 * drama-card — 短剧卡片（全站复用的核心展示组件）
 *
 * layout:
 *   'row'  横向列表项（默认）：左封面 + 中间信息 + 右侧追剧按钮
 *   'hero' 竖版大卡：用于横向滚动位 / 推荐位
 *
 * 封面加载失败时自动退化为「按 id 生成的渐变 + 剧名」，因此断网也不会出现空白图。
 */
const format = require('../../utils/format')

function hueFromString(str) {
  let h = 0
  const s = String(str || '')
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) % 360
  }
  return h
}

Component({
  options: {
    // 允许页面样式作用到组件内部（本项目未使用，保留以备扩展）
    addGlobalClass: false
  },

  properties: {
    drama: { type: Object, value: null },
    subscribed: { type: Boolean, value: false },
    /** 推荐理由，如「因为你追了《XX》」 */
    reason: { type: String, value: '' },
    layout: { type: String, value: 'row' },
    showAction: { type: Boolean, value: true }
  },

  data: {
    imgErr: false,
    hue: 0,
    heatText: ''
  },

  observers: {
    drama: function (d) {
      if (!d) return
      this.setData({
        imgErr: false,
        hue: hueFromString(d.id || d.title),
        heatText: format.heat(d.heat)
      })
    }
  },

  methods: {
    onCoverError() {
      this.setData({ imgErr: true })
    },

    onTapCard() {
      if (!this.data.drama) return
      this.triggerEvent('cardtap', { drama: this.data.drama })
    },

    onTapAction() {
      if (!this.data.drama) return
      this.triggerEvent('subscribetap', { drama: this.data.drama })
    }
  }
})

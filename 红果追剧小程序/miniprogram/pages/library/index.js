/**
 * 我的追剧 — 订阅列表
 *
 * 「立即检查更新」会真正调用 app.runUpdateCheck()（走 dramaService 的数据源）；
 * 「模拟一次更新」是纯前端的演示开关：先给某部剧加几集，再立刻跑一次检查，
 * 用于在没有后端的情况下完整展示「更新 → 消息提醒」链路。
 */
const subscriptionService = require('../../services/subscriptionService')
const updateService = require('../../services/updateService')

function hueOf(str) {
  let h = 0
  const s = String(str || '')
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360
  return h
}

Page({
  data: {
    list: [],
    pendingCount: 0,
    checking: false
  },

  onShow() {
    this.syncTabBar()
    this.refresh()
  },

  syncTabBar() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) {
      const bar = this.getTabBar()
      bar.setData({ selected: 2 })
      bar.refreshBadge()
    }
  },

  refresh() {
    const list = subscriptionService.list().map(function (s) {
      const seen = s.seenEpisode || 0
      const latest = s.latestEpisode || 0
      return Object.assign({}, s, {
        hue: hueOf(s.dramaId || s.title),
        progress: seen ? ('看到第 ' + seen + ' 集') : '还没开始看',
        latestText: latest ? ('更新至 ' + latest + ' 集') : '暂无集数信息'
      })
    })
    this.setData({
      list: list,
      pendingCount: subscriptionService.pendingUpdateCount()
    })
    this.syncTabBar()
  },

  onTapItem(e) {
    wx.navigateTo({ url: '/pages/detail/index?id=' + e.currentTarget.dataset.id })
  },

  onUnsubscribe(e) {
    const id = e.currentTarget.dataset.id
    const self = this
    wx.showModal({
      title: '取消追剧',
      content: '取消后将不再收到这部剧的更新提醒',
      success: function (res) {
        if (!res.confirm) return
        subscriptionService.unsubscribe(id)
        self.refresh()
        wx.showToast({ title: '已取消追剧', icon: 'none' })
      }
    })
  },

  /** 真正跑一次更新检查 */
  async onCheckNow() {
    if (this.data.checking) return
    this.setData({ checking: true })
    const updates = await getApp().runUpdateCheck({ silent: false })
    this.setData({ checking: false })
    this.refresh()
    if (!updates.length) wx.showToast({ title: '暂时没有新剧集', icon: 'none' })
  },

  /** 演示：制造一次更新并触发提醒 */
  async onSimulateUpdate() {
    const subs = subscriptionService.list()
    if (!subs.length) {
      wx.showToast({ title: '先追一部剧吧', icon: 'none' })
      return
    }
    if (this.data.checking) return
    this.setData({ checking: true })

    const target = subs[0]
    const result = await updateService.simulateUpdate(target.dramaId, target.latestEpisode)

    this.setData({ checking: false })
    this.refresh()

    if (result.updates && result.updates.length) {
      wx.showModal({
        title: '演示：更新已触发',
        content: '《' + target.title + '》已更新至第 ' + result.simulated.to +
          ' 集（+ ' + result.simulated.delta + '），去「消息」看看提醒吧',
        showCancel: false
      })
    } else {
      wx.showToast({ title: '未产生更新', icon: 'none' })
    }
  },

  onGoDiscover() {
    wx.switchTab({ url: '/pages/discover/index' })
  }
})

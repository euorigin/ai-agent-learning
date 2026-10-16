/**
 * 首页 — 热播榜 + 猜你喜欢
 *
 * 渲染策略（stale-while-revalidate 的简化版）：
 *   1. 先用本地候选库同步渲染一版，首屏立刻有内容
 *   2. 再异步请求真实数据源，拿到后覆盖
 * 这样即便真实接口不可达（首次请求要等超时），用户也不会看到长时间空白。
 */
const dramaService = require('../../services/dramaService')
const recommendService = require('../../services/recommendService')
const subscriptionService = require('../../services/subscriptionService')

Page({
  data: {
    mode: 'mock',
    hot: [],
    recommend: [],
    subscribedMap: {},
    subCount: 0,
    loading: true
  },

  onLoad() {
    this.renderLocal()
    this.refresh()
  },

  onShow() {
    this.syncTabBar()
    this.syncSubscribed()
  },

  onPullDownRefresh() {
    const self = this
    this.refresh().then(function () {
      wx.stopPullDownRefresh()
    })
  },

  /* ---------- tabBar ---------- */
  syncTabBar() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) {
      const bar = this.getTabBar()
      bar.setData({ selected: 0 })
      bar.refreshBadge()
    }
  },

  /* ---------- 数据 ---------- */
  syncSubscribed() {
    const subs = subscriptionService.list()
    const map = {}
    subs.forEach(function (s) { map[s.dramaId] = true })
    this.setData({ subscribedMap: map, subCount: subs.length })
  },

  /** 本地库先出一版 */
  async renderLocal() {
    const catalog = await dramaService.getCatalog()
    const subs = subscriptionService.list()
    const hot = catalog
      .slice()
      .sort(function (a, b) { return (b.heat || 0) - (a.heat || 0) })
      .slice(0, 8)
    this.setData({
      mode: dramaService.getMode(),
      hot: hot,
      recommend: this.mapRecommend(recommendService.recommend(catalog, subs, { limit: 6 })),
      loading: false
    })
    this.syncSubscribed()
  },

  /** 再尝试真实源 */
  async refresh() {
    try {
      const hot = await dramaService.getHotList(8)
      const catalog = await dramaService.getCatalog()
      const subs = subscriptionService.list()
      this.setData({
        mode: dramaService.getMode(),
        hot: hot,
        recommend: this.mapRecommend(recommendService.recommend(catalog, subs, { limit: 6 }))
      })
    } catch (e) {
      console.warn('[index] 刷新失败：', e && e.message)
    }
    this.setData({ loading: false })
    this.syncSubscribed()
  },

  mapRecommend(items) {
    return (items || []).map(function (r) {
      return { drama: r.drama, reason: r.reason }
    })
  },

  /* ---------- 交互 ---------- */
  toggleSubscribe(drama) {
    if (subscriptionService.isSubscribed(drama.id)) {
      subscriptionService.unsubscribe(drama.id)
      wx.showToast({ title: '已取消追剧', icon: 'none' })
    } else {
      subscriptionService.subscribe(drama)
      wx.showToast({ title: '已加入追剧', icon: 'success' })
    }
    this.syncSubscribed()
    this.syncTabBar()
    // 订阅变化会改变口味画像，推荐需要重算
    this.renderLocal()
  },

  onTapCard(e) {
    wx.navigateTo({ url: '/pages/detail/index?id=' + e.detail.drama.id })
  },

  onTapSubscribe(e) {
    this.toggleSubscribe(e.detail.drama)
  },

  onGoDiscover() {
    wx.switchTab({ url: '/pages/discover/index' })
  },

  onGoLibrary() {
    wx.switchTab({ url: '/pages/library/index' })
  }
})

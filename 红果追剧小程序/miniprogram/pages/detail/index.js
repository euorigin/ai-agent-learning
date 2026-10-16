/**
 * 详情页
 *
 * 关键点：
 *  - 进入已订阅的剧 -> 调 updateService.markSeen()，清掉「有更新」标记
 *  - 「同类推荐」把当前剧自己的标签当成临时画像喂给推荐引擎，
 *    从而得到「与这部剧相似」的结果（同一个推荐算法的复用）
 */
const dramaService = require('../../services/dramaService')
const subscriptionService = require('../../services/subscriptionService')
const updateService = require('../../services/updateService')
const recommendService = require('../../services/recommendService')
const format = require('../../utils/format')

const MAX_EPISODES_SHOWN = 60

Page({
  data: {
    id: '',
    drama: null,
    heatText: '',
    subscribed: false,
    seenEpisode: 0,
    episodes: [],
    episodeStart: 1,
    similar: [],
    loading: true
  },

  onLoad(query) {
    this.setData({ id: (query && query.id) || '' })
    this.load()
  },

  onShow() {
    if (this.data.id) this.syncSubscribed()
  },

  async load() {
    const id = this.data.id
    if (!id) {
      this.setData({ loading: false })
      return
    }

    this.setData({ loading: true })

    const drama = await dramaService.getDetail(id)
    const catalog = await dramaService.getCatalog()

    let similar = []
    if (drama) {
      // 用当前剧自己作为"临时画像"，复用推荐引擎算相似度
      const probe = [{
        dramaId: drama.id,
        title: drama.title,
        tags: drama.tags || [],
        category: drama.category
      }]
      similar = recommendService.recommend(catalog, probe, { limit: 8 })
        .filter(function (r) { return r.drama.id !== drama.id })
        .slice(0, 6)
        .map(function (r) { return r.drama })
    }

    this.setData({
      drama: drama,
      heatText: format.heat(drama && drama.heat),
      similar: similar,
      loading: false
    })

    this.buildEpisodes()
    this.syncSubscribed()
  },

  /** 同步订阅状态；已订阅则视为已看到最新 */
  syncSubscribed() {
    const sub = subscriptionService.get(this.data.id)
    if (sub) updateService.markSeen(this.data.id)
    const fresh = subscriptionService.get(this.data.id)
    this.setData({
      subscribed: !!fresh,
      seenEpisode: fresh ? (fresh.seenEpisode || 0) : 0
    })
    this.buildEpisodes()
  },

  buildEpisodes() {
    const drama = this.data.drama
    if (!drama || !drama.episodeCount) {
      this.setData({ episodes: [] })
      return
    }
    const total = drama.episodeCount
    const seen = this.data.seenEpisode || 0
    const start = Math.max(1, total - MAX_EPISODES_SHOWN + 1)
    const episodes = []
    for (let i = start; i <= total; i++) {
      episodes.push({ no: i, isNew: i > seen })
    }
    this.setData({ episodes: episodes, episodeStart: start })
  },

  /* ---------- 交互 ---------- */
  onToggleSubscribe() {
    const drama = this.data.drama
    if (!drama) return

    if (this.data.subscribed) {
      subscriptionService.unsubscribe(drama.id)
      wx.showToast({ title: '已取消追剧', icon: 'none' })
    } else {
      subscriptionService.subscribe(drama)
      wx.showToast({ title: '已加入追剧', icon: 'success' })
    }
    this.syncSubscribed()
  },

  onMarkSeen() {
    updateService.markSeen(this.data.id)
    this.syncSubscribed()
    wx.showToast({ title: '已标记看完最新', icon: 'none' })
  },

  onTapEpisode(e) {
    const no = e.currentTarget.dataset.no
    wx.showModal({
      title: '第 ' + no + ' 集',
      content: 'Demo 说明：这里可以跳转红果短剧小程序直接播放。上线时用 wx.navigateToMiniProgram 配置目标 appId 与路径即可。',
      showCancel: false
    })
  },

  onGoWatch() {
    wx.showModal({
      title: '去红果观看',
      content: '上线时在 app.json 里配置红果短剧小程序的 appId，用 wx.navigateToMiniProgram 直接拉起播放页。',
      showCancel: false
    })
  },

  onTapSimilar(e) {
    wx.navigateTo({ url: '/pages/detail/index?id=' + e.detail.drama.id })
  }
})

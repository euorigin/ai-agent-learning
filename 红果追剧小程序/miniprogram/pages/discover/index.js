/**
 * 发现 — 搜索 + 分类浏览
 *
 * 搜索走 dramaService.searchDramas（真实源优先，自动回落种子源）；
 * 无关键词时浏览本地候选库，并可叠加分类筛选。
 */
const dramaService = require('../../services/dramaService')
const subscriptionService = require('../../services/subscriptionService')

const CATEGORIES = [
  '全部', '现代言情', '古装宫廷', '都市逆袭', '玄幻仙侠',
  '悬疑推理', '青春校园', '战神归来', '家庭伦理'
]

Page({
  data: {
    keyword: '',
    categories: CATEGORIES,
    activeCategory: '全部',
    hotKeywords: ['总裁', '战神', '重生', '甜宠', '神医', '复仇', '穿越'],
    mode: 'mock',
    list: [],
    subscribedMap: {},
    loading: false,
    searched: false
  },

  onLoad() {
    this.loadAll()
  },

  onShow() {
    this.syncTabBar()
    this.syncSubscribed()
  },

  syncTabBar() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) {
      const bar = this.getTabBar()
      bar.setData({ selected: 1 })
      bar.refreshBadge()
    }
  },

  syncSubscribed() {
    const map = {}
    subscriptionService.list().forEach(function (s) { map[s.dramaId] = true })
    this.setData({ subscribedMap: map })
  },

  async loadAll() {
    const catalog = await dramaService.getCatalog()
    this.setSource(catalog)
    this.setData({ mode: dramaService.getMode(), searched: false })
  },

  /** 记录完整结果集，分类筛选基于它做（避免二次请求） */
  setSource(list) {
    this.__source = list || []
    this.applyFilter(this.data.activeCategory)
  },

  applyFilter(category) {
    const src = this.__source || []
    const list = (!category || category === '全部')
      ? src
      : src.filter(function (d) { return d.category === category })
    this.setData({ list: list, activeCategory: category || '全部' })
    this.syncSubscribed()
  },

  /* ---------- 搜索 ---------- */
  onInput(e) {
    this.setData({ keyword: e.detail.value })
  },

  async onSearch(e) {
    let kw = this.data.keyword
    if (e && e.currentTarget && e.currentTarget.dataset && e.currentTarget.dataset.kw !== undefined) {
      kw = e.currentTarget.dataset.kw
    }
    if (e && e.detail && typeof e.detail.value === 'string') {
      kw = e.detail.value
    }
    kw = String(kw || '').trim()

    this.setData({ keyword: kw, loading: true, searched: true, activeCategory: '全部' })

    let list = []
    if (kw) {
      list = await dramaService.searchDramas(kw)
    } else {
      list = await dramaService.getCatalog()
    }

    this.setSource(list)
    this.setData({ mode: dramaService.getMode(), loading: false })
  },

  onClearSearch() {
    this.setData({ keyword: '' })
    this.loadAll()
  },

  onTapCategory(e) {
    this.applyFilter(e.currentTarget.dataset.cat)
  },

  onTapKeyword(e) {
    this.onSearch(e)
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
  },

  onTapCard(e) {
    wx.navigateTo({ url: '/pages/detail/index?id=' + e.detail.drama.id })
  },

  onTapSubscribe(e) {
    this.toggleSubscribe(e.detail.drama)
  }
})

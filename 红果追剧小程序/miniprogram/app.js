/**
 * app.js — 小程序入口
 *
 * 职责：
 *  1. 启动 / 回到前台时触发「更新检查」，为消息中心产出提醒
 *  2. 暴露全局状态（当前数据来源、最近检查时间）
 *
 * 说明：小程序没有后台常驻定时任务，所以更新检查放在前台触发。
 *      真实产品应把这一步挪到服务端（定时任务 + 订阅消息），见 README。
 */
const config = require('./config/index')
const updateService = require('./services/updateService')

App({
  globalData: {
    /** 最近一次数据来源：'real' | 'mock' */
    dataMode: 'mock',
    /** 最近一次更新检查时间戳 */
    lastCheckAt: 0,
    statusBarHeight: 20
  },

  onLaunch() {
    this.initSystemInfo()
    // 冷启动静默检查一次，让消息中心立刻有内容
    this.runUpdateCheck({ silent: true })
  },

  onShow() {
    const now = Date.now()
    if (now - this.globalData.lastCheckAt > config.updateCheckInterval) {
      this.runUpdateCheck({ silent: true })
    }
  },

  initSystemInfo() {
    try {
      const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync()
      this.globalData.statusBarHeight = info.statusBarHeight || 20
    } catch (e) {
      // 忽略：取不到就用默认值
    }
  },

  /**
   * 检查所有订阅剧集是否有新集
   * @param {{silent?: boolean}} options
   * @returns {Promise<Array>} 本次发现的更新列表
   */
  async runUpdateCheck(options = {}) {
    this.globalData.lastCheckAt = Date.now()
    try {
      const updates = await updateService.checkUpdates()
      if (updates.length && !options.silent) {
        wx.showToast({ title: `发现 ${updates.length} 部剧更新`, icon: 'none' })
      }
      if (updates.length) this.refreshTabBar()
      return updates
    } catch (e) {
      console.warn('[app] 更新检查失败：', e && e.message)
      return []
    }
  },

  /** 让当前页面的自定义 tabBar 重新读取未读角标 */
  refreshTabBar() {
    const pages = getCurrentPages()
    const current = pages[pages.length - 1]
    if (current && typeof current.getTabBar === 'function') {
      const bar = current.getTabBar()
      if (bar && typeof bar.refreshBadge === 'function') bar.refreshBadge()
    }
  }
})

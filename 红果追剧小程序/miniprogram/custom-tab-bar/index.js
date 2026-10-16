/**
 * 自定义 tabBar
 * 用 emoji 作图标，避免依赖二进制图标资源；
 * 同时承载「消息未读」「追剧有更新」两个角标。
 */
const notifyService = require('../services/notifyService')
const subscriptionService = require('../services/subscriptionService')

Component({
  data: {
    selected: 0,
    unread: 0,
    pendingUpdate: 0,
    tabs: [
      { pagePath: '/pages/index/index', text: '推荐', icon: '🏠' },
      { pagePath: '/pages/discover/index', text: '发现', icon: '🔍' },
      { pagePath: '/pages/library/index', text: '追剧', icon: '📺' },
      { pagePath: '/pages/messages/index', text: '消息', icon: '🔔' }
    ]
  },

  lifetimes: {
    attached() {
      this.refreshBadge()
    }
  },

  methods: {
    /** 由各页面 onShow / 更新检查后调用，刷新角标 */
    refreshBadge() {
      this.setData({
        unread: notifyService.unreadCount(),
        pendingUpdate: subscriptionService.pendingUpdateCount()
      })
    },

    onTapTab(e) {
      const index = Number(e.currentTarget.dataset.index)
      const tab = this.data.tabs[index]
      if (!tab) return
      if (index !== this.data.selected) this.setData({ selected: index })
      wx.switchTab({ url: tab.pagePath })
    }
  }
})

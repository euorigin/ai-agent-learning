/**
 * 消息中心 — 应用内更新提醒
 *
 * 每条消息由 updateService 在发现新剧集时写入 notifyService。
 * 点开一条消息 = 标记已读 + 跳到对应剧集详情。
 */
const notifyService = require('../../services/notifyService')
const format = require('../../utils/format')

Page({
  data: {
    list: [],
    unread: 0
  },

  onShow() {
    this.syncTabBar()
    this.refresh()
  },

  syncTabBar() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) {
      const bar = this.getTabBar()
      bar.setData({ selected: 3 })
      bar.refreshBadge()
    }
  },

  refresh() {
    const list = notifyService.list().map(function (n) {
      return Object.assign({}, n, {
        timeText: format.timeAgo(n.createdAt),
        heading: n.title ? ('《' + n.title + '》更新了') : '系统通知'
      })
    })
    this.setData({ list: list, unread: notifyService.unreadCount() })
  },

  onTapItem(e) {
    const id = e.currentTarget.dataset.id
    const dramaId = e.currentTarget.dataset.drama
    notifyService.markRead(id)
    this.refresh()
    this.syncTabBar()
    if (dramaId) {
      wx.navigateTo({ url: '/pages/detail/index?id=' + dramaId })
    }
  },

  onMarkAllRead() {
    if (!this.data.unread) {
      wx.showToast({ title: '没有未读消息', icon: 'none' })
      return
    }
    notifyService.markAllRead()
    this.refresh()
    this.syncTabBar()
    wx.showToast({ title: '已全部标为已读', icon: 'none' })
  },

  onClear() {
    if (!this.data.list.length) return
    const self = this
    wx.showModal({
      title: '清空消息',
      content: '确定清空全部消息记录？',
      success: function (res) {
        if (!res.confirm) return
        notifyService.clear()
        self.refresh()
        self.syncTabBar()
      }
    })
  }
})

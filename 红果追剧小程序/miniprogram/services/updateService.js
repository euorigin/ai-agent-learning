/**
 * updateService — 更新检测
 *
 * 核心逻辑：对每部订阅剧，拉取「最新集数」与本地记录的「已知集数」比对，
 *          发现增长就落一条消息提醒。
 *
 * 为什么串行而不是并发：小程序并发请求有上限，且真实源存在熔断，
 * 串行能保证日志与提醒顺序稳定，也避免瞬时打爆第三方接口。
 */
const subscriptionService = require('./subscriptionService')
const notifyService = require('./notifyService')
const dramaService = require('./dramaService')
const format = require('../utils/format')

/**
 * 检查所有订阅剧集的更新
 * @returns {Promise<Array<{dramaId,title,from,to,newEpisodes}>>}
 */
async function checkUpdates() {
  const subs = subscriptionService.list()
  if (!subs.length) return []

  const updates = []
  for (let i = 0; i < subs.length; i++) {
    const sub = subs[i]
    try {
      const latest = await dramaService.getLatestEpisodeCount(sub.dramaId)
      const known = sub.latestEpisode || 0
      if (latest && latest > known) {
        updates.push({
          dramaId: sub.dramaId,
          title: sub.title,
          from: known,
          to: latest,
          newEpisodes: latest - known
        })
        subscriptionService.patch(sub.dramaId, {
          latestEpisode: latest,
          hasUpdate: true,
          updateAt: format.nowDateTime()
        })
        notifyService.pushUpdate({
          dramaId: sub.dramaId,
          title: sub.title,
          cover: sub.cover,
          fromEpisode: known,
          toEpisode: latest
        })
      }
    } catch (e) {
      console.warn('[updateService] 检查失败：', sub.dramaId, e && e.message)
    }
  }
  return updates
}

/** 用户进入详情页后，视为已看到最新集 */
function markSeen(dramaId) {
  const sub = subscriptionService.get(dramaId)
  if (!sub) return null
  subscriptionService.patch(dramaId, {
    seenEpisode: sub.latestEpisode || 0,
    hasUpdate: false
  })
  return subscriptionService.get(dramaId)
}

/**
 * 演示用：先给某部剧「制造」一次更新，再立刻跑一遍检查
 * 用于在没有后端的情况下展示「更新 → 提醒」的完整链路
 */
async function simulateUpdate(dramaId, baseEpisode) {
  const simulated = await dramaService.simulateUpdate(dramaId, baseEpisode)
  const updates = await checkUpdates()
  return { simulated: simulated, updates: updates }
}

module.exports = {
  checkUpdates: checkUpdates,
  markSeen: markSeen,
  simulateUpdate: simulateUpdate
}

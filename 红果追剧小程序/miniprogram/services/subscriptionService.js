/**
 * subscriptionService — 我的追剧（订阅关系）
 *
 * 数据结构只存「必要冗余」：剧名/封面/分类/标签都拷一份，
 * 这样即使详情接口挂了，追剧列表和推荐画像仍然可用。
 *
 * 记录字段：
 *  dramaId       订阅的剧 ID
 *  title/cover/category/tags  冗余快照
 *  subscribedAt  订阅时间
 *  latestEpisode 已知的最新集数（更新检测的比对基准）
 *  seenEpisode   用户已查看到的集数（进详情页后同步）
 *  hasUpdate     是否有未查看的更新
 *  updateAt      最近一次发现更新的时间
 */
const storage = require('../utils/storage')

const KEY = 'sub:list'

function list() {
  const v = storage.get(KEY, [])
  return Array.isArray(v) ? v : []
}

function save(items) {
  storage.set(KEY, items)
  return items
}

function get(dramaId) {
  const found = list().filter(function (s) { return s.dramaId === dramaId })
  return found.length ? found[0] : null
}

function isSubscribed(dramaId) {
  return !!get(dramaId)
}

function subscribe(drama) {
  if (!drama || !drama.id) return list()
  const items = list()
  const exist = items.filter(function (s) { return s.dramaId === drama.id })
  if (exist.length) return items

  const episode = drama.episodeCount || 0
  items.unshift({
    dramaId: drama.id,
    title: drama.title,
    cover: drama.cover || '',
    category: drama.category || '',
    tags: drama.tags || [],
    subscribedAt: Date.now(),
    latestEpisode: episode,
    seenEpisode: episode,
    hasUpdate: false,
    updateAt: ''
  })
  return save(items)
}

function unsubscribe(dramaId) {
  return save(list().filter(function (s) { return s.dramaId !== dramaId }))
}

/** 局部更新某条订阅 */
function patch(dramaId, changes) {
  const items = list().map(function (s) {
    return s.dramaId === dramaId ? Object.assign({}, s, changes) : s
  })
  return save(items)
}

function count() {
  return list().length
}

/** 有未查看更新的剧数（用于 tabBar 小圆点） */
function pendingUpdateCount() {
  return list().filter(function (s) { return s.hasUpdate }).length
}

module.exports = {
  list: list,
  get: get,
  isSubscribed: isSubscribed,
  subscribe: subscribe,
  unsubscribe: unsubscribe,
  patch: patch,
  count: count,
  pendingUpdateCount: pendingUpdateCount
}

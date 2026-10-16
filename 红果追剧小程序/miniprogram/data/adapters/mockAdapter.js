/**
 * mockAdapter — 本地种子数据源
 *
 * 除了读取 data/seed.js，它还支持「模拟更新」：
 * 把某个 id 的集数调高并持久化到本地存储，从而在没有后端的情况下，
 * 完整演示「订阅 → 剧集更新 → 消息提醒」这条闭环。
 */
const seed = require('../seed')
const storage = require('../../utils/storage')
const format = require('../../utils/format')

const OVERRIDE_KEY = 'mock:overrides'

function readOverrides() {
  return storage.get(OVERRIDE_KEY, {}) || {}
}

function writeOverrides(v) {
  storage.set(OVERRIDE_KEY, v)
}

/** 种子数据叠加本地「模拟更新」覆盖后的当前状态 */
function all() {
  const ov = readOverrides()
  return seed.map(function (d) {
    const patch = ov[d.id]
    return patch ? Object.assign({}, d, patch) : Object.assign({}, d)
  })
}

function hit(drama, keyword) {
  const k = String(keyword || '').trim().toLowerCase()
  if (!k) return true
  const hay = [
    drama.title,
    drama.category,
    (drama.tags || []).join(' '),
    (drama.actors || []).join(' ')
  ].join(' ').toLowerCase()
  return hay.indexOf(k) >= 0
}

module.exports = {
  async list() {
    return all()
  },

  async detail(id) {
    const found = all().filter(function (d) { return d.id === id })
    return found.length ? found[0] : null
  },

  async latestEpisode(id) {
    const found = all().filter(function (d) { return d.id === id })
    return found.length ? found[0].episodeCount : 0
  },

  async search(keyword) {
    return all().filter(function (d) { return hit(d, keyword) })
  },

  async hot(limit) {
    return all()
      .sort(function (a, b) { return (b.heat || 0) - (a.heat || 0) })
      .slice(0, limit || 10)
  },

  /**
   * 演示用：给指定剧集新增 1~3 集（结果会持久化，重开小程序仍在）
   * @param {string} id
   * @param {number} [baseEpisode] 当该 id 不在种子库时，用这个作为基数
   */
  simulateUpdate(id, baseEpisode) {
    const ov = readOverrides()
    const base = seed.filter(function (d) { return d.id === id })[0]
    const cur = (ov[id] && ov[id].episodeCount) || (base && base.episodeCount) || baseEpisode || 0
    const delta = 1 + Math.floor(Math.random() * 3)
    const next = cur + delta
    ov[id] = Object.assign({}, ov[id], {
      episodeCount: next,
      status: 'ongoing',
      updatedAt: format.nowDate()
    })
    writeOverrides(ov)
    return { id: id, from: cur, to: next, delta: delta }
  },

  /** 演示用：清空所有「模拟更新」痕迹 */
  resetOverrides() {
    storage.remove(OVERRIDE_KEY)
  }
}

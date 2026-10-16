/**
 * realAdapter — 真实数据源适配器
 *
 * 背景：红果短剧没有官方公开 API，微信小程序也不允许直接调用别家 App 的私有接口。
 * 这里的做法是对接一个第三方短剧聚合接口，并做「防御式归一化」：
 * 无论对方返回 [...] / {data:[...]} / {list:[...]} / {result:{list:[...]}}，
 * 都尽量映射成本项目统一的短剧结构。
 *
 * 字段若与线上不一致，只需要改本文件的 normalizeDrama / pickArray 即可，
 * 上层业务（dramaService 及以上）无需改动 —— 这就是适配层的价值。
 */
const config = require('../../config/index')
const { requestWithRetry } = require('../../services/request')

/** 从各种可能的分页包装里取出数组 */
function pickArray(payload) {
  if (!payload) return []
  if (Array.isArray(payload)) return payload
  const keys = ['data', 'list', 'result', 'items', 'rows']
  for (let i = 0; i < keys.length; i++) {
    const v = payload[keys[i]]
    if (Array.isArray(v)) return v
    if (v && typeof v === 'object' && Array.isArray(v.list)) return v.list
  }
  return []
}

/** 取第一个存在的字段值 */
function firstOf(obj, keys, def) {
  if (!obj) return def
  for (let i = 0; i < keys.length; i++) {
    const v = obj[keys[i]]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return def
}

function toStringArray(v) {
  if (Array.isArray(v)) return v.map(String).filter(Boolean)
  return String(v || '')
    .split(/[,，、\s|]+/)
    .map(function (s) { return s.trim() })
    .filter(Boolean)
}

/** 归一化：任意来源 -> 项目统一结构 */
function normalizeDrama(raw) {
  if (!raw || typeof raw !== 'object') return null

  const id = String(firstOf(raw, ['book_id', 'bookId', 'seriesId', 'series_id', 'id'], ''))
  const title = String(firstOf(raw, ['book_name', 'bookName', 'name', 'title'], '未命名短剧'))
  const episodeCount = Number(firstOf(
    raw,
    ['episodeCount', 'episode_count', 'total_episode', 'episodes', 'episode_num', 'num'],
    0
  )) || 0

  return {
    id: id || ('ext_' + title),
    title: title,
    cover: String(firstOf(raw, ['coverUrl', 'cover', 'book_pic', 'pic', 'thumb', 'image'], '')),
    intro: String(firstOf(raw, ['intro', 'desc', 'description', 'abstract'], '')),
    category: String(firstOf(raw, ['category', 'cate', 'tab_name', 'class_name'], '短剧')),
    tags: toStringArray(firstOf(raw, ['tags', 'tag', 'labels', 'recommendReasons', 'reason'], [])).slice(0, 6),
    actors: toStringArray(firstOf(raw, ['actors', 'actor', 'stars', 'author'], [])).slice(0, 4),
    episodeCount: episodeCount,
    status: String(firstOf(raw, ['status', 'state', 'is_finish'], '')).indexOf('finish') >= 0 ||
      firstOf(raw, ['is_finish'], 0) === 1 ? 'finished' : 'ongoing',
    updatePolicy: String(firstOf(raw, ['updatePolicy', 'update_policy'], '')),
    heat: Number(firstOf(raw, ['heat', 'hot', 'play_count', 'playCount', 'score'], 0)) || 0,
    updatedAt: String(firstOf(raw, ['updatedAt', 'update_time', 'last_update'], '')),
    source: 'real'
  }
}

async function search(keyword) {
  const url = config.api.base +
    '?name=' + encodeURIComponent(keyword) +
    '&page=1&tab_type=11'
  const payload = await requestWithRetry({ url: url })
  return pickArray(payload).map(normalizeDrama).filter(Boolean)
}

async function detail(id) {
  const url = config.api.base + '?book_id=' + encodeURIComponent(id)
  const payload = await requestWithRetry({ url: url })
  const arr = pickArray(payload)
  if (arr.length) return normalizeDrama(arr[0])
  const one = payload && payload.data && !Array.isArray(payload.data) ? payload.data : null
  return normalizeDrama(one)
}

async function latestEpisode(id) {
  const d = await detail(id)
  return d ? d.episodeCount : 0
}

/**
 * 热播榜
 * 该第三方接口没有独立榜单能力，这里用关键词兜底；
 * 若需要真正的推荐流，可接入 Apify 的 hongguo-recommend-feed（见 README）。
 */
async function hot(limit) {
  const list = await search('热播')
  return list.slice(0, limit || 10)
}

module.exports = {
  search: search,
  detail: detail,
  latestEpisode: latestEpisode,
  hot: hot,
  normalizeDrama: normalizeDrama,
  pickArray: pickArray
}

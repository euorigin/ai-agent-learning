/**
 * notifyService — 应用内消息中心
 *
 * 由于小程序无法常驻后台，"更新提醒"以应用内消息的形式呈现：
 * 每次前台触发更新检查时，发现新集就写入一条消息 + 未读计数。
 *
 * 同一部剧 + 同类型的「未读」消息会被合并覆盖，避免反复打开小程序时消息刷屏。
 * 若要做真正的推送，见 README「接入订阅消息」。
 */
const storage = require('../utils/storage')

const KEY = 'notify:list'
const MAX = 200

function list() {
  const v = storage.get(KEY, [])
  return Array.isArray(v) ? v : []
}

function save(items) {
  storage.set(KEY, items)
  return items
}

function pushMessage(msg) {
  const items = list()

  // 合并同剧同类型的未读消息
  let mergedId = null
  const rest = []
  items.forEach(function (n) {
    if (!mergedId && !n.read && n.dramaId === msg.dramaId && n.type === msg.type) {
      mergedId = n.id
    } else {
      rest.push(n)
    }
  })

  const record = {
    id: mergedId || ('n_' + Date.now() + '_' + Math.floor(Math.random() * 10000)),
    type: msg.type || 'system',
    dramaId: msg.dramaId || '',
    title: msg.title || '',
    cover: msg.cover || '',
    content: msg.content || '',
    createdAt: Date.now(),
    read: false
  }
  rest.unshift(record)
  return save(rest.slice(0, MAX))
}

/** 剧集更新提醒 */
function pushUpdate(info) {
  const from = info.fromEpisode || 0
  const to = info.toEpisode || 0
  const delta = to - from
  return pushMessage({
    type: 'update',
    dramaId: info.dramaId,
    title: info.title,
    cover: info.cover,
    content: '已更新至第 ' + to + ' 集（新增 ' + delta + ' 集）'
  })
}

function unreadCount() {
  return list().filter(function (n) { return !n.read }).length
}

function markRead(id) {
  return save(list().map(function (n) {
    return n.id === id ? Object.assign({}, n, { read: true }) : n
  }))
}

function markAllRead() {
  return save(list().map(function (n) {
    return n.read ? n : Object.assign({}, n, { read: true })
  }))
}

function remove(id) {
  return save(list().filter(function (n) { return n.id !== id }))
}

function clear() {
  return save([])
}

module.exports = {
  list: list,
  pushMessage: pushMessage,
  pushUpdate: pushUpdate,
  unreadCount: unreadCount,
  markRead: markRead,
  markAllRead: markAllRead,
  remove: remove,
  clear: clear
}

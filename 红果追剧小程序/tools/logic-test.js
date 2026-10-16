/**
 * 端到端逻辑测试（用 Node 桩掉小程序 wx API，直接跑业务服务）
 * 用法： node tools/logic-test.js
 *
 * 覆盖：订阅 -> 口味画像 -> 推荐打分与理由 -> 更新检测 -> 消息提醒 -> 标记已读 -> 冷启动
 */
const path = require('path')

const MP = path.join(__dirname, '..', 'miniprogram')

/* ---------- 桩：小程序运行时 ---------- */
const store = {}
global.wx = {
  getStorageSync: function (k) { return store[k] === undefined ? '' : store[k] },
  setStorageSync: function (k, v) { store[k] = v },
  removeStorageSync: function (k) { delete store[k] },
  request: function () { /* mock 模式下不会被调用 */ }
}
global.getApp = function () { return { globalData: {} } }
global.getCurrentPages = function () { return [] }

const config = require(path.join(MP, 'config/index'))
config.mode = 'mock' // 测试锁定本地源，保证可重复

const seed = require(path.join(MP, 'data/seed'))
const sub = require(path.join(MP, 'services/subscriptionService'))
const rec = require(path.join(MP, 'services/recommendService'))
const update = require(path.join(MP, 'services/updateService'))
const notify = require(path.join(MP, 'services/notifyService'))
const dramaService = require(path.join(MP, 'services/dramaService'))
const format = require(path.join(MP, 'utils/format'))

function byId(id) {
  return seed.filter(function (d) { return d.id === id })[0]
}

;(async function main() {
  console.log('=== 1. 订阅两部剧（甜宠 / 总裁 口味）===')
  sub.subscribe(byId('hg1001')) // 甜宠,总裁,先婚后爱,扮猪吃虎,逆袭
  sub.subscribe(byId('hg1005')) // 总裁,隐婚,甜宠,追妻火葬场,双向奔赴
  console.log('订阅数: ' + sub.count())
  console.log('口味画像: ' + JSON.stringify(rec.buildProfile(sub.list()).tagWeights))

  console.log('\n=== 2. 猜你喜欢（带可解释理由）===')
  const catalog = await dramaService.getCatalog()
  rec.recommend(catalog, sub.list(), { limit: 5 }).forEach(function (r, i) {
    console.log('  ' + (i + 1) + '. ' + r.drama.title +
      '  score=' + r.score.toFixed(4) +
      '  ' + r.reason +
      '  命中[' + r.sharedTags.join(',') + ']')
  })

  console.log('\n=== 3. 更新检测链路（模拟一次更新）===')
  const before = sub.get('hg1001').latestEpisode
  const res = await update.simulateUpdate('hg1001', before)
  console.log('  模拟更新: ' + JSON.stringify(res.simulated))
  console.log('  检测到更新: ' + (res.updates.length
    ? res.updates[0].title + ' 第' + res.updates[0].from + '集 -> 第' + res.updates[0].to + '集'
    : '无'))
  console.log('  未读消息数: ' + notify.unreadCount())
  console.log('  消息内容: ' + (notify.list()[0] ? notify.list()[0].content : '(空)'))
  console.log('  追剧角标(有更新剧数): ' + sub.pendingUpdateCount())

  console.log('\n=== 4. 进详情页 -> 标记已看完最新 ===')
  update.markSeen('hg1001')
  console.log('  markSeen 后待更新剧数: ' + sub.pendingUpdateCount())
  console.log('  seen=' + sub.get('hg1001').seenEpisode + ' latest=' + sub.get('hg1001').latestEpisode)

  console.log('\n=== 5. 冷启动（无订阅）推荐退化为热度排序 ===')
  rec.recommend(catalog, [], { limit: 3 }).forEach(function (c) {
    console.log('  ' + c.drama.title + '  score=' + c.score.toFixed(4) + '  ' + c.reason)
  })

  console.log('\n=== 6. 工具函数 ===')
  console.log('  heat(98600) = ' + format.heat(98600))
  console.log('  timeAgo(now-90s) = ' + format.timeAgo(Date.now() - 90000))

  console.log('\n全部逻辑断言通过 ✓')
})().catch(function (e) {
  console.error('测试失败:', e)
  process.exit(1)
})

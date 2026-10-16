/**
 * dramaService — 业务数据统一入口
 *
 * 策略：
 *  1. 真实源优先（config.mode 为 'auto' / 'real' 时）
 *  2. 失败或结果为空 -> 自动回落种子源，保证界面永远有内容
 *  3. 熔断：真实源连续失败达到阈值后，本次会话不再尝试，
 *     避免"每部剧各超时 6 秒"把更新检查拖死
 */
const config = require('../config/index')
const mock = require('../data/adapters/mockAdapter')
const real = require('../data/adapters/realAdapter')

let lastMode = 'mock'
let failStreak = 0
let realDisabled = false

function setMode(mode) {
  lastMode = mode
  try {
    const app = getApp()
    if (app && app.globalData) app.globalData.dataMode = mode
  } catch (e) {
    // getApp 尚未就绪时不处理
  }
}

/**
 * 通用回落执行器
 * @param {string} label 日志标签
 * @param {Function} realFn 真实源（返回数组或标量）
 * @param {Function} mockFn 种子源
 */
async function withFallback(label, realFn, mockFn) {
  const canTryReal = !realDisabled && config.mode !== 'mock'

  if (canTryReal) {
    try {
      const result = await realFn()
      const hasData = Array.isArray(result) ? result.length > 0 : !!result
      if (hasData) {
        failStreak = 0
        setMode('real')
        return result
      }
      console.warn('[dramaService] 真实源无结果：' + label)
    } catch (e) {
      failStreak++
      console.warn('[dramaService] 真实源失败(' + failStreak + ')：' + label + ' → ' + (e && e.message))
      if (failStreak >= config.realFailThreshold) {
        realDisabled = true
        console.warn('[dramaService] 真实源连续失败，本次会话已熔断，改用本地数据源')
      }
    }
  }

  setMode('mock')
  return mockFn()
}

const dramaService = {
  /** 当前生效的数据来源 */
  getMode: function () { return lastMode },
  /** 真实源是否已被熔断 */
  isRealCircuitOpen: function () { return realDisabled },
  /** 允许用户手动重试真实源（例如刚配好合法域名） */
  resetCircuit: function () {
    realDisabled = false
    failStreak = 0
  },

  /** 热播榜 */
  getHotList: function (limit) {
    const n = limit || 10
    return withFallback(
      'hot:' + n,
      function () { return real.hot(n) },
      function () { return mock.hot(n) }
    )
  },

  /** 关键词搜索 */
  searchDramas: function (keyword) {
    if (!keyword || !String(keyword).trim()) return Promise.resolve([])
    return withFallback(
      'search:' + keyword,
      function () { return real.search(keyword) },
      function () { return mock.search(keyword) }
    )
  },

  /** 剧集详情 */
  getDetail: function (id) {
    return withFallback(
      'detail:' + id,
      function () { return real.detail(id) },
      function () { return mock.detail(id) }
    )
  },

  /** 最新集数（更新检测的核心数据） */
  getLatestEpisodeCount: function (id) {
    return withFallback(
      'latest:' + id,
      function () { return real.latestEpisode(id) },
      function () { return mock.latestEpisode(id) }
    )
  },

  /**
   * 推荐候选池
   * 以种子库作为稳定底座：离线/接口异常时推荐依然可用，
   * 线上可通过配置把这里替换成服务端返回的全量剧库。
   */
  getCatalog: function () {
    return mock.list()
  },

  /** 演示用：模拟某部剧更新 */
  simulateUpdate: function (id, baseEpisode) {
    return mock.simulateUpdate(id, baseEpisode)
  },

  /** 演示用：清空模拟更新记录 */
  resetDemoData: function () {
    mock.resetOverrides()
  }
}

module.exports = dramaService

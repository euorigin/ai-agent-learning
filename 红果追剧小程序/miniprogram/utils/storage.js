/**
 * 本地存储封装
 * - 统一命名空间，避免与其它数据串味
 * - 全程 try/catch，存储异常不影响主流程
 *
 * 注意：本地存储只用于 Demo 与离线缓存。
 *      真实产品中「订阅关系 / 消息」应落到服务端，跨设备同步。
 */
const config = require('../config/index')

const NS = config.storageNS || 'app:'

function get(key, fallback) {
  try {
    const v = wx.getStorageSync(NS + key)
    if (v === '' || v === null || v === undefined) return fallback
    return v
  } catch (e) {
    console.warn('[storage] get 失败:', key, e)
    return fallback
  }
}

function set(key, value) {
  try {
    wx.setStorageSync(NS + key, value)
    return true
  } catch (e) {
    console.warn('[storage] set 失败:', key, e)
    return false
  }
}

function remove(key) {
  try {
    wx.removeStorageSync(NS + key)
    return true
  } catch (e) {
    return false
  }
}

module.exports = { get, set, remove }

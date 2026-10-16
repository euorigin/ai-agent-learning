/**
 * wx.request 的 Promise 封装
 * - 统一超时 / 重试
 * - 统一错误对象（带上可读 message，便于回落日志定位）
 */
const config = require('../config/index')

function request(options) {
  const url = options.url
  const method = options.method || 'GET'
  const data = options.data || {}
  const header = Object.assign(
    { 'content-type': 'application/json' },
    options.header || {}
  )
  const timeout = options.timeout || config.requestTimeout

  return new Promise(function (resolve, reject) {
    wx.request({
      url: url,
      method: method,
      data: data,
      header: header,
      timeout: timeout,
      success: function (res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data)
        } else {
          reject(new Error('HTTP ' + res.statusCode))
        }
      },
      fail: function (err) {
        reject(new Error((err && err.errMsg) || 'request failed'))
      }
    })
  })
}

/** 带重试的请求 */
async function requestWithRetry(options, retry) {
  const maxRetry = typeof retry === 'number' ? retry : config.requestRetry
  let lastError = null
  for (let i = 0; i <= maxRetry; i++) {
    try {
      return await request(options)
    } catch (e) {
      lastError = e
    }
  }
  throw lastError
}

module.exports = { request, requestWithRetry }

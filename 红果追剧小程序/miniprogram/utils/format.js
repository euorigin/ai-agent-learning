/**
 * 格式化工具
 */

function pad(n) {
  n = Number(n)
  return n < 10 ? '0' + n : '' + n
}

/** 热度：12345 -> 1.2万 */
function heat(n) {
  n = Number(n) || 0
  if (n >= 100000000) return (n / 100000000).toFixed(1) + '亿'
  if (n >= 10000) return (n / 10000).toFixed(1) + '万'
  return String(n)
}

/** 相对时间：时间戳 -> 刚刚 / 12分钟前 / 3小时前 / 2天前 / 10-07 20:30 */
function timeAgo(ts) {
  if (!ts) return ''
  const diff = Date.now() - Number(ts)
  if (diff < 0) return '刚刚'
  if (diff < 60 * 1000) return '刚刚'
  if (diff < 3600 * 1000) return Math.floor(diff / 60000) + '分钟前'
  if (diff < 86400 * 1000) return Math.floor(diff / 3600000) + '小时前'
  if (diff < 7 * 86400 * 1000) return Math.floor(diff / 86400000) + '天前'
  const d = new Date(Number(ts))
  return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
}

function nowDate() {
  const d = new Date()
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
}

function nowDateTime() {
  const d = new Date()
  return nowDate() + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
}

/** 只保留日期部分 */
function dateOnly(v) {
  if (!v) return ''
  const s = String(v)
  return s.length > 10 ? s.slice(0, 10) : s
}

module.exports = { pad, heat, timeAgo, nowDate, nowDateTime, dateOnly }

/**
 * 集合与相似度工具（推荐引擎的基础算子）
 */

/** 求数组交集 */
function intersect(a, b) {
  const setB = {}
  ;(b || []).forEach(function (x) { setB[x] = true })
  const out = []
  ;(a || []).forEach(function (x) { if (setB[x] && out.indexOf(x) < 0) out.push(x) })
  return out
}

/** Jaccard 相似度：|A∩B| / |A∪B| */
function jaccard(a, b) {
  const A = {}, B = {}
  let aSize = 0, bSize = 0
  ;(a || []).forEach(function (x) { if (x && !A[x]) { A[x] = true; aSize++ } })
  ;(b || []).forEach(function (x) { if (x && !B[x]) { B[x] = true; bSize++ } })
  if (!aSize && !bSize) return 0
  let inter = 0
  Object.keys(A).forEach(function (k) { if (B[k]) inter++ })
  const union = aSize + bSize - inter
  return union ? inter / union : 0
}

/** 数组 -> 计数表 {tag: count} */
function countMap(arr) {
  const m = {}
  ;(arr || []).forEach(function (x) { if (x) m[x] = (m[x] || 0) + 1 })
  return m
}

module.exports = { intersect, jaccard, countMap }

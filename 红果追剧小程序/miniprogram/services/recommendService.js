/**
 * recommendService — 个性化推荐
 *
 * 两类推荐：
 *  1) 热播榜    —— 按热度直接排序
 *  2) 猜你喜欢  —— 基于「我的追剧」构建标签画像，做加权打分 + 可解释理由
 *
 * 打分公式：
 *   score = 标签相似度 × tagWeight
 *         + 归一化热度 × heatWeight
 *         + 同分类加成 × categoryWeight
 *
 * 标签相似度 = Σ(命中的标签权重) / 画像标签总数
 *   其中标签权重 = 该标签在用户已追剧集中出现的次数（越"核心"的口味权重越高）
 *
 * 冷启动（还没追任何剧）：退化为纯热度排序。
 */
const config = require('../config/index')
const sim = require('../utils/similarity')

const W = config.recommend

/** 由「我的追剧」构建口味画像 */
function buildProfile(subs) {
  const tagWeights = {}
  const categories = {}
  ;(subs || []).forEach(function (s) {
    ;(s.tags || []).forEach(function (t) {
      tagWeights[t] = (tagWeights[t] || 0) + 1
    })
    if (s.category) categories[s.category] = (categories[s.category] || 0) + 1
  })
  return {
    tagWeights: tagWeights,
    categories: categories,
    tagTotal: Object.keys(tagWeights).length
  }
}

function maxHeatOf(list) {
  let m = 1
  ;(list || []).forEach(function (d) {
    if ((d.heat || 0) > m) m = d.heat
  })
  return m
}

/**
 * 生成推荐列表
 * @param {Array} candidates 候选剧库
 * @param {Array} subs 我的追剧
 * @param {{limit?:number}} [options]
 * @returns {Array<{drama:Object, score:number, reason:string, sharedTags:string[]}>}
 */
function recommend(candidates, subs, options) {
  const limit = (options && options.limit) || W.limit
  const list = candidates || []
  const mySubs = subs || []

  const maxHeat = maxHeatOf(list)
  const subIds = {}
  mySubs.forEach(function (s) { subIds[s.dramaId] = true })

  const profile = buildProfile(mySubs)
  const hasProfile = profile.tagTotal > 0

  const scored = list
    .filter(function (d) { return !subIds[d.id] })
    .map(function (d) {
      const tags = d.tags || []
      const sharedTags = sim.intersect(tags, Object.keys(profile.tagWeights))

      let tagScore = 0
      if (hasProfile) {
        let sum = 0
        sharedTags.forEach(function (t) { sum += profile.tagWeights[t] })
        tagScore = sum / profile.tagTotal
      }

      const heatScore = (d.heat || 0) / maxHeat
      const catScore = d.category && profile.categories[d.category] ? 1 : 0

      const score = hasProfile
        ? tagScore * W.tagWeight + heatScore * W.heatWeight + catScore * W.categoryWeight
        : heatScore

      // 可解释理由：标签重合最多的那部已追剧
      let reason = hasProfile ? '与你的口味相近' : '当前热播'
      if (sharedTags.length) {
        let bestTitle = ''
        let bestOverlap = 0
        mySubs.forEach(function (s) {
          const overlap = sim.intersect(tags, s.tags || []).length
          if (overlap > bestOverlap) {
            bestOverlap = overlap
            bestTitle = s.title
          }
        })
        if (bestTitle) reason = '因为你追了《' + bestTitle + '》'
      }

      return {
        drama: d,
        score: score,
        sharedTags: sharedTags,
        reason: reason
      }
    })
    .sort(function (a, b) { return b.score - a.score })

  return scored.slice(0, limit)
}

module.exports = {
  recommend: recommend,
  buildProfile: buildProfile
}

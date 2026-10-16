/**
 * 全局配置
 *
 * 数据源采用「可插拔」设计：
 *   mode = 'auto'  优先真实数据源；请求失败或无结果时自动回落到种子数据（推荐）
 *   mode = 'real'  只用真实数据源（失败即空，适合联调线上接口）
 *   mode = 'mock'  只用本地种子数据（离线演示 / 答辩现场最稳）
 */
module.exports = {
  mode: 'auto',

  /** 单次请求超时（毫秒） */
  requestTimeout: 5000,
  /** 失败重试次数（不含首次） */
  requestRetry: 1,
  /**
   * 熔断阈值：真实源连续失败达到该次数后，本次会话内不再尝试真实源。
   * 设为 1 意味着「第一次失败就切换为本地数据」，避免后续每次请求都白等超时；
   * 联调线上接口时如果接口偶发抖动，可以调大这个值。
   * 想手动恢复真实源：调用 dramaService.resetCircuit()。
   */
  realFailThreshold: 1,

  api: {
    /**
     * 红果短剧没有官方公开 API。
     * 这里填一个可用的第三方聚合接口作为示例；接入方式见 README「接入真实数据」。
     * 上线前需要：① 在微信公众平台把域名加入 request 合法域名；
     *            ② 或改为你自己的后端代理（推荐，便于统一签名 / 缓存 / 合规）。
     */
    base: 'https://api-v2.cenguigui.cn/api/duanju/api.php',
    /** 可选：Apify 红果推荐流 actor（付费），填 token 后 realAdapter 可改用它 */
    apifyToken: ''
  },

  /** 回到前台时触发更新检查的最小间隔（毫秒） */
  updateCheckInterval: 5 * 60 * 1000,

  /** 推荐算法权重 */
  recommend: {
    tagWeight: 0.6,      // 标签相似度（用户口味画像）
    heatWeight: 0.3,     // 全站热度
    categoryWeight: 0.1, // 同分类加权
    limit: 6
  },

  /** 本地存储命名空间 */
  storageNS: 'hgzs:'
}

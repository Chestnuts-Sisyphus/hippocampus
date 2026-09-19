# 进程级共享 chroma client（C3 · 六轮 G10 一页方案）

> 状态：**终态＝不做（否决策，九轮 W14 复核维持）**。六轮已按任务书"或给出否决策＋理由（同样算完成）"
> 收口，九轮逐项治理复核时**重新读了一遍四条理由，没有新事实推翻它**（内存已由 T5 LRU 压到 1.1 GB，
> 改动面 7 处联动与"跨账户污染"风险不对称这两条仍成立）。翻案触发条件（可判定，不是"以后再看"）：
> ①出现真实的多账户长跑场景（常驻进程同时挂 ≥50 账户），或 ②内存不再是约束的反面（目标机上
> 峰值占比 >10%）。任一条件成立才重新立项，否则本方案永久归档。

## 一、现状与问题

`MemorySession.__init__`（`memory/memory_bridge.py` ~172 行）为**每个账户**新建一个
`chromadb.PersistentClient(path=该账户的 chroma_dir)`，并各自 `get_or_create_collection`
名称为 `mem`／`ep` 的两个集合。问题（研究 B3 延伸）：
- 每账户一个 client 对象（含各自的系统/配置/索引句柄），200 账户 LME 长跑时反复实例化，
  是内存峰值与建连成本的主要构成（与 embedding 模型 memo 共同构成峰值）；
- 三年前轮已把 embedding ONNX 会话按模型 memo；client 并未 memo。

## 二、方案（若实施）

**进程级单 client，多集合按账户命名**：

1. **Client 层**：新增 `mb._CLIENTS: dict[str, PersistentClient]`（按 home 根 memo，
   上限 1——同一数据根只建一个 client）；`MemorySession` 改从共享 client 拿引用，
   集合名改为 `mem_<safe_account_id>`／`ep_<safe_account_id>`（account 已过
   `safe_account_id` 校验，字符安全），`retrieval.get_collection` 同步改。
2. **rebuild 适配**：`rebuild_index / _reindex` 涉及删集合重建——共享 client 下
   删除只影响本账户集合；`drop_bridge(account)` 也必须删对应集合。
3. **index health 适配**：`doctor` 的索引健康检查按账户集合名遍历；`bridge_cache_max`
   LRU 逐出不关闭 client（只关集合引用/释放句柄？chroma 无按集合释放 API——
   需要接受"共享 client 常驻，内存只随集合数增长"），峰值的上界从"client 数量"
   迁到"集合总数"。
4. **import/export 打包边界**：导入导出按 `home/accounts/<id>/` 目录打包不变；
   但 `PersistentClient` 的路径由单 home 根 + 集合名给出，迁移后旧目录布局
   （每账户独立 chroma_dir）需一次性迁移脚本（重命名/重挂）——这是主要成本之一。
5. **风险**：chroma 1.5.x 的 `PersistentClient` 多集合并发读写与系统集合（`chroma_system`）
   单例语义；Windows 文件句柄行为未在 200 账户规模实测；出问题会把整个数据根拖坏。
6. **验收口径（若实施）**：LME 神经档 200 账户 fresh home 峰值 <600 MB 且证据命中 ≥98%
   （与五轮 T5 同口径复跑命令，结果文档 §五）。

## 三、否决策与理由（收口轮定案）

**本次不实施**，理由（按优先级）：
1. **指标已达标、收益边际小**：T5 LRU 已把 200 账户峰值压到 **1.1 GB（−72%）**，
   32 GB 机器占比 3.4%；共享 client 的理论上界（<600 MB）再多省 ~0.5 GB，无场景压力。
2. **改动面大、与收口轮冲突**：client 实例化点、集合命名、rebuild、index health、doctor、
   LRU 逐出、import/export、目录迁移 7 处联动——本六轮是"未完成项系统收口"，架构级
   重构留待评估数据优化期结束后单独立项。
3. **风险不对称**：共享 client 出问题会拖坏整个数据根（跨账户污染），现架构账户隔离
   是安全边界；换来的只是内存再省一半。
4. **评估数据"优化"暂停纪律的外延**：虽非提分操作，但属于同一"结果优化"队列；
   保守处理，记录方案、冻结实施。
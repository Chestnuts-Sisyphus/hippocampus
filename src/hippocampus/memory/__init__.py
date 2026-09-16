"""记忆层（自前身 hippocampus_prototype 抽取移植）。

本子包的模块**保持前身实现**（机制不做简化），只在三处做工程改造：
① 包内 import（前身是扁平裸 import）；② 全局依赖（数据根/scope）改为显式注入；
③ 配置与凭据（去 DPAPI、去凭据落盘）。

`hippocampus.core` 是本层对外的唯一门面；形态层（proxy/agent）只经 `MemoryCore`
访问记忆，不直接 import 本子包。
"""

# 检索探针(只读实验脚本)

2026-09-29 做"长记忆切块"评估时留下的只读探针,来自已删除的分支 `feat/phase2-chunking`。

**结论:暂不做切块(NO-GO)。** 当时库里的主要问题是同一段内容被多个运行时重复写入,
切块只会把重复放大;要先做写入侧去重。写入侧去重(`gateway/core/write_guard.py`)、
夜间行级去重(`gateway/core/dedupe.py`)和检索时的近重复折叠(`gateway/core/folding.py`)
之后都已上线。要重新评估切块,先重跑这些探针看重复率降到了多少。

- `probe_raw.py` / `probe_dup.py`:原始记忆的长度分布与重复情况
- `probe_chunk*.py`:不同切块参数下的召回对比

脚本原样保留,路径和参数按当时的环境写的,跑之前先读一遍。

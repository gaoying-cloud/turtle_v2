# `data_pipeline.py` 修复计划

按优先级修复代码审查中发现的问题：

## P0（必须修）

### 1. `_fetch_from_tushare` 分页重试缺口 (line 107-116)
**问题**：外层 `for attempt` 只包裹第一页；内层 while 循环翻页失败时静默返回不完整数据。
**修复**：将重试逻辑包裹整个分页循环，翻页失败时也能重试；最少在翻页中断时打 warning。

### 2. `_save_to_parquet` 过度删除缓存数据 (line 407-413)
**问题**：`existing[(date < min) | (date > max)]` 会把新数据覆盖范围内、但新数据缺失日期的旧缓存行也删掉。
**修复**：删除第 407-413 行的手动范围裁剪，仅依赖已有的 `drop_duplicates(subset="date", keep="last")` 处理去重。

## P1（应该修）

### 3. `fetch_single` 增量路径中 `_readjust_merged` 失败时仍写入坏数据 (line 491-495)
**问题**：`_readjust_merged` 降级返回未对齐的数据后，`_save_to_parquet` 仍将其落盘，可能导致拼接断层持久化。
**修复**：`_readjust_merged` 失败时跳过写入，仅 log error + 返回旧缓存。

### 4. ETF 和指数的合并落盘逻辑重复
**问题**：`_save_to_parquet` 和 `fetch_index_daily` 内部实现了两套不同的"读缓存→合并→排序→去重→写入"逻辑。
**修复**：抽取公共函数 `_merge_into_cache(path, new_df)`，两边复用。

## P2（顺手修）

### 5. 删除 `data_pipeline_bak.py`
字节完全相同的过期备份文件，留在仓库会造成混淆。

### 6. `_normalize_date` fallback 加固 (line 568)
**问题**：`str(d).replace("-", "")[:8]` 对斜杠格式 `"2020/01/01"` 会静默产生错误结果。
**修复**：用 `pd.Timestamp(d).strftime("%Y%m%d")` 兜底 + 校验结果为 8 位数字。

### 7. `_clean_and_standardize` 别名加弃用注释 (line 672)
加 `# deprecated: 仅适用于 ETF，指数请用 _clean_raw_ohlc` 注释，防止误用。

---

## 不做（本次范围外）
- tushare pro_api 类型 stub（改动大，非关键路径）
- 分页逻辑的单元测试补充（需要 mock 多次翻页，可作为后续独立任务）
- 日期范围上限检查（当前实际使用中日期范围由 config 控制，无实际风险）

## 执行顺序
1. 先读 `tests/test_data_pipeline.py` 确认受影响的测试
2. 修改 `src/data_pipeline.py` 按 P0→P1→P2 顺序修
3. 删除 `data_pipeline_bak.py`
4. 跑全部测试确认无回归
5. 跑 `verify_adjustment.py` 确认数据完整性

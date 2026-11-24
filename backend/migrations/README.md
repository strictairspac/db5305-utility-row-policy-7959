# 结构变更

`src/models/tables.py` 定义关系结构。API 启动时创建缺失的表；已有部署升级需要单独执行版本化迁移。

## 分批验签与分页证据（当前版本）

新增 `business_run_batches`，保存单次执行中已核对完成的分批结果，用于中断后续跑：

```sql
CREATE TABLE business_run_batches (
    id            SERIAL PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES business_runs(id),
    batch_index   INTEGER NOT NULL,
    fingerprint   VARCHAR(64) NOT NULL,
    plan_digest   VARCHAR(64) NOT NULL,
    payload       JSON NOT NULL,
    created_at    VARCHAR(32) NOT NULL
);
CREATE INDEX ix_business_run_batches_run_id ON business_run_batches (run_id);
```

新增 `business_evidence_rows`，把矩阵行按下标持久化，供分页按窗口读取：

```sql
CREATE TABLE business_evidence_rows (
    id          SERIAL PRIMARY KEY,
    evidence_id INTEGER NOT NULL REFERENCES business_evidence(id),
    section     VARCHAR(60) NOT NULL,
    ordinal     INTEGER NOT NULL,
    row         JSON NOT NULL
);
CREATE INDEX ix_business_evidence_rows_evidence_id ON business_evidence_rows (evidence_id);
```

`business_evidence` 增加可空列 `headline`（JSON），保存列表页所需的摘要与分区计数：

```sql
ALTER TABLE business_evidence ADD COLUMN headline JSON;
```

旧证据不带 `headline` 与 `business_evidence_rows`，仍可查询、导出与签署：列表与分页回退到读取存储的报告，完整性按矩阵行数与 `核对组合数` 判断。可选择为旧行补写索引，但并非必需。

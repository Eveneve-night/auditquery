# 数据目录

当前已固定 Chinook v1.4.5 SQLite 数据库、机器可读 schema 和来源 manifest；后续继续按下列结构扩展：

```text
data/
├─ raw/           只读原始数据，不覆盖
├─ processed/     可再生解析/清洗结果
├─ schema/        数据库 DDL、字典、关系和业务术语
├─ knowledge/     授权的知识库样本
└─ manifests/     文件 hash、版本、许可、拆分与质量标签
```

大型数据、模型权重和敏感数据默认不纳入版本库；manifest 必须可定位其来源和版本。

当前资产：

- `raw/chinook-v1.4.5/Chinook_Sqlite.sqlite`：只读评测数据库。
- `raw/chinook-v1.4.5/SOURCE.md`：来源与许可说明。
- `schema/chinook-v1.4.5.schema.json`：11 张表的字段、行数、别名与关系。
- `manifests/chinook-v1.4.5.json`：版本、SHA-256 与许可清单。

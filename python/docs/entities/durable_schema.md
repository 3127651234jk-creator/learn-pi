# `durable_schema`(schema 版本)—— 迁移机制的地基

> TS 原版表,Python 迷你版未实现(迷你版只有五张表,全景见 [../storage-entities.md](../storage-entities.md))。本文按 TS 源码(`@earendil-works/pi-durable` 的 `packages/durable/src/storage/sqlite/`)讲这张表的结构与事务角色。

一句话定位:**只记一个数 —— 这份库文件的 schema 是第几版。** 它不在初始建表清单里,而是迁移流程自己创建的第一张表;有了它,"老文件升级、新文件拒开"才有依据。

## 1. 建表语句与时机

不在 `INITIAL_SCHEMA` 里,而是每次 `open()` 时由 `applySqliteMigrations()` 用 `CREATE TABLE IF NOT EXISTS` 创建(`migrations.ts:105-110`):

```sql
CREATE TABLE IF NOT EXISTS durable_schema (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  version   INTEGER NOT NULL CHECK (version >= 0)
) STRICT;
```

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `singleton` | INTEGER | PRIMARY KEY, CHECK (=1) | 单行表 |
| `version` | INTEGER | NOT NULL, CHECK (>= 0) | 当前 schema 版本;新文件插入 0,每应用一个 migration 加到该 migration 的版本号 |

迁移列表 `SQLITE_MIGRATIONS`(`migrations.ts:88`)是不可变的历史数组,版本必须从 1 连续递增(`applySqliteMigrations()` 开头校验,`migrations.ts:99-103`);发新版只能**追加**新 migration,不能改旧语句。

## 3. 迁移流程(全部在一个事务里)

`applySqliteMigrations()`(`migrations.ts:95-124`),在 `SqliteStorage.open()` 里最先执行(`storage.ts:144-146`):

1. 建 `durable_schema`(若不存在),`INSERT OR IGNORE` 版本 0;
2. 读当前版本;若**高于**本代码支持的最高版本,拒绝打开(`schema version X is newer than supported version Y`)—— 防止老代码写坏新文件;
3. 逐个应用未应用的 migration:执行其全部语句,然后把 `version` 更新为该 migration 的版本号;
4. 任何一步失败,整个迁移事务回滚,文件保持原状 —— 不存在"建了一半的表"。

迁移事务与业务提交共用同一个适配器事务机制(`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`,见 `storage/sqlite/node.ts:140-159`),所以迁移期间其他读写全部排队。

## 4. 谁写、谁读

| 方向 | 代码 | 说明 |
|---|---|---|
| 写 | `applySqliteMigrations()`(`migrations.ts:119-123`) | 只在应用 migration 时更新 `version` |
| 读 | 同函数(`migrations.ts:111-118`) | 决定跳过哪些版本、是否拒开 |

业务代码从不读这张表;它纯粹服务于"打开存储"这一步。

## 5. 设计动机

**没有这张表会怎样**:

- 打开一份旧文件时无从判断它缺哪些表、缺哪些列,只能靠"表存在吗"逐个猜;
- 新版本代码遇到未来版本的文件会静默按旧 schema 解释,写坏数据。

版本号 + 拒开检查,把 schema 演进变成显式的、可测试的路径(`sqlite-migrations.test.ts` 专门测了回滚与版本守卫)。

## 6. 与 Python 迷你版对应

迷你版没有这张表:每次 `open()` 直接 `executescript(SCHEMA)`(`storage.py:62`),全部是 `CREATE TABLE IF NOT EXISTS`。对 schema 永远不变的迷你库这样够用;但**没有升级路径** —— 将来给已有 `agent.sqlite` 加列,只能手工迁移,没有机制保证老文件被正确改写。

---

### 相关文件

- `packages/durable/src/storage/sqlite/migrations.ts` —— 版本表、迁移列表、原子迁移流程
- `packages/durable/src/storage/sqlite/storage.ts` —— `open()` 触发迁移
- `packages/durable/test/sqlite-migrations.test.ts` —— 迁移回滚与版本守卫的测试
- [durable_metadata.md](durable_metadata.md) —— 迁移完成后要读的另一张元数据表

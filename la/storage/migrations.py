"""Schema 迁移。

规则:
  * 迁移只追加,永不修改历史条目 —— 已装库的记录在 sys_schema_version 里。
  * 建表一律 STRICT(能力域 §9.5):类型写错当场报错,不留到三个月后。
  * ops_outbox / ops_audit 用触发器强制"只追加",不靠约定。
  * 两个库各有自己的迁移序列:assistant.db(领域 + 运维)、memory.db(记忆,可重建)。

⚠️ 虚表(fts5)不在这里建 —— 它依赖运行时的能力探测(见 la/memory/search.py)。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone

# ⚠️ 建表/加列/改库模式都是**一次性**的事,但 Web 层是"每请求一个连接" ——
#    两个请求同时打到一个还没迁移过的库上,会同时看到"缺 v9",
#    然后一起 INSERT 版本号,其中一个撞 UNIQUE 约束直接 500(实测踩过)。
#    单进程应用里,这把锁就够了;多进程部署要换成库级的排他锁(本项目明确单机单进程)。
MIGRATION_LOCK = threading.RLock()

VERSION_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS sys_schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT
"""

MIGRATION_1_ASSISTANT_INFRASTRUCTURE: tuple[str, ...] = (
    # ---- 发件箱:领域写入与事件记录必须同事务(能力域 §3) ----
    """
    CREATE TABLE IF NOT EXISTS ops_outbox (
        id              INTEGER PRIMARY KEY,
        event_type      TEXT    NOT NULL,
        payload         TEXT    NOT NULL,
        idempotency_key TEXT    NOT NULL UNIQUE,
        created_at      TEXT    NOT NULL,
        processed_at    TEXT,
        attempts        INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT
    ) STRICT
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ops_outbox_immutable
    BEFORE UPDATE OF event_type, payload, idempotency_key, created_at ON ops_outbox
    BEGIN
        SELECT RAISE(ABORT, 'ops_outbox 只追加:仅 processed_at/attempts/last_error 可改');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ops_outbox_no_delete
    BEFORE DELETE ON ops_outbox
    BEGIN
        SELECT RAISE(ABORT, 'ops_outbox 只追加:不允许 DELETE');
    END
    """,
    "CREATE INDEX IF NOT EXISTS idx_ops_outbox_pending ON ops_outbox(processed_at, id)",
    # ---- 审计:谁写的(user | agent | import | system) ----
    """
    CREATE TABLE IF NOT EXISTS ops_audit (
        id         INTEGER PRIMARY KEY,
        ts         TEXT NOT NULL,
        actor      TEXT NOT NULL CHECK (actor IN ('user', 'agent', 'import', 'system')),
        action     TEXT NOT NULL,
        target     TEXT,
        detail     TEXT,
        session_id TEXT
    ) STRICT
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ops_audit_no_update
    BEFORE UPDATE ON ops_audit
    BEGIN
        SELECT RAISE(ABORT, 'ops_audit 只追加:不允许 UPDATE');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ops_audit_no_delete
    BEFORE DELETE ON ops_audit
    BEGIN
        SELECT RAISE(ABORT, 'ops_audit 只追加:不允许 DELETE');
    END
    """,
    "CREATE INDEX IF NOT EXISTS idx_ops_audit_ts ON ops_audit(ts)",
    # ---- 幂等:key + 载荷指纹(能力域 §6.2④) ----
    """
    CREATE TABLE IF NOT EXISTS sys_idempotency (
        key         TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
        response    TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    ) STRICT
    """,
    # ---- 失败样本库(执行计划 §4.3):今天就开始攒,微调的唯一前提 ----
    """
    CREATE TABLE IF NOT EXISTS ops_failure_samples (
        id              INTEGER PRIMARY KEY,
        ts              TEXT NOT NULL,
        kind            TEXT NOT NULL CHECK (kind IN ('gate_blocked', 'exec_error', 'user_correction', 'other')),
        source          TEXT,
        input           TEXT,
        actual_output   TEXT,
        expected_output TEXT,
        note            TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_failure_samples_ts ON ops_failure_samples(ts)",
)

MIGRATION_1_MEMORY: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id               TEXT PRIMARY KEY,
        created_at       TEXT NOT NULL,
        title            TEXT,
        parent_session   TEXT,
        last_active_at   TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(last_active_at)",
    # 事件镜像:JSONL 是事实源,本表供范围查询与统计(可 DROP 后重放重建)
    """
    CREATE TABLE IF NOT EXISTS events (
        session_id TEXT    NOT NULL,
        seq        INTEGER NOT NULL,
        type       TEXT    NOT NULL,
        time       TEXT    NOT NULL,
        surface    INTEGER NOT NULL DEFAULT 1,
        data       TEXT    NOT NULL,
        PRIMARY KEY (session_id, seq)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_time ON events(session_id, time)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)",
    # 摘要池:派生索引,不是事实源;允许重建
    """
    CREATE TABLE IF NOT EXISTS summary_pool (
        id            INTEGER PRIMARY KEY,
        session_id    TEXT    NOT NULL,
        start_seq     INTEGER NOT NULL,
        end_seq       INTEGER NOT NULL,
        start_time    TEXT    NOT NULL,
        end_time      TEXT    NOT NULL,
        text          TEXT    NOT NULL,
        anchors       TEXT    NOT NULL DEFAULT '[]',
        tags          TEXT    NOT NULL DEFAULT '[]',
        category      TEXT    NOT NULL DEFAULT 'periodic'
                      CHECK (category IN ('temporary', 'periodic', 'permanent')),
        base_strength REAL    NOT NULL DEFAULT 1.0,
        visits        INTEGER NOT NULL DEFAULT 0,
        last_access   TEXT,
        heat_cache    REAL,
        supersedes    INTEGER REFERENCES summary_pool(id),
        created_at    TEXT    NOT NULL,
        UNIQUE (session_id, start_seq, end_seq)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_pool_heat ON summary_pool(heat_cache DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pool_range ON summary_pool(session_id, start_seq, end_seq)",
    # 永久事实(偏好/身份/决定)。矛盾消解:旧事实置 active=0 而不是删除。
    """
    CREATE TABLE IF NOT EXISTS facts (
        id            INTEGER PRIMARY KEY,
        session_id    TEXT,
        type          TEXT    NOT NULL DEFAULT 'other'
                      CHECK (type IN ('identity', 'preference', 'decision', 'other')),
        category      TEXT    NOT NULL DEFAULT 'permanent'
                      CHECK (category IN ('temporary', 'periodic', 'permanent')),
        content       TEXT    NOT NULL,
        keywords      TEXT    NOT NULL DEFAULT '[]',
        confidential  INTEGER NOT NULL DEFAULT 0,
        active        INTEGER NOT NULL DEFAULT 1,
        superseded_by INTEGER REFERENCES facts(id),
        created_at    TEXT    NOT NULL,
        updated_at    TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_facts_active ON facts(active, category)",
    # 能力探测结果(例如 FTS5 trigram 是否可用)—— 降级策略的依据
    """
    CREATE TABLE IF NOT EXISTS sys_capabilities (
        name      TEXT PRIMARY KEY,
        value     TEXT NOT NULL,
        probed_at TEXT NOT NULL
    ) STRICT
    """,
)

MIGRATION_2_MEMORY_CONSOLIDATION: tuple[str, ...] = (
    # 巩固/回填的幂等键:(session_id, start_seq, end_seq, rounds_after_reply)
    # 回复轮次作为版本,防止同一段证据被反复回填(设计深化 §1.5)
    """
    CREATE TABLE IF NOT EXISTS consolidations (
        id                 INTEGER PRIMARY KEY,
        session_id         TEXT    NOT NULL,
        start_seq          INTEGER NOT NULL,
        end_seq            INTEGER NOT NULL,
        rounds_after_reply INTEGER NOT NULL,
        summary_id         INTEGER,
        evidence_count     INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT    NOT NULL,
        UNIQUE (session_id, start_seq, end_seq, rounds_after_reply)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_consolidations_session ON consolidations(session_id)",
)

MIGRATION_3_MEMORY_TASKS: tuple[str, ...] = (
    # 长期目标:事件溯源(goal/change 全量快照)+ revision 做 CAS
    """
    CREATE TABLE IF NOT EXISTS goals (
        id             TEXT PRIMARY KEY,
        objective      TEXT    NOT NULL,
        phase          TEXT    NOT NULL DEFAULT 'active'
                       CHECK (phase IN ('active', 'paused', 'blocked', 'complete')),
        revision       INTEGER NOT NULL DEFAULT 1,
        blocked_reason TEXT,
        max_rounds     INTEGER,
        rounds_started INTEGER NOT NULL DEFAULT 0,
        created_at     TEXT    NOT NULL,
        updated_at     TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_goals_phase ON goals(phase, updated_at)",
    # 定时提醒。⚠️ every_seconds 的下限 300 秒是硬约束,写在 CHECK 里而不是提示词里
    """
    CREATE TABLE IF NOT EXISTS schedules (
        id            TEXT PRIMARY KEY,
        kind          TEXT    NOT NULL CHECK (kind IN ('after', 'at', 'every')),
        at            TEXT,
        every_seconds INTEGER,
        prompt        TEXT    NOT NULL,
        enabled       INTEGER NOT NULL DEFAULT 1,
        last_fired_at TEXT,
        created_at    TEXT    NOT NULL,
        updated_at    TEXT    NOT NULL,
        CHECK (kind <> 'every' OR (every_seconds IS NOT NULL AND every_seconds >= 300))
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled, kind)",
    # 待办
    """
    CREATE TABLE IF NOT EXISTS todos (
        id             TEXT PRIMARY KEY,
        content        TEXT    NOT NULL,
        status         TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'in_progress', 'completed')),
        source         TEXT    NOT NULL DEFAULT 'user',
        due_date_local TEXT,
        created_at     TEXT    NOT NULL,
        updated_at     TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, due_date_local)",
    # 派发记录:抢占式 INSERT 防重启重发(能力域 §5.2 的提醒写法,这里推广到全部定时派发)
    """
    CREATE TABLE IF NOT EXISTS dispatches (
        id         TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        payload    TEXT NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_dispatches_kind ON dispatches(kind, created_at)",
)

MIGRATION_2_ASSISTANT_LEDGER: tuple[str, ...] = (
    # 账户:五类(资产/负债/收入/支出/权益),借鉴 Beancount 的严格分类
    """
    CREATE TABLE IF NOT EXISTS app_accounts (
        id        INTEGER PRIMARY KEY,
        name      TEXT    NOT NULL UNIQUE,
        kind      TEXT    NOT NULL
                  CHECK (kind IN ('asset', 'liability', 'income', 'expense', 'equity')),
        parent_id INTEGER REFERENCES app_accounts(id),
        currency  TEXT    NOT NULL DEFAULT 'CNY',
        archived  INTEGER NOT NULL DEFAULT 0
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_categories (
        id        INTEGER PRIMARY KEY,
        name      TEXT    NOT NULL,
        kind      TEXT    NOT NULL DEFAULT 'expense' CHECK (kind IN ('expense', 'income')),
        parent_id INTEGER REFERENCES app_categories(id),
        archived  INTEGER NOT NULL DEFAULT 0
    ) STRICT
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name ON app_categories(name, kind)",
    # 交易头:用户眼中"一笔事"。status 是 AI 写入的保护阀:助手只能写 draft。
    """
    CREATE TABLE IF NOT EXISTS app_txns (
        id                INTEGER PRIMARY KEY,
        ts                TEXT    NOT NULL,
        payee             TEXT,
        narration         TEXT,
        source            TEXT    NOT NULL,
        media_id          INTEGER,
        status            TEXT    NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'confirmed', 'void')),
        idempotency_key   TEXT    UNIQUE,
        import_batch_id   INTEGER,
        dedup_key         TEXT,
        refund_of_txn_id  INTEGER REFERENCES app_txns(id),
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_txns_ts ON app_txns(ts)",
    "CREATE INDEX IF NOT EXISTS idx_txns_status ON app_txns(status, ts)",
    # 分录腿:每笔交易由 2 条或更多 posting 组成,**金额之和为零**(在 Python 层校验)
    """
    CREATE TABLE IF NOT EXISTS app_postings (
        id          INTEGER PRIMARY KEY,
        txn_id      INTEGER NOT NULL REFERENCES app_txns(id) ON DELETE CASCADE,
        account_id  INTEGER NOT NULL REFERENCES app_accounts(id),
        category_id INTEGER REFERENCES app_categories(id),
        amount_cents INTEGER NOT NULL,
        currency    TEXT    NOT NULL DEFAULT 'CNY'
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_postings_txn ON app_postings(txn_id)",
    "CREATE INDEX IF NOT EXISTS idx_postings_acct ON app_postings(account_id, category_id)",
    # 种子账户与分类(幂等:INSERT OR IGNORE 靠 UNIQUE 约束)
    "INSERT OR IGNORE INTO app_accounts(name, kind) VALUES ('现金', 'asset')",
    "INSERT OR IGNORE INTO app_accounts(name, kind) VALUES ('银行卡', 'asset')",
    "INSERT OR IGNORE INTO app_accounts(name, kind) VALUES ('支付宝', 'asset')",
    "INSERT OR IGNORE INTO app_accounts(name, kind) VALUES ('微信钱包', 'asset')",
    "INSERT OR IGNORE INTO app_accounts(name, kind) VALUES ('信用卡', 'liability')",
    "INSERT OR IGNORE INTO app_accounts(name, kind) VALUES ('收入', 'income')",
    "INSERT OR IGNORE INTO app_accounts(name, kind) VALUES ('支出', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('餐饮', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('交通', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('购物', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('居住', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('医疗', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('教育', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('娱乐', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('人情', 'expense')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('其他', 'expense')",
)

MIGRATION_3_ASSISTANT_IMPORT_RULES: tuple[str, ...] = (
    # 导入批次:file_sha256 唯一 -> 同一文件重复上传直接短路
    """
    CREATE TABLE IF NOT EXISTS app_import_batches (
        id            INTEGER PRIMARY KEY,
        source        TEXT    NOT NULL,
        file_name     TEXT,
        file_sha256   TEXT    NOT NULL UNIQUE,
        imported_at   TEXT    NOT NULL,
        row_count     INTEGER NOT NULL DEFAULT 0,
        imported_count INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        failed_count  INTEGER NOT NULL DEFAULT 0
    ) STRICT
    """,
    # 规则引擎:借鉴 Firefly III 的 trigger -> action。规则进库,不硬编码。
    """
    CREATE TABLE IF NOT EXISTS app_rules (
        id            INTEGER PRIMARY KEY,
        priority      INTEGER NOT NULL DEFAULT 100,
        name          TEXT,
        match_field   TEXT    NOT NULL
                      CHECK (match_field IN ('peer', 'item', 'method', 'amount', 'time_window')),
        match_op      TEXT    NOT NULL DEFAULT 'contains'
                      CHECK (match_op IN ('contains', 'equals', 'gt', 'lt', 'in_range')),
        match_value   TEXT    NOT NULL,
        target_account_id  INTEGER REFERENCES app_accounts(id),
        target_category_id INTEGER REFERENCES app_categories(id),
        add_tag       TEXT,
        origin        TEXT    NOT NULL DEFAULT 'manual'
                      CHECK (origin IN ('manual', 'learned')),
        enabled       INTEGER NOT NULL DEFAULT 1,
        hit_count     INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT    NOT NULL,
        updated_at    TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_rules_priority ON app_rules(enabled, priority, id)",
    # 预算(设计 §5.1);本轮只落表与查询口径,预警留到 M6
    """
    CREATE TABLE IF NOT EXISTS app_budgets (
        id          INTEGER PRIMARY KEY,
        category_id INTEGER REFERENCES app_categories(id),
        period      TEXT    NOT NULL,
        limit_cents INTEGER NOT NULL,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        UNIQUE (category_id, period)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_txns_dedup ON app_txns(source, dedup_key)",
    "CREATE INDEX IF NOT EXISTS idx_txns_refund ON app_txns(refund_of_txn_id)",
)

MIGRATION_4_ASSISTANT_CALENDAR: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS app_calendars (
        id     INTEGER PRIMARY KEY,
        name   TEXT NOT NULL UNIQUE,
        color  TEXT,
        source TEXT NOT NULL DEFAULT 'local'
    ) STRICT
    """,
    # ⚠️ rrule 存 **RFC5545 原文**,不自己解析。展开是查询时行为,不是存储结构。
    """
    CREATE TABLE IF NOT EXISTS app_events (
        id          INTEGER PRIMARY KEY,
        calendar_id INTEGER REFERENCES app_calendars(id),
        uid         TEXT    NOT NULL UNIQUE,
        title       TEXT    NOT NULL,
        description TEXT,
        location    TEXT,
        dtstart     TEXT    NOT NULL,
        dtend       TEXT,
        all_day     INTEGER NOT NULL DEFAULT 0,
        tzid        TEXT    NOT NULL DEFAULT 'Asia/Shanghai',
        rrule       TEXT,
        reminder_minutes INTEGER NOT NULL DEFAULT 30,
        status      TEXT    NOT NULL DEFAULT 'confirmed'
                    CHECK (status IN ('confirmed', 'cancelled')),
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_start ON app_events(dtstart)",
    # 重复事件的例外必须单独建表 —— 这是日历实现最大的坑。
    # 三种例外在这里用 range_mode 区分:
    #   single        -> RECURRENCE-ID,只覆盖/取消某一次
    #   thisandfuture -> RANGE=THISANDFUTURE,此次及以后都改
    #   (EXDATE 用 is_cancelled=1 的单次覆盖表示,与 RFC 语义一致)
    """
    CREATE TABLE IF NOT EXISTS app_event_overrides (
        id            INTEGER PRIMARY KEY,
        event_id      INTEGER NOT NULL REFERENCES app_events(id) ON DELETE CASCADE,
        recurrence_id TEXT    NOT NULL,
        is_cancelled  INTEGER NOT NULL DEFAULT 0,
        range_mode    TEXT    NOT NULL DEFAULT 'single'
                      CHECK (range_mode IN ('single', 'thisandfuture')),
        dtstart       TEXT,
        dtend         TEXT,
        title         TEXT,
        location      TEXT,
        description   TEXT,
        created_at    TEXT    NOT NULL,
        UNIQUE (event_id, recurrence_id, range_mode)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_overrides_event ON app_event_overrides(event_id)",
    # 提醒实例:物化未来 N 次 + 抢占式更新防重启重发(能力域 §5.2)
    """
    CREATE TABLE IF NOT EXISTS app_reminder_instances (
        id               INTEGER PRIMARY KEY,
        event_uid        TEXT    NOT NULL,
        recurrence_id    TEXT    NOT NULL,
        alarm_trigger    TEXT    NOT NULL DEFAULT '-PT30M',
        fire_at_utc      TEXT    NOT NULL,
        fired_at         TEXT,
        dismissed_at     TEXT,
        snooze_until_utc TEXT,
        created_at       TEXT    NOT NULL,
        UNIQUE (event_uid, recurrence_id, alarm_trigger)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_reminders_due ON app_reminder_instances(fired_at, fire_at_utc)",
    "INSERT OR IGNORE INTO app_calendars(name, color, source) VALUES ('默认', '#3b82f6', 'local')",
)

MIGRATION_5_ASSISTANT_STUDY: tuple[str, ...] = (
    # 媒体资产:内容寻址(sha256),数据库只存路径不存 BLOB(能力域 §2)
    """
    CREATE TABLE IF NOT EXISTS app_assets (
        id            INTEGER PRIMARY KEY,
        sha256        TEXT    NOT NULL UNIQUE,
        rel_path      TEXT    NOT NULL,
        media_type    TEXT    NOT NULL DEFAULT 'image/jpeg',
        byte_size     INTEGER NOT NULL,
        width         INTEGER,
        height        INTEGER,
        original_name TEXT,
        created_at    TEXT    NOT NULL
    ) STRICT
    """,
    # 知识点:闭集 + 别名(避免"分数的加减"与"异分母分数加减"分裂成两个)
    """
    CREATE TABLE IF NOT EXISTS app_knowledge_points (
        id        INTEGER PRIMARY KEY,
        name      TEXT    NOT NULL,
        subject   TEXT,
        grade     TEXT,
        parent_id INTEGER REFERENCES app_knowledge_points(id),
        code      TEXT,
        created_at TEXT   NOT NULL,
        UNIQUE (name, subject)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_kp_aliases (
        alias TEXT PRIMARY KEY,
        kp_id INTEGER NOT NULL REFERENCES app_knowledge_points(id) ON DELETE CASCADE
    ) STRICT
    """,
    # 内容层:一道题就是一道题(改内容不重置复习进度)
    """
    CREATE TABLE IF NOT EXISTS app_problems (
        id             INTEGER PRIMARY KEY,
        subject        TEXT    NOT NULL,
        grade          TEXT,
        source         TEXT,
        stem_text      TEXT,
        answer_text    TEXT,
        explanation    TEXT,
        asset_ids      TEXT    NOT NULL DEFAULT '[]',
        ocr_confidence REAL,
        status         TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'confirmed', 'archived')),
        created_at     TEXT    NOT NULL,
        updated_at     TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_problems_subject ON app_problems(subject, status)",
    """
    CREATE TABLE IF NOT EXISTS app_problem_kps (
        problem_id INTEGER NOT NULL REFERENCES app_problems(id) ON DELETE CASCADE,
        kp_id      INTEGER NOT NULL REFERENCES app_knowledge_points(id) ON DELETE CASCADE,
        weight     REAL    NOT NULL DEFAULT 1.0,
        PRIMARY KEY (problem_id, kp_id)
    ) STRICT
    """,
    # ⭐ 复习单元 = 调度单元。一道题按知识点拆成多个单元 —— 这不是优化,是能不能归因的问题:
    #    只挂标签的话,孩子答错时你分不清他卡在"通分"还是"约分"。
    """
    CREATE TABLE IF NOT EXISTS app_review_units (
        id               INTEGER PRIMARY KEY,
        problem_id       INTEGER NOT NULL REFERENCES app_problems(id) ON DELETE CASCADE,
        ord              INTEGER NOT NULL DEFAULT 0,
        kp_id            INTEGER REFERENCES app_knowledge_points(id),
        stability        REAL    NOT NULL DEFAULT 0,
        difficulty       REAL    NOT NULL DEFAULT 0,
        fsrs_state       TEXT    NOT NULL DEFAULT 'new'
                         CHECK (fsrs_state IN ('new', 'learning', 'review', 'relearning')),
        reps             INTEGER NOT NULL DEFAULT 0,
        lapses           INTEGER NOT NULL DEFAULT 0,
        last_review_utc  TEXT,
        due_utc          TEXT    NOT NULL,
        due_date_local   TEXT    NOT NULL,
        suspended        INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL,
        UNIQUE (problem_id, ord)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_units_due ON app_review_units(suspended, due_date_local, fsrs_state)",
    # 复习日志只增不改:未来训练个人 FSRS 参数的唯一依据(需约 1000 条)
    """
    CREATE TABLE IF NOT EXISTS app_review_logs (
        id                 INTEGER PRIMARY KEY,
        unit_id            INTEGER NOT NULL REFERENCES app_review_units(id) ON DELETE CASCADE,
        problem_id         INTEGER NOT NULL,
        ts_utc             TEXT    NOT NULL,
        rating             INTEGER NOT NULL,
        interval_days      REAL,
        last_interval_days REAL,
        elapsed_ms         INTEGER,
        state_before       TEXT,
        state_after        TEXT,
        source             TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_rlog_unit ON app_review_logs(unit_id, ts_utc)",
    # 录入会话:AI 结果先进候选区,人工确认后才进正式表
    """
    CREATE TABLE IF NOT EXISTS app_intake_sessions (
        id         INTEGER PRIMARY KEY,
        profile_id INTEGER,
        created_at TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'reviewing', 'done', 'abandoned'))
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_intake_candidates (
        id           INTEGER PRIMARY KEY,
        session_id   INTEGER NOT NULL REFERENCES app_intake_sessions(id) ON DELETE CASCADE,
        asset_id     INTEGER REFERENCES app_assets(id),
        region_json  TEXT,
        payload_json TEXT    NOT NULL,
        confidence   REAL,
        decision     TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (decision IN ('pending', 'accepted', 'rejected', 'edited')),
        created_at   TEXT    NOT NULL
    ) STRICT
    """,
)

MIGRATION_6_ASSISTANT_HEALTH: tuple[str, ...] = (
    # 档案:算 BMR 需要身高/年龄/性别,设计里没给这些一个落点。
    # 单行表(UNIQUE(name) 保证),不做多用户。
    """
    CREATE TABLE IF NOT EXISTS app_profiles (
        id             INTEGER PRIMARY KEY,
        name           TEXT NOT NULL DEFAULT '本人' UNIQUE,
        sex            TEXT NOT NULL DEFAULT '男' CHECK (sex IN ('男', '女')),
        birth_year     INTEGER,
        height_cm      REAL,
        activity_level TEXT NOT NULL DEFAULT 'sedentary'
                       CHECK (activity_level IN
                              ('sedentary', 'light', 'moderate', 'active', 'very_active')),
        updated_at     TEXT NOT NULL
    ) STRICT
    """,
    "INSERT OR IGNORE INTO app_profiles(name, sex, activity_level, updated_at) VALUES ('本人','男','sedentary','1970-01-01T00:00:00+00:00')",
    # 指标字典:泛化测量表的关键(借鉴 wger)。加新指标零成本,不新建表。
    """
    CREATE TABLE IF NOT EXISTS app_metric_types (
        id          INTEGER PRIMARY KEY,
        code        TEXT    NOT NULL UNIQUE,
        name        TEXT    NOT NULL,
        unit        TEXT    NOT NULL,
        parent_code TEXT,
        is_derived  INTEGER NOT NULL DEFAULT 0,
        decimals    INTEGER NOT NULL DEFAULT 1
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_measurements (
        id          INTEGER PRIMARY KEY,
        metric_code TEXT    NOT NULL REFERENCES app_metric_types(code),
        value       REAL    NOT NULL,
        unit        TEXT    NOT NULL,
        measured_at TEXT    NOT NULL,
        local_date  TEXT    NOT NULL,
        source      TEXT    NOT NULL DEFAULT 'user',
        note        TEXT,
        created_at  TEXT    NOT NULL,
        UNIQUE (metric_code, measured_at, source)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_meas ON app_measurements(metric_code, local_date)",
    # 运动类型与强度系数单独成表:调整系数不动历史数据
    """
    CREATE TABLE IF NOT EXISTS app_activity_types (
        id       INTEGER PRIMARY KEY,
        name     TEXT    NOT NULL UNIQUE,
        met      REAL    NOT NULL,
        category TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_workouts (
        id             INTEGER PRIMARY KEY,
        ts             TEXT    NOT NULL,
        local_date     TEXT    NOT NULL,
        kind           TEXT    NOT NULL,
        activity_id    INTEGER REFERENCES app_activity_types(id),
        duration_min   INTEGER,
        distance_km    REAL,
        note           TEXT,
        created_at     TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_workouts_date ON app_workouts(local_date)",
    # 自己的常用食物库 —— 这才是真正的数据资产,不依赖外部表
    """
    CREATE TABLE IF NOT EXISTS app_foods (
        id           INTEGER PRIMARY KEY,
        name         TEXT    NOT NULL,
        unit         TEXT    NOT NULL DEFAULT '份',
        kcal_per_unit REAL,
        protein_g    REAL,
        carb_g       REAL,
        fat_g        REAL,
        source       TEXT    NOT NULL DEFAULT 'manual',
        external_ref TEXT,
        cached_at    TEXT,
        UNIQUE (name, unit)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_food_logs (
        id         INTEGER PRIMARY KEY,
        ts         TEXT    NOT NULL,
        local_date TEXT    NOT NULL,
        meal_type  TEXT,
        food_id    INTEGER REFERENCES app_foods(id),
        amount     REAL    NOT NULL DEFAULT 1,
        kcal       REAL,
        note       TEXT,
        media_id   INTEGER,
        created_at TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_food_logs_date ON app_food_logs(local_date)",
    """
    CREATE TABLE IF NOT EXISTS app_health_goals (
        id           INTEGER PRIMARY KEY,
        metric       TEXT    NOT NULL,
        target_value REAL    NOT NULL,
        start_value  REAL,
        target_date  TEXT,
        status       TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'done', 'abandoned')),
        created_at   TEXT    NOT NULL
    ) STRICT
    """,
    # 种子指标与运动类型
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals) VALUES ('weight','体重','kg',1)",
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals) VALUES ('body_fat','体脂率','%',1)",
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals) VALUES ('waist','腰围','cm',1)",
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals) VALUES ('resting_hr','静息心率','bpm',0)",
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals) VALUES ('bp_sys','收缩压','mmHg',0)",
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals) VALUES ('bp_dia','舒张压','mmHg',0)",
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals) VALUES ('sleep_hours','睡眠时长','h',1)",
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals) VALUES ('steps','步数','步',0)",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('走路',3.5,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('快走',4.3,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('跑步',8.3,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('骑车',6.8,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('游泳',7.0,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('跳绳',11.0,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('力量训练',5.0,'力量')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('瑜伽',2.5,'柔韧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('家务',3.0,'日常')",
)

# --- M6:软删除 / 撤销 / 只读开关 --------------------------------------------
# 为什么是软删除而不是 DELETE:
#   账本是"领域数据",误删不可接受。deleted_at 一列换回"按下葫芦浮起瓢"的能力。
# 为什么读走视图:
#   app_txns 是包含已删行的**全量**表;app_txns_live 才是"当前现实"。
#   读走视图、写走表 —— 写错一边编译器不会说话,但测试会。
def _add_column(table: str, column: str, ddl: str):
    """把一个"加列"包成**幂等**的迁移项(实现见文件末尾的 add_column_if_missing)。

    ⚠️ 2026-10-17 审计:v7 原来是两条裸 ALTER。ALTER 的 DDL 不进事务,于是
    "第一条加完了、版本号还没写"时断电 → 之后**每次开连接**都抛 duplicate column,
    Web / CLI / 微信桥全部打不开,只能手工改库。加列本来是一次性的事,
    写成幂等的成本几乎为零。
    """
    def apply(conn: sqlite3.Connection) -> None:
        add_column_if_missing(conn, table, column, ddl)

    return apply


MIGRATION_7_ASSISTANT_SAFETY: tuple[str | Callable[[sqlite3.Connection], None], ...] = (
    # STRICT 表允许 ADD COLUMN,前提是默认值是常量(NULL / 1 都是)。
    _add_column("app_txns", "deleted_at", "deleted_at TEXT"),
    _add_column("app_txns", "version", "version INTEGER NOT NULL DEFAULT 1"),
    "CREATE INDEX IF NOT EXISTS idx_txns_live ON app_txns(deleted_at, ts)",
    "CREATE VIEW IF NOT EXISTS app_txns_live AS SELECT * FROM app_txns WHERE deleted_at IS NULL",
    # 撤销日志:每一步可逆操作都记下"怎么退回去"。
    # 与审计日志的区别:审计是"谁做了什么"(只增不改);撤销是"怎么退回去"(可标 used)。
    """
    CREATE TABLE IF NOT EXISTS ops_undo_log (
        id          INTEGER PRIMARY KEY,
        ts          TEXT    NOT NULL,
        actor       TEXT    NOT NULL DEFAULT 'user',
        kind        TEXT    NOT NULL,
        label       TEXT    NOT NULL,
        table_name  TEXT    NOT NULL,
        row_id      INTEGER NOT NULL,
        before_json TEXT    NOT NULL,
        after_json  TEXT,
        used_at     TEXT,
        session_id  TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_undo_recent ON ops_undo_log(used_at, id DESC)",
    # 撤销日志也是证据的一部分 —— 和审计一样只追加。
    """
    CREATE TRIGGER IF NOT EXISTS trg_undo_no_update
    BEFORE UPDATE ON ops_undo_log
    WHEN NOT (OLD.used_at IS NULL AND NEW.used_at IS NOT NULL)
    BEGIN
        SELECT RAISE(ABORT, '撤销日志只追加,不允许改写');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_undo_no_delete
    BEFORE DELETE ON ops_undo_log
    BEGIN
        SELECT RAISE(ABORT, '撤销日志只追加,不允许删除');
    END
    """,
)

# --- M7(前端反馈):统一写入回执 ------------------------------------------
# 为什么要有这张表(而不是让前端自己记):
#   * **写入反馈是项目级机制,不是某个域的** —— 四个域(账本/日历/健康/错题)与
#     事项层共用同一张表、同一个撤销接口、同一个前端组件;
#   * 领域服务在**自己那个事务里**写好回执,所以"写进去了但前端不知道"不可能发生;
#   * 撤销语义由领域服务提供(reverter 按 kind 注册),表只负责"只写一次、只撤一次"。
# 与 ops_audit / ops_undo_log 的分工:
#   ops_audit    = 谁在什么时候做了什么(不可变的行为记录,给人查)
#   ops_receipts = 刚才那一下**写了什么** + 怎么退回去(给用户看的回执,可标记已撤销)
MIGRATION_8_ASSISTANT_RECEIPTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ops_receipts (
        id          INTEGER PRIMARY KEY,
        ts          TEXT    NOT NULL,
        actor       TEXT    NOT NULL DEFAULT 'agent',
        kind        TEXT    NOT NULL,
        domain      TEXT    NOT NULL,
        label       TEXT    NOT NULL,
        detail      TEXT,
        undo_plan   TEXT,
        undoable    INTEGER NOT NULL DEFAULT 1,
        undone_at   TEXT,
        undone_note TEXT,
        session_id  TEXT,
        turn        INTEGER
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_receipts_recent ON ops_receipts(id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_receipts_open ON ops_receipts(undone_at, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_receipts_session ON ops_receipts(session_id, id DESC)",
    # 回执和审计一样是证据:只追加,唯一允许的改写是"标成已撤销"。
    """
    CREATE TRIGGER IF NOT EXISTS trg_receipts_no_update
    BEFORE UPDATE ON ops_receipts
    WHEN NOT (OLD.undone_at IS NULL AND NEW.undone_at IS NOT NULL)
    BEGIN
        SELECT RAISE(ABORT, '写入回执只追加,仅允许标记已撤销');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_receipts_no_delete
    BEFORE DELETE ON ops_receipts
    BEGIN
        SELECT RAISE(ABORT, '写入回执只追加:不允许 DELETE');
    END
    """,
)

# --- M9(统一反馈机制):让模型也能问用户一句 -------------------------------
# 为什么落库(而不是只留在内存里):
#   * **等待发生在 SSE 那一轮,回答从另一个请求进来** —— 进程内的登记处负责唤醒,
#     但这张表负责「刷新页面之后,用户还看得见它在等什么」;
#   * 问题与答案都是证据:事后能回答「它当时问了什么、我答了什么、它就照做了吗」。
# 与 ops_receipts 的分工:
#   ops_receipts = 它写完了什么(事后告知 + 撤销)
#   ops_questions = 它在等你拍板什么(事中提问 + 回答)
MIGRATION_9_ASSISTANT_FEEDBACK: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ops_questions (
        id          TEXT PRIMARY KEY,
        ts          TEXT    NOT NULL,
        expires_at  TEXT    NOT NULL DEFAULT '',
        session_id  TEXT,
        domain      TEXT    NOT NULL DEFAULT 'system',
        source      TEXT    NOT NULL DEFAULT 'agent',
        prompt      TEXT    NOT NULL,
        options     TEXT    NOT NULL DEFAULT '[]',
        context     TEXT    NOT NULL DEFAULT '{}',
        allow_text  INTEGER NOT NULL DEFAULT 1,
        multi       INTEGER NOT NULL DEFAULT 0,
        status      TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'answered', 'expired', 'cancelled')),
        answer      TEXT,
        answered_at TEXT,
        answered_by TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_questions_pending ON ops_questions(status, ts DESC)",
    # 收入分类的种子。之前只有支出分类 —— 于是"收入"这件事在账本里根本没有落脚点。
    # 名字与 categories.py 的关键词表一一对应,别在代码里另起一套名字。
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('工资', 'income')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('奖金', 'income')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('报销', 'income')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('退款', 'income')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('红包收入', 'income')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('理财', 'income')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('兼职', 'income')",
    "INSERT OR IGNORE INTO app_categories(name, kind) VALUES ('其他收入', 'income')",
    "CREATE INDEX IF NOT EXISTS idx_questions_session ON ops_questions(session_id, ts DESC)",
    # 一个问题只有一个答案:只允许 pending -> 终态,且答案不可再改。
    # 这条不变量由**数据库**兜住,而不是靠调用方自觉 —— 两个浏览器同时点也不怕。
    """
    CREATE TRIGGER IF NOT EXISTS trg_questions_answer_once
    BEFORE UPDATE ON ops_questions
    WHEN NOT (OLD.status = 'pending' AND NEW.status IN ('answered', 'expired', 'cancelled'))
    BEGIN
        SELECT RAISE(ABORT, '一个问题只能被回答一次:answer 不可改写');
    END
    """,
)

# --- M10(错题可用化):学生档案 / 拍照录入 / 每日复习推送 ---------------------
# 三个问题的落点,都是"数据放哪"的问题,而不是"界面怎么写"的问题:
#   ① 错题的对象是**学生**,不是"本人"。健康档案(app_profiles)里那套身高/性别/BMR
#      是给本人算代谢用的,把学生塞进去会让两张表互相污染 —— 所以另起 app_students。
#   ② 拍照录入必须经过"候选区":模型识别错了要能改,识别挂了要能手填。
#      app_intake_sessions / app_intake_candidates 在 M4.3a 就建好了(M4.3b 一直没做),
#      这里补上识别用的两列(哪个模型认的、它原话说了什么)—— 排错时最想知道这两件事。
#   ③ 推送时间必须**落库**,不能靠模型"记得":确定性调度器要读它,重启后还得一样。
MIGRATION_10_ASSISTANT_STUDY_V2: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    """
    CREATE TABLE IF NOT EXISTS app_students (
        id         INTEGER PRIMARY KEY,
        name       TEXT    NOT NULL UNIQUE,
        grade      TEXT,
        note       TEXT,
        is_default INTEGER NOT NULL DEFAULT 0,
        archived   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT    NOT NULL
    ) STRICT
    """,
    # 默认学生:老库升级上来也得有一个,否则新录入的错题无处归属。
    "INSERT OR IGNORE INTO app_students(name, is_default, created_at)"
    " VALUES ('孩子', 1, '1970-01-01T00:00:00+00:00')",
    # 归属列**可空**:历史错题保持 NULL(未指定学生),不去猜它是谁的。
    lambda conn: add_column_if_missing(
        conn, "app_problems", "student_id",
        "student_id INTEGER REFERENCES app_students(id)",
    ),
    "CREATE INDEX IF NOT EXISTS idx_problems_student ON app_problems(student_id, status)",
    # 单行设置表。CHECK (id = 1) 是"只有一行"这条不变量的**数据库**表达,不是约定。
    """
    CREATE TABLE IF NOT EXISTS app_study_settings (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        student_id       INTEGER REFERENCES app_students(id),
        push_enabled     INTEGER NOT NULL DEFAULT 1,
        push_time_local  TEXT    NOT NULL DEFAULT '19:30',
        push_daily_limit INTEGER NOT NULL DEFAULT 10,
        -- 拍照识别时是否让模型顺带补全答案与解析。默认**关**:先忠于照片。
        solve_on_recognize INTEGER NOT NULL DEFAULT 0,
        updated_at       TEXT    NOT NULL
    ) STRICT
    """,
    lambda conn: add_column_if_missing(conn, "app_intake_candidates", "model", "model TEXT"),
    lambda conn: add_column_if_missing(conn, "app_intake_candidates", "raw_text", "raw_text TEXT"),
    lambda conn: add_column_if_missing(
        conn, "app_intake_candidates", "problem_id",
        "problem_id INTEGER REFERENCES app_problems(id)",
    ),
)

# --- M11(手机优先):素材层与产物层 -------------------------------------------
# 用户的原话是"我大部分时间用手机,拍完错题直接微信传过来"。这句话落到数据上就是
# **两条方向相反的通道**,而且必须各有各的家:
#
#   素材层(用户来的东西) → 照片/文件先原样存下来,**绝不因为"没认出来"就丢**;
#   产物层(助手做出来的东西) → 错题卷/统计/清单落盘并登记,能被下载、也能发回手机。
#
# 与既有 app_assets / data/media 的关系:那是**内容寻址的字节仓**(同一张照片只存一份),
# 这里记的是**来源与去向**(哪个渠道、哪条消息、最后变成了什么)。两者分工,不重叠。
#
# ⚠️ 为什么素材要记 channel + external_id 并做 UNIQUE:微信会重发回调,
#    同一条消息入两次素材 = 同一张错题照片录两道题(实测过类似的重复现象)。
MIGRATION_11_ASSISTANT_MATERIALS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS app_materials (
        id            INTEGER PRIMARY KEY,
        channel       TEXT    NOT NULL DEFAULT 'local',
        external_id   TEXT,
        sender        TEXT,
        thread_id     TEXT,
        asset_id      INTEGER REFERENCES app_assets(id),
        original_name TEXT,
        media_type    TEXT    NOT NULL DEFAULT 'application/octet-stream',
        byte_size     INTEGER NOT NULL DEFAULT 0,
        sha256        TEXT    NOT NULL DEFAULT '',
        kind          TEXT    NOT NULL DEFAULT 'file'
                      CHECK (kind IN ('image', 'file', 'voice', 'text')),
        note          TEXT,
        student_id    INTEGER REFERENCES app_students(id),
        handled       TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (handled IN ('pending', 'recorded', 'stored', 'ignored', 'failed')),
        result_json   TEXT    NOT NULL DEFAULT '{}',
        created_at    TEXT    NOT NULL
    ) STRICT
    """,
    # ⚠️ external_id 为 NULL 时 SQLite 认为彼此**不相等**,所以本地上传(没有渠道消息号)
    #    不会被这条唯一约束误伤 —— 正是想要的行为。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_channel_msg"
    " ON app_materials(channel, external_id)",
    "CREATE INDEX IF NOT EXISTS idx_materials_recent ON app_materials(created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_materials_handled ON app_materials(handled, id DESC)",
    # 产物层:助手做出来、要交到用户手上的东西。
    # id = "f" + sha1(相对路径)[:12] —— 句柄与文件一一对应,登记/查归属/下载共用同一个算法。
    """
    CREATE TABLE IF NOT EXISTS app_artifacts (
        id         TEXT PRIMARY KEY,
        kind       TEXT    NOT NULL DEFAULT 'text',
        rel_path   TEXT    NOT NULL,
        label      TEXT    NOT NULL DEFAULT '',
        media_type TEXT    NOT NULL DEFAULT 'application/octet-stream',
        byte_size  INTEGER NOT NULL DEFAULT 0,
        meta       TEXT    NOT NULL DEFAULT '{}',
        session_id TEXT,
        created_at TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_artifacts_recent ON app_artifacts(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_session ON app_artifacts(session_id, created_at DESC)",
)

# --- M12:运行期开关(聊天页那个「联网」按钮)--------------------------------
# ⚠️ 为什么是一张**通用键值表**而不是给某个域加一列:开关会越来越多
#    (联网、允许语音、允许抓取……),而它们与任何领域数据都无关。
#    也刻意**不写进 settings.yaml**:那是"启动配置",而这是"运行中想改就改"的东西 ——
#    改文件要重启,点按钮不该要重启。
MIGRATION_12_ASSISTANT_SETTINGS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sys_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
)

# --- M14:错题卷(答案来源 + 图形) -----------------------------------------
# ⭐ 为什么要把 `student_answer` 与 `answer_text` **分家**:
#    在此之前,提示词要求把"照片上出现的答案或批改"抄进 answer_text —— 而照片上
#    那行字多半就是**孩子的错答**(实测:错题 #5 抄进来的是"孩子第一次算的 65 分钟
#    + 老师订正 54 分钟"两段混在一起)。字段混用 → 错答案被当成标准答案印到卷子上。
#    M14 起:照片上写了什么只进 student_answer;answer_text 只装**模型独立解出/
#    老师批改/家长手填**的答案,并用 answer_source + answer_status 标明它是哪一种、
#    可不可信。老数据一律 answer_source='legacy'(**不假装它可信**)。
MIGRATION_13_ASSISTANT_STUDY_V3: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    lambda conn: add_column_if_missing(conn, "app_problems", "student_answer", "student_answer TEXT"),
    lambda conn: add_column_if_missing(conn, "app_problems", "answer_source", "answer_source TEXT"),
    lambda conn: add_column_if_missing(conn, "app_problems", "answer_status", "answer_status TEXT"),
    lambda conn: add_column_if_missing(conn, "app_problems", "solution_steps", "solution_steps TEXT"),
    # 图形:裁剪图存素材层(内容寻址去重),这里只存 id 与"当初框在哪"
    lambda conn: add_column_if_missing(conn, "app_problems", "figure_asset_id", "figure_asset_id INTEGER"),
    lambda conn: add_column_if_missing(conn, "app_problems", "figure_source", "figure_source TEXT"),
    lambda conn: add_column_if_missing(conn, "app_problems", "figure_bbox", "figure_bbox TEXT"),
    lambda conn: add_column_if_missing(conn, "app_problems", "figure_spec", "figure_spec TEXT"),
    # 老数据回填:有答案的标 legacy(来历不明),没答案的留空 —— 两者都不许冒充"已确认"
    "UPDATE app_problems SET answer_source = 'legacy'"
    " WHERE answer_source IS NULL AND answer_text IS NOT NULL AND TRIM(answer_text) <> ''",
    "UPDATE app_problems SET answer_status = 'unverified'"
    " WHERE answer_status IS NULL AND answer_source IS NOT NULL",
)

# --- M14b:老师批改的那一票 ----------------------------------------------
# 三方对照(模型 / 老师 / 孩子)里,老师那一票是**比模型更可信**的,所以要单独一列。
# 之前这些字被混在 answer_text 里(实测 #3 是"7;订:8"),对不出"谁写的什么"。
MIGRATION_14_ASSISTANT_ANSWER_TEACHER: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    lambda conn: add_column_if_missing(conn, "app_problems", "teacher_answer", "teacher_answer TEXT"),
    lambda conn: add_column_if_missing(conn, "app_problems", "answer_note", "answer_note TEXT"),
    "CREATE INDEX IF NOT EXISTS idx_problems_answer_status ON app_problems(answer_status)",
)

# --- M14c:默认开启"录入即解题" ------------------------------------------
# `solve_on_recognize` 这一列是 M10 加的,当时的语义是"照片上没答案才顺带解一下",默认**关**。
# M14 起语义变成"录完自动产出标准答案与图形"(答案不再采信照片上的字),默认**开**。
# ⚠️ 只改**已经有设置行**的库:没这行的库走代码里的默认值(True),不去凭空插一行。
MIGRATION_15_ASSISTANT_AUTO_SOLVE: tuple[str, ...] = (
    "UPDATE app_study_settings SET solve_on_recognize = 1 WHERE solve_on_recognize = 0",
)

# --- M14d:把"声音"也变成错题自己的素材 --------------------------------------
# 用户 2026-10-13 拍板:听力/听写题的音频**在录入时就生成一次,绑定在题上**。
# ⭐ 理由是"错题要按记忆曲线反复学"这件事本身:
#    ① 每次复习现做一遍 → 每次念出来的**都不是同一段声音**(音色/语速/停顿都会变),
#       孩子听到的东西不稳定,家长也无从判断"他到底听的是哪一版";
#    ② 每复习一次都要让模型重新判断"要不要出声、念什么"再合成 → 白花 token 与语音费;
#    ③ 绑定之后,出卷只读库与产物目录 —— 模型欠费、断网照样有声音。
# ⚠️ 存的是产物层的**字符串句柄**(音频进产物目录,不走素材层):audio_spec 里带
#    kind/text/repeat/gap 与音色、秒数,万一产物丢了也能不调模型重新合成同一段。
MIGRATION_16_ASSISTANT_PROBLEM_AUDIO: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    lambda conn: add_column_if_missing(conn, "app_problems", "audio_artifact_id", "audio_artifact_id TEXT"),
    lambda conn: add_column_if_missing(conn, "app_problems", "audio_spec", "audio_spec TEXT"),
)

# --- D147:撤销可以"先认领、再动数据"了 ---------------------------------------
# 旧触发器只允许 undone_at 从 NULL 变成非 NULL。于是撤销只能"先动数据、后认领":
# 两个入口(网页 + 微信、或两个标签页)同时点撤销时,双方都读到 undone=False,
# 都会执行一遍 reverter,而"已经撤过"的错是在**副作用之后**才抛出来的。
#
# 要支持"认领在副作用之前",就必须允许两种**很窄**的额外改写:
#   ① 认领失败时还回去(还带着认领标记的行 -> NULL);
#   ② 认领成功后补上说明(标记 -> 真实说明,undone_at 不动)。
# 仍然禁止:改 undone_at 的时间、改别的列、把**已经撤完**的回执再收回。
_RECEIPTS_UPDATE_GUARD = """
CREATE TRIGGER trg_receipts_no_update
BEFORE UPDATE ON ops_receipts
WHEN NOT (
    (OLD.undone_at IS NULL AND NEW.undone_at IS NOT NULL)
    OR (OLD.undone_at IS NOT NULL AND NEW.undone_at IS NULL
        AND OLD.undone_note = '撤销中')
    OR (OLD.undone_at = NEW.undone_at AND OLD.undone_at IS NOT NULL
        AND NEW.undone_note <> OLD.undone_note AND OLD.undone_note = '撤销中')
)
BEGIN
    SELECT RAISE(ABORT, '写入回执只追加,仅允许标记或收回撤销');
END
"""

MIGRATION_17_ASSISTANT_UNDO_CLAIM: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_receipts_no_update",
    _RECEIPTS_UPDATE_GUARD,
)

#: 家庭版成员与渠道绑定。一个安装 = 一个家庭;成员只是"谁在用",不是租户。
#: 会话 id 用 `member:<id>` 表达记忆/事件的分片键(见 la/family/identity.py)。
MIGRATION_18_ASSISTANT_MEMBERS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS app_members (
        id          INTEGER PRIMARY KEY,
        name        TEXT    NOT NULL,
        role        TEXT    NOT NULL DEFAULT 'parent'
                    CHECK (role IN ('admin', 'parent', 'child')),
        active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at  TEXT    NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_member_bindings (
        channel     TEXT    NOT NULL,
        external_id TEXT    NOT NULL,
        member_id   INTEGER NOT NULL REFERENCES app_members(id),
        created_at  TEXT    NOT NULL,
        PRIMARY KEY (channel, external_id)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_member_bindings_member"
    " ON app_member_bindings(member_id)",
    """
    INSERT INTO app_members(id, name, role, active, created_at)
    SELECT 1, '管理员', 'admin', 1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE NOT EXISTS (SELECT 1 FROM app_members)
    """,
)

def _assistant_member_auth(conn: sqlite3.Connection) -> None:
    """成员 PIN 列(幂等加列;旧库升级不重建 app_members)。"""
    add_column_if_missing(conn, "app_members", "pin_hash", "pin_hash TEXT")
    add_column_if_missing(conn, "app_members", "pin_salt", "pin_salt TEXT")
    add_column_if_missing(conn, "app_members", "pin_updated_at", "pin_updated_at TEXT")


#: A 阶段身份层:成员 PIN + 可撤销的登录票据。
#: 票据只存 SHA-256,不存明文 token —— 库被看到也不能直接当 cookie 用。
MIGRATION_19_ASSISTANT_AUTH: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    _assistant_member_auth,
    """
    CREATE TABLE IF NOT EXISTS app_sessions (
        token_hash  TEXT    PRIMARY KEY,
        member_id   INTEGER NOT NULL REFERENCES app_members(id),
        created_at  TEXT    NOT NULL,
        expires_at  TEXT    NOT NULL,
        revoked_at  TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_member ON app_sessions(member_id)",
)


def _assistant_member_recovery(conn: sqlite3.Connection) -> None:
    """恢复码列:与 PIN 分开存,忘记 PIN 时用它换新 PIN。"""
    add_column_if_missing(conn, "app_members", "recovery_hash", "recovery_hash TEXT")
    add_column_if_missing(conn, "app_members", "recovery_salt", "recovery_salt TEXT")
    add_column_if_missing(conn, "app_members", "recovery_at", "recovery_at TEXT")


MIGRATION_20_ASSISTANT_RECOVERY: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_assistant_member_recovery,)


#: 登录/恢复码失败记录。落库而不是放进程内存:重启不清零,多进程共享。
MIGRATION_21_ASSISTANT_THROTTLE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS app_login_failures (
        id        INTEGER PRIMARY KEY,
        scope     TEXT    NOT NULL,
        member_id INTEGER NOT NULL,
        host      TEXT    NOT NULL,
        ts        TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_login_failures_lookup"
    " ON app_login_failures(scope, member_id, host, ts)",
)


_OWNER_TABLES = (
    "app_txns", "app_events", "app_problems", "app_review_units",
    "app_measurements", "app_workouts", "app_food_logs",
)


def _assistant_owner_columns(conn: sqlite3.Connection) -> None:
    """领域表加 owner_id:NULL=全家,数字=仅该成员。"""
    for table in _OWNER_TABLES:
        add_column_if_missing(conn, table, "owner_id", "owner_id INTEGER")


MIGRATION_22_ASSISTANT_OWNER: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    _assistant_owner_columns,
    *(
        "CREATE INDEX IF NOT EXISTS idx_" + table + "_owner"
        " ON " + table + "(owner_id)"
        for table in _OWNER_TABLES
    ),
    # 注意:不要把 current_member() 写进视图。测试/脚本里存在直接用
    # sqlite3.connect 的只读连接,它们不会注册这个函数;视图一旦引用就会
    # "no such function"。可见性放在服务层 SQL 里(见 ledger_service)。
    "DROP VIEW IF EXISTS app_txns_live",
    "CREATE VIEW app_txns_live AS SELECT * FROM app_txns WHERE deleted_at IS NULL",
)


def _rebuild_measurements_with_owner(conn: sqlite3.Connection) -> None:
    """重建 app_measurements:唯一键加 owner_id。

    旧唯一键 (metric_code, measured_at, source) 不含 owner:两个家长同一时刻
    记同一指标会互相覆盖。SQLite 不能改唯一约束,只能重建表。
    """
    conn.execute("DROP TABLE IF EXISTS app_measurements_new")
    conn.execute(
        """
        CREATE TABLE app_measurements_new (
            id          INTEGER PRIMARY KEY,
            metric_code TEXT    NOT NULL REFERENCES app_metric_types(code),
            value       REAL    NOT NULL,
            unit        TEXT    NOT NULL,
            measured_at TEXT    NOT NULL,
            local_date  TEXT    NOT NULL,
            source      TEXT    NOT NULL DEFAULT 'user',
            note        TEXT,
            created_at  TEXT    NOT NULL,
            owner_id    INTEGER,
            UNIQUE (metric_code, measured_at, source, owner_id)
        ) STRICT
        """
    )
    conn.execute(
        "INSERT INTO app_measurements_new(id, metric_code, value, unit, measured_at,"
        " local_date, source, note, created_at, owner_id)"
        " SELECT id, metric_code, value, unit, measured_at, local_date, source, note,"
        " created_at, owner_id FROM app_measurements"
    )
    conn.execute("DROP TABLE app_measurements")
    conn.execute("ALTER TABLE app_measurements_new RENAME TO app_measurements")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meas ON app_measurements(metric_code, local_date)"
    )
    # NULL 在 SQLite 的 UNIQUE 里彼此不相等,所以全家(owner NULL)要单独保一条唯一索引。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_meas_family"
        " ON app_measurements(metric_code, measured_at, source) WHERE owner_id IS NULL"
    )


MIGRATION_23_ASSISTANT_MEASUREMENT_OWNER: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_rebuild_measurements_with_owner,)


def _assistant_private_calendar_health(conn: sqlite3.Connection) -> None:
    """日程/健康不可共享:旧 NULL 数据归管理员，并给事件补收件人。"""
    add_column_if_missing(
        conn, "app_events", "reminder_target_member_id",
        "reminder_target_member_id INTEGER",
    )
    row = conn.execute("SELECT id FROM app_members ORDER BY id LIMIT 1").fetchone()
    admin = int(row[0]) if row is not None else 1
    for table in ("app_events", "app_measurements", "app_workouts", "app_food_logs"):
        conn.execute(
            "UPDATE " + table + " SET owner_id = ? WHERE owner_id IS NULL", (admin,)
        )
    conn.execute(
        "UPDATE app_events SET reminder_target_member_id = owner_id"
        " WHERE reminder_target_member_id IS NULL"
    )


MIGRATION_24_ASSISTANT_PRIVATE_REMINDERS: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_assistant_private_calendar_health,)


def _assistant_intake_owner(conn: sqlite3.Connection) -> None:
    """录入会话加归属(2026-10-18 审计):旧库全是 NULL = 单用户遗留,按旧行为可见。"""
    add_column_if_missing(conn, "app_intake_sessions", "owner_session", "owner_session TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_intake_sessions_owner"
        " ON app_intake_sessions(owner_session, status)"
    )


MIGRATION_25_ASSISTANT_INTAKE_OWNER: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_assistant_intake_owner,)


def _assistant_import_reverted(conn: sqlite3.Connection) -> None:
    """导入批次加“已撤销”标记:撤销后重新导入要能恢复,而不是被 sha256 永久短路。"""
    add_column_if_missing(conn, "app_import_batches", "reverted_at", "reverted_at TEXT")


MIGRATION_26_ASSISTANT_IMPORT_REVERTED: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_assistant_import_reverted,)


def _assistant_config_rev(conn: sqlite3.Connection) -> None:
    """种下 config_rev:页面改模型/语音配置时 +1,各进程据此重建 Settings。

    ⚠️ 用 INSERT OR IGNORE 而不是 UPDATE:旧库里可能已经有这个键(手工设过),
       迁移只能"保证存在",不能覆盖用户的值。
    """
    conn.execute(
        "INSERT OR IGNORE INTO sys_settings(key, value, updated_at) VALUES (?, ?, ?)",
        ("config_rev", "1", datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


#: 页面可编辑的密钥与 sudo 票据(D157)。
#: 密钥单独一张表,不塞 sys_settings —— 那张表是明文,且被导出/诊断脚本到处读;
#: 读密钥的代码只允许有 la/ops/secrets.py 一处,审计面才收得拢。
def _assistant_study_push_targets(conn: sqlite3.Connection) -> None:
    """错题推送的收件人(D161):JSON 数组,空 = 所有已绑微信的成员。"""
    add_column_if_missing(conn, "app_study_settings", "push_targets", "push_targets TEXT")


MIGRATION_28_ASSISTANT_STUDY_TARGETS: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_assistant_study_push_targets,)


# --- M33:笔记域(文件夹树 + 笔记 + 版本)------------------------------------
# 设计:docs/笔记域设计.md。三条不变量:
#   * 笔记是**唯一事实源**(不可重建),住在领域库;记忆层只能通过发件箱"知道"它;
#   * 树用「邻接表 + 物化 path」两条都留:parent 保引用,path 让"搬一整棵子树"
#     变成一条 UPDATE,不必递归;
#   * 版本表只存**改动之前**的样子(撤销与"找回"都靠它)。
def _assistant_notes_seed_inbox(conn: sqlite3.Connection) -> None:
    """种一个「收件箱」系统夹。

    ⭐ 它是"随手记"的默认落点:用户打完球说一句就想记,不能每次都逼他先选夹子。
      没指定夹时落这里,之后在网页上一键归类(先保证不丢,再谈整理)。
    ⚠️ 夹的 owner_id 一律 NULL(v1 夹是共用结构,笔记自己带 owner_id):
      否则每个成员登录后都要先种一份收件箱,而 path 唯一性又按 owner 分,
      同一台机器上会长出好几个「收件箱」。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO app_note_folders(parent_id, name, path, kind, sort_order,"
        " archived, created_at, updated_at) VALUES (NULL, ?, ?, 'inbox', 0, 0, ?, ?)",
        ("收件箱", "收件箱", now, now),
    )


MIGRATION_29_ASSISTANT_NOTES: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    # 检索能力探测的落脚点。助手库原先没有这张表(只有记忆库有)——
    # 笔记的 FTS 也要记住"这台机器支不支持 trigram",否则每请求都要真建一次虚表试。
    """
    CREATE TABLE IF NOT EXISTS sys_capabilities (
        name      TEXT PRIMARY KEY,
        value     TEXT NOT NULL,
        probed_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_note_folders (
        id         INTEGER PRIMARY KEY,
        parent_id  INTEGER REFERENCES app_note_folders(id),
        name       TEXT    NOT NULL,
        path       TEXT    NOT NULL,
        kind       TEXT    NOT NULL DEFAULT 'normal',
        sort_order INTEGER NOT NULL DEFAULT 0,
        archived   INTEGER NOT NULL DEFAULT 0,
        owner_id   INTEGER,
        created_at TEXT    NOT NULL,
        updated_at TEXT    NOT NULL
    ) STRICT
    """,
    # ⚠️ owner_id 为 NULL 时 SQLite 的 UNIQUE 判不出重复(NULL ≠ NULL),
    #    必须把 NULL 折叠成 0 再用表达式索引。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_note_folders_path
        ON app_note_folders(COALESCE(owner_id, 0), path) WHERE archived = 0
    """,
    """
    CREATE TABLE IF NOT EXISTS app_notes (
        id           INTEGER PRIMARY KEY,
        folder_id    INTEGER REFERENCES app_note_folders(id),
        title        TEXT    NOT NULL,
        body         TEXT    NOT NULL,
        kind         TEXT    NOT NULL DEFAULT 'note',
        ts           TEXT    NOT NULL,
        local_date   TEXT    NOT NULL,
        source       TEXT    NOT NULL,
        material_ids TEXT,
        tags         TEXT,
        deleted_at   TEXT,
        owner_id     INTEGER,
        created_at   TEXT    NOT NULL,
        updated_at   TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS ix_notes_folder ON app_notes(folder_id, local_date DESC)",
    "CREATE INDEX IF NOT EXISTS ix_notes_live ON app_notes(deleted_at, local_date DESC)",
    """
    CREATE TABLE IF NOT EXISTS app_note_versions (
        id         INTEGER PRIMARY KEY,
        note_id    INTEGER NOT NULL REFERENCES app_notes(id),
        title      TEXT    NOT NULL,
        body       TEXT    NOT NULL,
        folder_id  INTEGER,
        reason     TEXT    NOT NULL,
        created_at TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS ix_note_versions_note ON app_note_versions(note_id, id DESC)",
    _assistant_notes_seed_inbox,
)



# --- M30:健康数据从 iPhone 来(2026-11-11) ----------------------------------
# 背景(用户原话):*「健康领域功能太过简单。我用的 iPhone,没有电话手表,
# 所以主要想从手机的健康 App 里拿到状态信息。」*
#
# ⚠️ 一条硬事实先说清楚:**HealthKit 没有服务端 API** —— 数据只在手机上,
#    没有任何办法让服务器自己去拉。所以进来的路只有两条(见 docs/苹果健康接入方案.md):
#      ① iPhone「快捷指令」定时把当天的汇总推过来(POST /api/health/sync);
#      ② 健康 App →「导出所有健康数据」→ 上传 export.zip(首次补历史)。
#    两条都落到**同一张测量表**,只是 source 不同。
#
# 为什么只加三列 + 一张日志表,不建"每日汇总表":
#    项目的老规矩是**存原始观测、算派生指标**(D43)。步数/活动能量这类高频指标
#    一天几十条,但一年也就一两万行,SQLite 聚合毫无压力 —— 真正需要的是
#    "同一天的几十条怎么合成一个数"这条规则,而不是一张新表。
#    所以规则写进字典(app_metric_types.aggregate):sum / avg / last / min / max。
#    加一个新指标从此只是插一行,不碰表结构。
def _health_metric_aggregate(conn: sqlite3.Connection) -> None:
    """给指标字典补 aggregate 列(幂等)。

    ⚠️ 这一步不能只写在下面那条 UPDATE 里:老库里根本没有这一列。
    """
    add_column_if_missing(conn, "app_metric_types", "aggregate",
                          "aggregate TEXT NOT NULL DEFAULT 'avg'")


#: (code, 名称, 单位, 小数位, 聚合方式)。⭐ 聚合方式是这个迁移的重点:
#:   步数/能量/距离/爬楼/锻炼/站立是**一天内多条相加**,取平均会少一个量级;
#:   体重/体脂/静息心率是**一天的读数取平均/最后一条**。
HEALTH_METRICS_V2: tuple[tuple[str, str, str, int, str], ...] = (
    ("steps", "步数", "步", 0, "sum"),
    ("active_kcal", "活动能量", "kcal", 0, "sum"),
    ("basal_kcal", "基础能量", "kcal", 0, "sum"),
    ("distance_km", "步行距离", "km", 2, "sum"),
    ("flights", "爬楼层", "层", 0, "sum"),
    ("exercise_min", "锻炼分钟", "分钟", 0, "sum"),
    ("stand_hours", "站立小时", "小时", 0, "sum"),
    ("sleep_hours", "睡眠时长", "h", 1, "sum"),
    ("sleep_inbed", "卧床时长", "h", 1, "sum"),
    ("sleep_midpoint", "睡眠中点", "时", 2, "last"),
    ("resting_hr", "静息心率", "bpm", 0, "avg"),
    ("heart_rate", "心率", "bpm", 0, "avg"),
    ("hrv", "心率变异性", "ms", 0, "avg"),
    ("vo2max", "最大摄氧量", "mL/kg/min", 1, "last"),
    ("respiratory_rate", "呼吸频率", "次/分", 1, "avg"),
    ("oxygen_saturation", "血氧", "%", 0, "avg"),
)

MIGRATION_30_ASSISTANT_HEALTH_SYNC: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    _health_metric_aggregate,
    # 运动记录也要能说清"这条是手机同步来的",并且重复导入不能变成两条。
    #   source      : user / device:apple / device:google …
    #   external_id : 设备侧的身份(苹果是 "类型|开始时间"),用于**幂等去重**;
    #                 手工记的运动没有它,UNIQUE 索引因此带 WHERE 条件。
    _add_column("app_workouts", "source", "source TEXT NOT NULL DEFAULT 'user'"),
    _add_column("app_workouts", "external_id", "external_id TEXT"),
    # 苹果自己算的消耗(千卡)。有它就用它,没有才退回 MET × 体重 ——
    # 手机与手表测的是同一个动作,不该被我们的公式再估一遍。
    _add_column("app_workouts", "kcal", "kcal REAL"),
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_workouts_external
        ON app_workouts(source, external_id) WHERE external_id IS NOT NULL
    """,
    # "今天"那一页要按日期扫全部指标(以前只有"按指标+日期",那是趋势页的形状)。
    "CREATE INDEX IF NOT EXISTS idx_meas_day ON app_measurements(local_date)",
    # 同步日志:谁在什么时候推了多少条。只用于页面显示"上次同步",不参与业务计算。
    # ⚠️ 不记 token、不记原始报文 —— 日志不是数据副本,泄露一条日志不该泄露健康数据。
    """
    CREATE TABLE IF NOT EXISTS app_health_syncs (
        id      INTEGER PRIMARY KEY,
        ts      TEXT    NOT NULL,
        source  TEXT    NOT NULL,
        items   INTEGER NOT NULL DEFAULT 0,
        days    INTEGER NOT NULL DEFAULT 0,
        note    TEXT,
        ip      TEXT,
        ua      TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_health_syncs_ts ON app_health_syncs(id DESC)",
    # 种子 + 修正老行的聚合方式。⚠️ steps / sleep_hours 在 M6 就存在了,
    #    它们默认是 'avg' —— 不改成 sum 的话,一天走 8000 步会显示成平均每段几十步。
    *(
        "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals, aggregate)"
        " VALUES ('" + code + "', '" + name + "', '" + unit + "', " + str(decimals)
        + ", '" + aggregate + "')"
        for code, name, unit, decimals, aggregate in HEALTH_METRICS_V2
    ),
    "UPDATE app_metric_types SET aggregate = 'sum' WHERE code IN ('steps', 'sleep_hours')",
    # 运动类型:苹果那边的类型名映射过来要能查到同一个 MET,否则会退回 4.0,
    # 把 8.3 MET 的跑步记成中等强度(消耗直接腰斩)。
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('爬山',6.0,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('椭圆机',5.0,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('划船',7.0,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('爬楼',8.0,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('跳舞',5.0,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('高强度间歇',8.0,'有氧')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('核心训练',4.0,'力量')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category) VALUES ('其他运动',4.0,'日常')",
)


# --- M31:补上身高指标(2026-11-11 真机) -------------------------------------
# ⚠️ 为什么单独一个迁移:Apple 的导出里有 **身高** 记录(注册健康档案时填的),
#    解析器白名单也认它,但 M6 的种子里漏了这一行 —— 写入时
#    app_measurements.metric_code 的外键直接失败,整次 71.5 万条导入回滚,
#    页面只看到 "FOREIGN KEY constraint failed"。
#    以后加白名单指标时,**必须同时**在字典里有一行(有测试钉住)。
MIGRATION_31_ASSISTANT_HEIGHT_METRIC: tuple[str, ...] = (
    "INSERT OR IGNORE INTO app_metric_types(code, name, unit, decimals, aggregate)"
    " VALUES ('height', '身高', 'cm', 1, 'last')",
)

# --- M32:运动的第二个维度(2026-11-21,D188) --------------------------------
# 用户原话:*「每项运动,应该能有两个不同维度的记录……仰卧起坐,另一个就个数。」*
# 现状是:距离列(app_workouts.distance_km)本来就有,但手工记录没有入口、页面与
# 模型都不读它;个数则**连列都没有** —— 于是"10 个仰卧起坐"只能被记成一段时长,
# 或者把个数丢掉。
# ⚠️ 只加一列、且允许为空:老记录一条都不用动,"没记个数"与"个数是 0"必须分得开
#    (与 app_measurements 里"没数据 ≠ 0"同一条纪律)。
MIGRATION_32_ASSISTANT_WORKOUT_REPS: tuple[str, ...] = (
    _add_column("app_workouts", "reps", "reps INTEGER"),
)

# --- M33:自重动作的强度系数(2026-11-21,D188 补记) -------------------------
# 起因:仰卧起坐 / 俯卧撑 / 深蹲 / 引体向上 / 平板支撑**不在字典里** ——
# log_workout 于是按 4.0 兜底:记录本身没错,但「强度」那一栏是编的(中等),
# 有时长的那些场次连 kcal 都跟着偏。
# 值取自 Compendium(2024 版 pacompendium.com)的**具体条目**,不是拍平均:
#   02022 俯卧撑 / 仰卧起坐 / 引体向上,moderate      → 3.8
#   02052 负重训练(深蹲 / 硬拉)                      → 5.0
#   02020 自重训练(引体向上按 vigorous 这一档)        → 7.5
#   02024 卷腹 / 平板支撑,light                      → 2.8
# ⚠️ 这些数只在**有分钟数**时参与 MET-分钟与 kcal;只记个数时它们仅影响强度标签。
MIGRATION_33_ASSISTANT_BODYWEIGHT_ACTIVITIES: tuple[str, ...] = (
    "INSERT OR IGNORE INTO app_activity_types(name, met, category)"
    " VALUES ('仰卧起坐',3.8,'力量')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category)"
    " VALUES ('俯卧撑',3.8,'力量')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category)"
    " VALUES ('深蹲',5.0,'力量')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category)"
    " VALUES ('引体向上',7.5,'力量')",
    "INSERT OR IGNORE INTO app_activity_types(name, met, category)"
    " VALUES ('平板支撑',2.8,'力量')",
)


MIGRATION_27_ASSISTANT_SECRETS: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (
    _assistant_config_rev,
    """
    CREATE TABLE IF NOT EXISTS app_secrets (
        name       TEXT    PRIMARY KEY,
        ciphertext BLOB    NOT NULL,
        updated_at TEXT    NOT NULL,
        updated_by INTEGER
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS app_sudo_tokens (
        token_hash TEXT    PRIMARY KEY,
        member_id  INTEGER NOT NULL,
        created_at TEXT    NOT NULL,
        expires_at TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_sudo_expiry ON app_sudo_tokens(expires_at)",
)


MIGRATIONS_ASSISTANT: tuple[tuple[int, str, tuple[str | Callable[[sqlite3.Connection], None], ...]], ...] = (
    (1, "m0_infrastructure", MIGRATION_1_ASSISTANT_INFRASTRUCTURE),
    (2, "m4_ledger", MIGRATION_2_ASSISTANT_LEDGER),
    (3, "m4_import_rules", MIGRATION_3_ASSISTANT_IMPORT_RULES),
    (4, "m4_calendar", MIGRATION_4_ASSISTANT_CALENDAR),
    (5, "m4_study", MIGRATION_5_ASSISTANT_STUDY),
    (6, "m4_health", MIGRATION_6_ASSISTANT_HEALTH),
    (7, "m6_safety", MIGRATION_7_ASSISTANT_SAFETY),
    (8, "m7_receipts", MIGRATION_8_ASSISTANT_RECEIPTS),
    (9, "m9_feedback", MIGRATION_9_ASSISTANT_FEEDBACK),
    (10, "m10_study_intake", MIGRATION_10_ASSISTANT_STUDY_V2),
    (11, "m11_materials", MIGRATION_11_ASSISTANT_MATERIALS),
    (12, "m12_settings", MIGRATION_12_ASSISTANT_SETTINGS),
    (13, "m14_study_sheet", MIGRATION_13_ASSISTANT_STUDY_V3),
    (14, "m14_answer_teacher", MIGRATION_14_ASSISTANT_ANSWER_TEACHER),
    (15, "m14c_auto_solve", MIGRATION_15_ASSISTANT_AUTO_SOLVE),
    (16, "m14d_problem_audio", MIGRATION_16_ASSISTANT_PROBLEM_AUDIO),
    (17, "d147_undo_claim", MIGRATION_17_ASSISTANT_UNDO_CLAIM),
    (18, "m16_members", MIGRATION_18_ASSISTANT_MEMBERS),
    (19, "m17_auth", MIGRATION_19_ASSISTANT_AUTH),
    (20, "m18_recovery", MIGRATION_20_ASSISTANT_RECOVERY),
    (21, "m19_login_throttle", MIGRATION_21_ASSISTANT_THROTTLE),
    (22, "m20_owner", MIGRATION_22_ASSISTANT_OWNER),
    (23, "m21_measurement_owner", MIGRATION_23_ASSISTANT_MEASUREMENT_OWNER),
    (24, "m22_private_reminders", MIGRATION_24_ASSISTANT_PRIVATE_REMINDERS),
    (25, "m23_intake_owner", MIGRATION_25_ASSISTANT_INTAKE_OWNER),
    (26, "m24_import_reverted", MIGRATION_26_ASSISTANT_IMPORT_REVERTED),
    (27, "m25_secrets", MIGRATION_27_ASSISTANT_SECRETS),
    (28, "m26_study_push_targets", MIGRATION_28_ASSISTANT_STUDY_TARGETS),
    (29, "m27_notes", MIGRATION_29_ASSISTANT_NOTES),
    (30, "m28_health_sync", MIGRATION_30_ASSISTANT_HEALTH_SYNC),
    (31, "m29_height_metric", MIGRATION_31_ASSISTANT_HEIGHT_METRIC),
    (32, "m30_workout_reps", MIGRATION_32_ASSISTANT_WORKOUT_REPS),
    (33, "m31_bodyweight_activities", MIGRATION_33_ASSISTANT_BODYWEIGHT_ACTIVITIES),
)

# --- M7:事项层(待办/目标/提醒)的写入回执 --------------------------------
# ⚠️ 为什么这张表在两个库里各有一份:回执必须与它记录的那次写入**同库同事务**
#    (领域数据在 assistant.db,事项数据在 memory.db),跨库就没有原子性可言。
#    记忆库是可重建的,所以这里不做只追加触发器 —— 触发器留给 assistant.db 的那份。
MIGRATION_4_MEMORY_RECEIPTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ops_receipts (
        id          INTEGER PRIMARY KEY,
        ts          TEXT    NOT NULL,
        actor       TEXT    NOT NULL DEFAULT 'agent',
        kind        TEXT    NOT NULL,
        domain      TEXT    NOT NULL,
        label       TEXT    NOT NULL,
        detail      TEXT,
        undo_plan   TEXT,
        undoable    INTEGER NOT NULL DEFAULT 1,
        undone_at   TEXT,
        undone_note TEXT,
        session_id  TEXT,
        turn        INTEGER
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_receipts_recent ON ops_receipts(id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_receipts_open ON ops_receipts(undone_at, id DESC)",
)

# --- 事实准入:候选队列 ----------------------------------------------------
# ⭐ 为什么事实需要一个"候选"阶段,而不是直接写 facts:
#    facts 是唯一**每轮无条件进上下文**的一层。实测(as of 2026-10-13)11 条生效事实里
#    5 条有问题 —— 极性写反("我不吃香菜" → "用户比较喜欢 吃香菜")、疑问句入库
#    ("用户喜欢 吃什么东西")、半句截断("…喜欢看网络小说,然")、未来动作入库
#    ("，明天晚上7点钟发信息提醒我")。它们全部来自**规则匹配到就直接写**这条路。
#    所以:提议(规则/模型)与准入(裁决)必须分开。候选表就是那道闸门。
#
# 为什么记 verdict 原文:排障时要能回答"它当初为什么被拒/被并",而 summary_pool 的
# 教训正是"事件里没留正文,事后重建不出来"(D93)。这一层不留这个遗憾。
MIGRATION_5_MEMORY_FACT_GATE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS fact_candidates (
        id             INTEGER PRIMARY KEY,
        session_id     TEXT,
        content        TEXT    NOT NULL,
        normalized     TEXT    NOT NULL,
        fact_type      TEXT    NOT NULL DEFAULT 'other',
        keywords       TEXT    NOT NULL DEFAULT '[]',
        confidential   INTEGER NOT NULL DEFAULT 0,
        source         TEXT    NOT NULL,
        status         TEXT    NOT NULL DEFAULT 'pending',
        reason         TEXT,
        verdict        TEXT,
        target_fact_id INTEGER,
        fact_id        INTEGER,
        created_at     TEXT    NOT NULL,
        resolved_at    TEXT
    ) STRICT
    """,
    # 同一句话反复出现(用户重复说、规则重复抽)只留一条 —— 状态一起进键,
    # 所以"同一句被拒之后又原样出现"仍然是同一条,不会刷屏。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_candidates_dedup"
    " ON fact_candidates(normalized, status)",
    "CREATE INDEX IF NOT EXISTS idx_fact_candidates_status"
    " ON fact_candidates(status, id DESC)",
)

def _memory_todo_alarms(conn: sqlite3.Connection) -> None:
    """迁移 6:待办带上"时刻"与"闹钟",提醒知道自己在给谁当闹钟(D115)。

    ⭐ 三列一起加,缺一不可:
      * `todos.due_at_local` —— 待办以前**只有日期**(YYYY-MM-DD),"明天中午更新云朵"
        在待办里无处安放,于是那句工作只能落到提醒表里,**工作本身丢了**;
      * `todos.remind_minutes_before` —— NULL=不提醒,0=到点提醒,N=提前 N 分钟;
      * `schedules.todo_id` —— 这条闹钟属于哪条待办(NULL = 独立提醒,例如"吃饭")。

    派发环**一行都不用改**:抢占式认领、迟到窗口、合并成一条、微信额度都留在
    la/bridge/reminder_push.py 里,它只是这回能说出"这是哪条待办的闹钟"。
    """
    add_column_if_missing(conn, "todos", "due_at_local", "due_at_local TEXT")
    add_column_if_missing(conn, "todos", "remind_minutes_before", "remind_minutes_before INTEGER")
    add_column_if_missing(conn, "schedules", "todo_id", "todo_id TEXT")


MIGRATION_6_MEMORY_TODO_ALARMS: tuple[str | Callable[[sqlite3.Connection], None], ...] = (
    _memory_todo_alarms,
    "CREATE INDEX IF NOT EXISTS idx_schedules_todo ON schedules(todo_id)",
)


def _memory_schedule_target(conn: sqlite3.Connection) -> None:
    """提醒的收件人:NULL=全部;数字=只推给该成员。"""
    add_column_if_missing(conn, "schedules", "target_member_id", "target_member_id INTEGER")


MIGRATION_7_MEMORY_SCHEDULE_TARGET: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_memory_schedule_target,)


def _memory_todo_owner(conn: sqlite3.Connection) -> None:
    """待办归属:旧待办归管理员(1)，它的闹钟跟着只提醒管理员。"""
    add_column_if_missing(conn, "todos", "owner_id", "owner_id INTEGER")
    conn.execute("UPDATE todos SET owner_id = 1 WHERE owner_id IS NULL")
    conn.execute(
        "UPDATE schedules SET target_member_id = ("
        "SELECT owner_id FROM todos WHERE todos.id = schedules.todo_id"
        ") WHERE todo_id IS NOT NULL AND target_member_id IS NULL"
    )


MIGRATION_8_MEMORY_TODO_OWNER: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_memory_todo_owner,)


def _memory_fact_supersedes(conn: sqlite3.Connection) -> None:
    """改口带着「要取代哪一条」进候选队列(2026-10-18 审计修)。

    ⚠️ 为什么必须存在候选行上:memory_correct 以前**先**把旧事实置为失效,**再**把
    新说法放进候选队列;候选被准入闸拒掉(疑问句/超长/重复)时,旧记忆就已经没了。
    现在旧事实一直有效,直到新候选真的入库 —— 那一刻才由准入闸把它置为失效并互指。
    """
    add_column_if_missing(conn, "fact_candidates", "supersedes", "supersedes INTEGER")


MIGRATION_9_MEMORY_FACT_SUPERSEDES: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_memory_fact_supersedes,)


def _memory_backfill_summary_tags(conn: sqlite3.Connection) -> None:
    """迁移 10:给历史摘要桶补领域标签(2026-11-07)。

    ⭐ 新压缩已经会写标签(la/agent/session.py 的 compact_once),但旧库里所有桶的
       tags 都是 [] —— 不补的话 query_summary_pool(tags=...) 对历史记忆仍然查不到,
       等于"新代码修了、老数据没修"。

    ⚠️ 回填口径**复用线上同一个函数**(la.memory.topics.tags_from_rows),不另写一套
       规则 —— 否则两个月后两边对不上,又是"文档说 A、代码做 B"。
    ⚠️ 只处理 tags='[]' 的行:已经打过标(包括 consolidate 的 ["consolidated"])的不动。
    """
    # 延迟导入:storage 是更底层,模块顶部 import memory 会形成 storage <-> memory 环。
    from ..memory.topics import tags_from_rows

    rows = conn.execute(
        "SELECT id, session_id, start_seq, end_seq FROM summary_pool WHERE tags = '[]'"
    ).fetchall()
    for row in rows:
        # 用位置索引:迁移可能拿到没设 row_factory 的连接(测试/脚本)。
        summary_id, session_id, start_seq, end_seq = row[0], row[1], row[2], row[3]
        event_rows = conn.execute(
            "SELECT type, data FROM events WHERE session_id = ? AND seq BETWEEN ? AND ?",
            (session_id, int(start_seq), int(end_seq)),
        ).fetchall()
        tags = tags_from_rows(event_rows)
        if not tags:
            continue
        conn.execute(
            "UPDATE summary_pool SET tags = ? WHERE id = ?",
            (json.dumps(tags, ensure_ascii=False), int(summary_id)),
        )
    conn.commit()


MIGRATION_10_MEMORY_SUMMARY_TAGS: tuple[
    str | Callable[[sqlite3.Connection], None], ...
] = (_memory_backfill_summary_tags,)


MIGRATION_11_MEMORY_WAKE: tuple[str, ...] = (
    # 定时唤醒。⚠️ 与 schedules 分开建表:提醒到点只发文本,唤醒到点要跑一轮模型。
    """
    CREATE TABLE IF NOT EXISTS wake_jobs (
        id              TEXT PRIMARY KEY,
        owner_member_id INTEGER NOT NULL,
        title           TEXT    NOT NULL,
        instruction     TEXT    NOT NULL,
        freq            TEXT    NOT NULL CHECK (freq IN ('daily', 'weekly')),
        time_local      TEXT    NOT NULL,
        weekday         INTEGER,
        timezone        TEXT    NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1,
        allow_writes    INTEGER NOT NULL DEFAULT 1,
        next_at         TEXT,
        last_run_at     TEXT,
        last_status     TEXT    NOT NULL DEFAULT '',
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL,
        CHECK (freq <> 'weekly' OR (weekday IS NOT NULL AND weekday BETWEEN 1 AND 7))
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_wake_jobs_due"
    " ON wake_jobs(enabled, next_at)",
    "CREATE INDEX IF NOT EXISTS idx_wake_jobs_owner"
    " ON wake_jobs(owner_member_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS wake_runs (
        id              TEXT PRIMARY KEY,
        job_id          TEXT    NOT NULL,
        title           TEXT    NOT NULL DEFAULT '',
        owner_member_id INTEGER NOT NULL,
        scheduled_for   TEXT    NOT NULL,
        dry_run         INTEGER NOT NULL DEFAULT 0,
        status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'ok', 'reported',
                                          'silent', 'failed', 'timeout', 'skipped',
                                          'unknown', 'missed', 'quota')),
        started_at      TEXT,
        finished_at     TEXT,
        summary         TEXT    NOT NULL DEFAULT '',
        error           TEXT    NOT NULL DEFAULT '',
        writes          INTEGER NOT NULL DEFAULT 0,
        receipts        TEXT    NOT NULL DEFAULT '[]',
        usage           TEXT    NOT NULL DEFAULT '{}',
        report_id       TEXT    NOT NULL DEFAULT '',
        created_at      TEXT    NOT NULL,
        UNIQUE (job_id, scheduled_for)
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_wake_runs_job"
    " ON wake_runs(job_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_wake_runs_owner"
    " ON wake_runs(owner_member_id, created_at)",
    # ⭐ 报告是**待送达队列**:先写 pending,微信进程发送成功后才标 sent。
    #    这一列与 reminder_push 的"先送达、后认领"同一条纪律。
    """
    CREATE TABLE IF NOT EXISTS wake_reports (
        id              TEXT PRIMARY KEY,
        run_id          TEXT    NOT NULL,
        job_id          TEXT    NOT NULL,
        title           TEXT    NOT NULL DEFAULT '',
        owner_member_id INTEGER NOT NULL,
        text            TEXT    NOT NULL,
        status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent', 'failed', 'expired')),
        attempts        INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    NOT NULL,
        sent_at         TEXT,
        expires_at      TEXT    NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_wake_reports_pending"
    " ON wake_reports(status, created_at)",
)

MIGRATIONS_MEMORY: tuple[
    tuple[int, str, tuple[str | Callable[[sqlite3.Connection], None], ...]], ...
] = (
    (1, "m1_memory", MIGRATION_1_MEMORY),
    (2, "m2_consolidation", MIGRATION_2_MEMORY_CONSOLIDATION),
    (3, "m3_tasks", MIGRATION_3_MEMORY_TASKS),
    (4, "m7_receipts", MIGRATION_4_MEMORY_RECEIPTS),
    (5, "m14_fact_gate", MIGRATION_5_MEMORY_FACT_GATE),
    (6, "m15_todo_alarms", MIGRATION_6_MEMORY_TODO_ALARMS),
    (7, "m16_schedule_target", MIGRATION_7_MEMORY_SCHEDULE_TARGET),
    (8, "m17_todo_owner", MIGRATION_8_MEMORY_TODO_OWNER),
    (9, "m18_fact_supersedes", MIGRATION_9_MEMORY_FACT_SUPERSEDES),
    (10, "m25_summary_tags", MIGRATION_10_MEMORY_SUMMARY_TAGS),
    (11, "m26_wake", MIGRATION_11_MEMORY_WAKE),
)


def add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> bool:
    """幂等地加一列。

    ⚠️ 为什么需要它:SQLite 的 `ALTER TABLE ADD COLUMN` **没有 IF NOT EXISTS**。
    而迁移的 DDL 在 Python 的 legacy 事务模式下不进事务(只有 DML 才隐式开事务),
    所以"执行到一半崩了"会留下"列已加、版本号没记"的库,下次启动直接 duplicate column。
    加列本来就是一次性的事,写成幂等的成本几乎为零,风险却直接归零。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}
    if not existing:
        raise sqlite3.OperationalError("表不存在,加不了列: " + table)
    if column in existing:
        return False
    conn.execute("ALTER TABLE " + table + " ADD COLUMN " + ddl)
    return True


def _db_path(conn: sqlite3.Connection) -> str:
    """这个连接主库的文件路径;内存库返回空串。"""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return ""
    return str(row[2] or "") if row is not None else ""


@contextmanager
def _cross_process_lock(conn: sqlite3.Connection):
    """库级排他锁:进程内的 MIGRATION_LOCK 管不住**另一个进程**。

    ⚠️ 为什么必须有:云上是 systemd 三条服务同时启动(web + 两个微信渠道),
       三个进程会同时看到"缺 v27"、同时建表、同时记版本号 —— 线程锁跨不了进程。
       以前是单进程启动,这个问题看不出来;第 27 号(密钥表)正好落在这个窗口里。

    锁文件放在库旁边(`<db>.migrate.lock`),不放在 data 根:两个库各有自己的
    迁移序列,锁也该各管各的。内存库直接跳过 —— 那种库没有第二个进程。
    """
    path = _db_path(conn)
    if not path or path == ":memory:":
        yield
        return
    handle = open(path + ".migrate.lock", "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


def _apply_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[
        tuple[int, str, tuple[str | Callable[[sqlite3.Connection], None], ...]], ...
    ],
) -> list[int]:
    conn.execute(VERSION_TABLE_DDL)
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM sys_schema_version")}
    done: list[int] = []
    for version, name, statements in migrations:
        if version in applied:
            continue
        for statement in statements:
            if callable(statement):
                statement(conn)
            else:
                conn.execute(statement)
        # OR IGNORE:万一有另一个进程抢先记了同一个版本号,这里也不炸 —— 迁移内容相同。
        conn.execute(
            "INSERT OR IGNORE INTO sys_schema_version(version, name, applied_at)"
            " VALUES (?, ?, ?)",
            (version, name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
        done.append(version)
    return done


def migrate(
    conn: sqlite3.Connection,
    migrations: tuple[
        tuple[int, str, tuple[str | Callable[[sqlite3.Connection], None], ...]], ...
    ] = MIGRATIONS_ASSISTANT,
) -> list[int]:
    """应用尚未执行的迁移,返回本次应用的版本号列表。

    两层锁:
      * `MIGRATION_LOCK` —— 进程内同一时刻只有一个连接在建表(线程);
      * `_cross_process_lock` —— 跨进程(云上 web + 两个微信渠道同时开机)。

    迁移项可以是 SQL 字符串,也可以是一个 `(conn) -> None` 的回调 ——
    后者用于"没有 IF NOT EXISTS 可用"的 DDL(例如 ALTER TABLE,见 add_column_if_missing)。
    """
    with MIGRATION_LOCK:
        with _cross_process_lock(conn):
            return _apply_migrations(conn, migrations)

// Ported from Hermes Agent (MIT): hermes_state_common.py (SCHEMA_SQL, FTS_SQL, FTS_TRIGRAM_SQL), hermes_state_schema.py, hermes_state_fts.py.
import Foundation
import SQLite3

/// What the FTS indexes can serve after `ConversationSchema.prepare`.
struct FTSState {
    var enabled: Bool
    var trigramAvailable: Bool
    var stale: Bool
}

enum ConversationSchema {
    static let version = 1

    /// Tool results can be megabytes of machine output; the word index keeps only this prefix of each tool row.
    static let toolContentPrefixChars = 8_192

    static let staleKey = "fts_stale"

    /// Data migrations keyed by the version they produce. Column additions never belong here:
    /// `reconcileColumns` adds any column `tablesSQL` declares that a live table lacks.
    static let migrations: [(version: Int, run: (Connection) throws -> Void)] = []

    static let tablesSQL = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS state_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        parent_session_id TEXT,
        started_at REAL NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0,
        message_count INTEGER NOT NULL DEFAULT 0,
        last_activity_at REAL,
        last_prompt_tokens INTEGER,
        last_prompt_tokens_at_seq INTEGER,
        FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        seq INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        tool_name TEXT,
        tool_call_id TEXT,
        timestamp REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        compacted INTEGER NOT NULL DEFAULT 0,
        _compressed_summary INTEGER NOT NULL DEFAULT 0,
        UNIQUE (session_id, seq)
    );
    """

    static let indexSQL = """
    CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_sessions_effective_activity
        ON sessions(COALESCE(last_activity_at, started_at) DESC, started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_messages_session_active ON messages(session_id, active, seq);
    """

    private static func indexedContent(_ alias: String) -> String {
        "CASE WHEN \(alias).role = 'tool' THEN substr(COALESCE(\(alias).content, ''), 1, \(toolContentPrefixChars)) ELSE \(alias).content END"
    }

    static let baseTriggers = ["messages_fts_insert", "messages_fts_delete", "messages_fts_update"]
    static let trigramTriggers = ["messages_fts_trigram_insert", "messages_fts_trigram_delete", "messages_fts_trigram_update"]

    /// The word index reads back exactly the projection its triggers write, so FTS5 'delete' and 'rebuild' agree with it.
    static let ftsSQL = """
    CREATE VIEW IF NOT EXISTS messages_fts_src AS
        SELECT id, \(indexedContent("messages")) AS content, tool_name FROM messages;
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        content, tool_name, content='messages_fts_src', content_rowid='id'
    );
    CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content, tool_name) VALUES (new.id, \(indexedContent("new")), new.tool_name);
    END;
    CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content, tool_name)
        VALUES ('delete', old.id, \(indexedContent("old")), old.tool_name);
    END;
    CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF content, tool_name, role ON messages
    WHEN old.content IS NOT new.content OR old.tool_name IS NOT new.tool_name OR old.role IS NOT new.role
    BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content, tool_name)
        VALUES ('delete', old.id, \(indexedContent("old")), old.tool_name);
        INSERT INTO messages_fts(rowid, content, tool_name) VALUES (new.id, \(indexedContent("new")), new.tool_name);
    END;
    """

    /// Substring index for CJK text. Tool rows are machine noise and stay out of it; they remain in the word index.
    static let trigramSQL = """
    CREATE VIEW IF NOT EXISTS messages_fts_trigram_src AS
        SELECT id, content, tool_name FROM messages WHERE role <> 'tool';
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
        content, tool_name, content='messages_fts_trigram_src', content_rowid='id', tokenize='trigram'
    );
    CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages WHEN new.role <> 'tool' BEGIN
        INSERT INTO messages_fts_trigram(rowid, content, tool_name) VALUES (new.id, new.content, new.tool_name);
    END;
    CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages WHEN old.role <> 'tool' BEGIN
        INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name)
        VALUES ('delete', old.id, old.content, old.tool_name);
    END;
    CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE OF content, tool_name, role ON messages
    WHEN old.content IS NOT new.content OR old.tool_name IS NOT new.tool_name OR old.role IS NOT new.role
    BEGIN
        INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name)
        SELECT 'delete', old.id, old.content, old.tool_name WHERE old.role <> 'tool';
        INSERT INTO messages_fts_trigram(rowid, content, tool_name)
        SELECT new.id, new.content, new.tool_name WHERE new.role <> 'tool';
    END;
    """

    static let busyTimeoutMS = 5_000

    /// Connection pragmas, then schema creation, column reconciliation, data migrations and FTS setup in one transaction.
    /// Another opener of the same file may hold the lock (the WAL switch can report busy without waiting), so a busy
    /// setup is retried until `patience` runs out.
    static func prepare(_ conn: Connection, patience: TimeInterval = 15) throws -> FTSState {
        try conn.exec("PRAGMA busy_timeout=\(busyTimeoutMS)")
        let deadline = Date().addingTimeInterval(patience)
        while true {
            do {
                return try prepareOnce(conn)
            } catch let error as SQLiteError where error.isBusy && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.01)
            }
        }
    }

    private static func prepareOnce(_ conn: Connection) throws -> FTSState {
        let mode = try conn.one("PRAGMA journal_mode=WAL")?.string("journal_mode")
        if mode?.lowercased() != "wal" {
            throw SQLiteError(code: SQLITE_ERROR, message: "could not enable WAL (journal_mode=\(mode ?? "nil"))")
        }
        try conn.exec("PRAGMA foreign_keys=ON")
        return try conn.transaction {
            try conn.exec(tablesSQL)
            try reconcileColumns(conn)
            try conn.exec(indexSQL)
            let fts5 = try supportsFTS5(conn)
            let stale = try conn.one("SELECT 1 AS hit FROM state_meta WHERE key = ?", [staleKey]) != nil
            if stale || !fts5 {
                try dropTriggers(conn, baseTriggers + trigramTriggers)
            }
            try stampVersion(conn)
            guard fts5 else { return FTSState(enabled: false, trigramAvailable: false, stale: false) }
            return try initFTS(conn, stale: stale)
        }
    }

    private static func stampVersion(_ conn: Connection) throws {
        guard let stored = try conn.one("SELECT version FROM schema_version LIMIT 1")?.int("version") else {
            try conn.run("INSERT INTO schema_version (version) VALUES (?)", [version])
            let created = ISO8601DateFormatter().string(from: Date())
            try conn.run("INSERT OR IGNORE INTO state_meta (key, value) VALUES ('store_instance_id', ?), ('store_created_at_utc', ?)",
                         [UUID().uuidString.lowercased(), created])
            return
        }
        guard stored < version else { return }
        for migration in migrations where migration.version > stored {
            try migration.run(conn)
        }
        try conn.run("UPDATE schema_version SET version = ?", [version])
    }

    /// Expected columns per table, read back from `tablesSQL` executed in a scratch in-memory database.
    static func declaredColumns() throws -> [String: [(name: String, definition: String)]] {
        let reference = try Connection(path: ":memory:")
        try reference.exec(tablesSQL)
        var tables: [String: [(String, String)]] = [:]
        for table in try reference.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'") {
            guard let name = table.string("name") else { continue }
            tables[name] = try reference.query("SELECT * FROM pragma_table_info(?)", [name]).map { col in
                var parts: [String] = []
                if let type = col.string("type"), !type.isEmpty { parts.append(type) }
                if col.bool("notnull") && !col.bool("pk") { parts.append("NOT NULL") }
                if let dflt = col.string("dflt_value") { parts.append("DEFAULT \(dflt)") }
                return (col.string("name") ?? "", parts.joined(separator: " "))
            }
        }
        return tables
    }

    /// Adds every declared column a live table is missing, so a store written by an older build reads with today's queries.
    static func reconcileColumns(_ conn: Connection) throws {
        for (table, columns) in try declaredColumns() {
            let live = Set(try conn.query("SELECT name FROM pragma_table_info(?)", [table]).compactMap { $0.string("name") })
            guard !live.isEmpty else { continue }
            for column in columns where !live.contains(column.name) {
                try conn.exec("ALTER TABLE \"\(table)\" ADD COLUMN \"\(column.name)\" \(column.definition)")
            }
        }
    }

    private static func supportsFTS5(_ conn: Connection) throws -> Bool {
        do {
            try conn.exec("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x); DROP TABLE temp._fts5_probe;")
            return true
        } catch let error as SQLiteError where error.message.contains("no such module") {
            return false
        }
    }

    private static func supportsTrigram(_ conn: Connection) throws -> Bool {
        do {
            try conn.exec("CREATE VIRTUAL TABLE temp._trigram_probe USING fts5(x, tokenize='trigram'); DROP TABLE temp._trigram_probe;")
            return true
        } catch let error as SQLiteError where error.message.contains("no such tokenizer") {
            return false
        }
    }

    static func dropTriggers(_ conn: Connection, _ names: [String]) throws {
        for name in names { try conn.exec("DROP TRIGGER IF EXISTS \(name)") }
    }

    private static func triggersMissing(_ conn: Connection, _ names: [String]) throws -> Bool {
        let marks = names.map { _ in "?" }.joined(separator: ",")
        let count = try conn.one("SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'trigger' AND name IN (\(marks))", names)?.int("n") ?? 0
        return count < names.count
    }

    /// Creates or repairs the FTS objects. An index whose sync triggers were absent has a gap of unknown extent,
    /// so it is rebuilt from `messages` before the triggers resume; a stale breadcrumb forces the same full rebuild.
    private static func initFTS(_ conn: Connection, stale: Bool) throws -> FTSState {
        let trigram = try supportsTrigram(conn)
        if stale {
            try conn.exec("DROP TABLE IF EXISTS messages_fts; DROP TABLE IF EXISTS messages_fts_trigram;")
        }
        let baseMissing = try stale || triggersMissing(conn, baseTriggers)
        let trigramMissing = try stale || triggersMissing(conn, trigramTriggers)
        try conn.exec(ftsSQL)
        if baseMissing { try conn.exec("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')") }
        if trigram {
            try conn.exec(trigramSQL)
            if trigramMissing { try conn.exec("INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')") }
        }
        try conn.run("DELETE FROM state_meta WHERE key = ?", [staleKey])
        return FTSState(enabled: true, trigramAvailable: trigram, stale: false)
    }

    /// FTS-scoped corruption: SQLITE_CORRUPT_VTAB, or a corruption report from fts5. A bare malformed image is structural.
    static func isFTSCorruption(_ error: Error) -> Bool {
        guard let error = error as? SQLiteError else { return false }
        return error.code == 267 || (error.isCorruption && error.message.lowercased().contains("fts5"))
    }

    /// Detaches the damaged indexes so canonical writes continue. The breadcrumb and the trigger drop commit together:
    /// once the triggers are gone nobody may reinstall them without the full rebuild the next open performs.
    static func enterFailOpen(_ conn: Connection) throws {
        try conn.transaction {
            try conn.run("INSERT INTO state_meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         [staleKey])
            try dropTriggers(conn, baseTriggers + trigramTriggers)
        }
    }
}

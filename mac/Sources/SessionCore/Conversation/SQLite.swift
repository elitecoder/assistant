// Thin SQLite3 C API wrapper for the conversation store (connection and statement setup from Hermes Agent's hermes_state.py).
import Foundation
import SQLite3

public struct SQLiteError: Error, CustomStringConvertible, Equatable {
    public let code: Int32
    public let message: String

    public var description: String { "SQLite error \(code): \(message)" }

    /// SQLITE_CORRUPT family, which includes SQLITE_CORRUPT_VTAB raised by a damaged FTS5 index.
    var isCorruption: Bool { code & 0xFF == SQLITE_CORRUPT }

    /// Another connection holds the lock the statement needs.
    var isBusy: Bool { code & 0xFF == SQLITE_BUSY || code & 0xFF == SQLITE_LOCKED }
}

private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

enum SQLValue: Equatable {
    case null
    case int(Int64)
    case double(Double)
    case text(String)
}

protocol SQLBindable {
    var sqlValue: SQLValue { get }
}

extension Int: SQLBindable { var sqlValue: SQLValue { .int(Int64(self)) } }
extension Int64: SQLBindable { var sqlValue: SQLValue { .int(self) } }
extension Double: SQLBindable { var sqlValue: SQLValue { .double(self) } }
extension String: SQLBindable { var sqlValue: SQLValue { .text(self) } }
extension Bool: SQLBindable { var sqlValue: SQLValue { .int(self ? 1 : 0) } }
extension SQLValue: SQLBindable { var sqlValue: SQLValue { self } }
extension Optional: SQLBindable where Wrapped: SQLBindable {
    var sqlValue: SQLValue { self?.sqlValue ?? .null }
}

/// One row read by `Connection.query`. Columns are looked up by name.
struct Row {
    private let values: [String: SQLValue]

    init(_ values: [String: SQLValue]) { self.values = values }

    subscript(_ column: String) -> SQLValue { values[column] ?? .null }

    func string(_ column: String) -> String? {
        switch self[column] {
        case .text(let s): return s
        case .int(let i): return String(i)
        case .double(let d): return String(d)
        case .null: return nil
        }
    }

    func int(_ column: String) -> Int? {
        switch self[column] {
        case .int(let i): return Int(i)
        case .double(let d): return Int(d)
        case .text(let s): return Int(s)
        case .null: return nil
        }
    }

    func int64(_ column: String) -> Int64? { int(column).map(Int64.init) }

    func double(_ column: String) -> Double? {
        switch self[column] {
        case .double(let d): return d
        case .int(let i): return Double(i)
        case .text(let s): return Double(s)
        case .null: return nil
        }
    }

    func bool(_ column: String) -> Bool { (int(column) ?? 0) != 0 }
}

/// A single SQLite connection. Not thread-safe; `ConversationStore` serializes access to it.
final class Connection {
    private(set) var handle: OpaquePointer?

    init(path: String, flags: Int32 = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_NOMUTEX) throws {
        var db: OpaquePointer?
        let rc = sqlite3_open_v2(path, &db, flags, nil)
        guard rc == SQLITE_OK, let db else {
            let message = db.map { String(cString: sqlite3_errmsg($0)) } ?? "cannot open \(path)"
            sqlite3_close_v2(db)
            throw SQLiteError(code: rc, message: message)
        }
        handle = db
        sqlite3_extended_result_codes(db, 1)
    }

    deinit { close() }

    func close() {
        guard let handle else { return }
        sqlite3_close_v2(handle)
        self.handle = nil
    }

    private func error(_ rc: Int32) -> SQLiteError {
        SQLiteError(code: rc, message: handle.map { String(cString: sqlite3_errmsg($0)) } ?? "closed")
    }

    /// Runs one or more statements with no parameters and no result rows.
    func exec(_ sql: String) throws {
        let rc = sqlite3_exec(handle, sql, nil, nil, nil)
        guard rc == SQLITE_OK else { throw error(rc) }
    }

    private func withStatement<T>(_ sql: String, _ params: [SQLBindable], _ body: (OpaquePointer) throws -> T) throws -> T {
        var stmt: OpaquePointer?
        let rc = sqlite3_prepare_v2(handle, sql, -1, &stmt, nil)
        guard rc == SQLITE_OK, let stmt else { throw error(rc) }
        defer { sqlite3_finalize(stmt) }
        for (index, param) in params.enumerated() {
            let position = Int32(index + 1)
            let bound: Int32
            switch param.sqlValue {
            case .null: bound = sqlite3_bind_null(stmt, position)
            case .int(let i): bound = sqlite3_bind_int64(stmt, position, i)
            case .double(let d): bound = sqlite3_bind_double(stmt, position, d)
            case .text(let s):
                let bytes = Array(s.utf8)
                bound = bytes.withUnsafeBufferPointer { buffer in
                    buffer.baseAddress.map { base in
                        base.withMemoryRebound(to: CChar.self, capacity: bytes.count) {
                            sqlite3_bind_text(stmt, position, $0, Int32(bytes.count), transient)
                        }
                    } ?? sqlite3_bind_text(stmt, position, "", 0, transient)
                }
            }
            guard bound == SQLITE_OK else { throw error(bound) }
        }
        return try body(stmt)
    }

    /// Runs one statement and returns the number of rows it changed.
    @discardableResult
    func run(_ sql: String, _ params: [SQLBindable] = []) throws -> Int {
        try withStatement(sql, params) { stmt in
            var rc = sqlite3_step(stmt)
            while rc == SQLITE_ROW { rc = sqlite3_step(stmt) }
            guard rc == SQLITE_DONE else { throw error(rc) }
            return Int(sqlite3_changes(handle))
        }
    }

    func query(_ sql: String, _ params: [SQLBindable] = []) throws -> [Row] {
        try withStatement(sql, params) { stmt in
            var rows: [Row] = []
            let count = sqlite3_column_count(stmt)
            let names = (0..<count).map { String(cString: sqlite3_column_name(stmt, $0)) }
            while true {
                let rc = sqlite3_step(stmt)
                if rc == SQLITE_DONE { break }
                guard rc == SQLITE_ROW else { throw error(rc) }
                var values: [String: SQLValue] = [:]
                for i in 0..<count {
                    switch sqlite3_column_type(stmt, i) {
                    case SQLITE_INTEGER: values[names[Int(i)]] = .int(sqlite3_column_int64(stmt, i))
                    case SQLITE_FLOAT: values[names[Int(i)]] = .double(sqlite3_column_double(stmt, i))
                    case SQLITE_NULL: values[names[Int(i)]] = .null
                    default:
                        let bytes = UnsafeBufferPointer(start: sqlite3_column_text(stmt, i),
                                                        count: Int(sqlite3_column_bytes(stmt, i)))
                        values[names[Int(i)]] = .text(String(decoding: bytes, as: UTF8.self))
                    }
                }
                rows.append(Row(values))
            }
            return rows
        }
    }

    func one(_ sql: String, _ params: [SQLBindable] = []) throws -> Row? {
        try query(sql, params).first
    }

    var lastInsertRowID: Int64 { sqlite3_last_insert_rowid(handle) }

    /// `BEGIN IMMEDIATE` … `COMMIT`, rolled back when `body` throws.
    func transaction<T>(_ body: () throws -> T) throws -> T {
        try exec("BEGIN IMMEDIATE")
        do {
            let result = try body()
            try exec("COMMIT")
            return result
        } catch {
            try? exec("ROLLBACK")
            throw error
        }
    }
}

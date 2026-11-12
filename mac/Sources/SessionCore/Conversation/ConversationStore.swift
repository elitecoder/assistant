// Ported from Hermes Agent (MIT): hermes_state.py, hermes_state_sessions.py, hermes_state_messages.py, hermes_state_titles.py, hermes_state_compression.py.
import Foundation
import SQLite3

public enum ConversationStoreError: Error, Equatable, CustomStringConvertible {
    case sessionNotFound(String)
    case titleTooLong(length: Int, limit: Int)
    case nothingToCompact(sessionID: String, seqRanges: [ClosedRange<Int>])
    case resumeTooLarge(count: Int, limit: Int)
    case negativeLimit(Int)
    case notActive(sessionID: String, seq: Int)
    case foreignMessage(sessionID: String, messageSessionID: String)
    case brokenToolPairing(toolCallID: String)
    case emptyCompaction(sessionID: String)
    case invalidWatermark(watermark: Int, highestCarried: Int?)
    case orphanToolResult(sessionID: String, toolCallID: String)

    public var description: String {
        switch self {
        case .sessionNotFound(let id):
            return "Session not found: \(id)"
        case .titleTooLong(let length, let limit):
            return "Title too long (\(length) chars, max \(limit))"
        case .nothingToCompact(let id, let ranges):
            let listed = ranges.map { "\($0.lowerBound)...\($0.upperBound)" }.joined(separator: ", ")
            return "Session \(id) has no active messages in seq \(listed) to compact"
        case .resumeTooLarge(let count, let limit):
            return "This session is too long to reload safely (\(count) messages; limit \(limit)). Start a fresh chat."
        case .notActive(let id, let seq):
            return "Seq \(seq) is not an active message of session \(id) (or is listed twice)"
        case .foreignMessage(let id, let other):
            return "A message of session \(other) cannot be carried into session \(id)"
        case .brokenToolPairing(let callID):
            return "Compaction would split tool call \(callID) from its result"
        case .invalidWatermark(let watermark, let highest):
            if let highest { return "Watermark \(watermark) is below carried seq \(highest); pass the highest seq the compactor read" }
            return "Watermark \(watermark) is negative; pass the highest seq the compactor read"
        case .orphanToolResult(let id, let callID):
            return "Session \(id) has no live tool call \(callID) for this result (its call is archived or missing)"
        case .emptyCompaction(let id):
            return "Compaction of session \(id) with no messages would archive everything; pass allowEmpty to do that"
        case .negativeLimit(let value):
            return "max messages must be non-negative (got \(value))"
        }
    }
}

/// Which stored rows a read returns.
public enum MessageScope: Sendable {
    /// The live context: rows not folded into a compaction summary.
    case active
    /// What the person saw: live rows plus compaction-archived history, without the superseded copies compaction leaves behind.
    case display
    /// Every stored row.
    case all

    var clause: String {
        switch self {
        case .active: return " AND active = 1"
        case .display: return " AND (active = 1 OR compacted = 1)"
        case .all: return ""
        }
    }
}

public enum ArchiveFilter: Sendable {
    case unarchived, archived, all
}

/// A message to append; the store assigns its row id and seq.
public struct MessageDraft: Equatable, Sendable {
    public var role: MessageRole
    public var content: String
    public var toolName: String?
    public var toolCallID: String?
    public var timestamp: Date

    public init(role: MessageRole, content: String, toolName: String? = nil, toolCallID: String? = nil,
                timestamp: Date = Date()) {
        self.role = role
        self.content = content
        self.toolName = toolName
        self.toolCallID = toolCallID
        self.timestamp = timestamp
    }
}

/// What `compact` did: the summary that now stands for the archived messages, and where the live messages after it moved.
public struct CompactionResult: Equatable, Sendable {
    public var summary: ConversationMessage
    /// Old seq to new seq for every live message re-sequenced after the summary.
    public var resequenced: [Int: Int]
}

/// What `applyCompaction` did, in seqs.
public struct CompactionApplied: Equatable, Sendable {
    /// Each carried message's old seq to its new one.
    public var oldToNew: [Int: Int]
    /// New seqs of the summary messages.
    public var summarySeqs: [Int]
    /// Old seqs of rows left out and archived as searchable history (rewritten carried rows are archived too, but listed in `oldToNew`).
    public var archivedSeqs: [Int]
    /// Rows that arrived after the watermark: old seq to the new seq they were copied to, after `newActive`.
    public var lateSeqs: [Int: Int]
}

/// A row in the chat list.
public struct ConversationListItem: Equatable, Sendable {
    public var session: ConversationSession
    public var lastActive: Date
    /// Active (not compacted) messages.
    public var messageCount: Int
    /// The first user message, shortened to one line.
    public var preview: String
}

/// Messages either side of an anchor, in seq order. Fewer than `radius` on a side means that end of the session.
public struct MessageWindow: Equatable, Sendable {
    public var messages: [ConversationMessage]
    public var messagesBefore: Int
    public var messagesAfter: Int
}

/// SQLite-backed chat history. Every call runs on one serial queue, so the store is safe to share across threads.
///
/// Tool results: `append` refuses a result whose call id names no live assistant call, because the app always appends
/// a call before its result and an orphan result breaks the next request to the provider. A result with an empty or
/// missing call id is accepted as plain tool output, as Hermes allows. A call interrupted by a later user message or
/// assistant text can be archived by compaction; a result that arrives for it afterwards is refused, so it is dropped.
public final class ConversationStore: @unchecked Sendable {
    public static let maxTitleLength = 100
    /// Hermes `sessions.max_resume_messages` default; 0 disables the guard.
    public static let defaultMaxResumeMessages = 20_000
    static let previewMaxChars = 60

    public static var defaultURL: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Assistant/conversations.db")
    }

    public let url: URL
    let conn: Connection
    var fts: FTSState
    private let queue = DispatchQueue(label: "ConversationStore")

    public init(url: URL = ConversationStore.defaultURL) throws {
        self.url = url
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        Self.secureFiles(url)
        conn = try Connection(path: url.path)
        fts = try ConversationSchema.prepare(conn)
    }

    /// The store and its WAL sidecars are readable only by the user: transcripts can hold anything.
    /// SQLite gives sidecars it creates later the main file's mode. O_EXCL creates only a brand-new file: opening and
    /// closing an existing one would drop the POSIX locks another connection in this process holds on it.
    private static func secureFiles(_ url: URL) {
        let fm = FileManager.default
        let fd = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        if fd >= 0 { close(fd) }
        for path in [url.path, url.path + "-wal", url.path + "-shm"] where fm.fileExists(atPath: path) {
            try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
        }
    }

    func sync<T>(_ body: () throws -> T) rethrows -> T {
        try queue.sync(execute: body)
    }

    // MARK: Sessions

    /// Creates a session, or returns the stored one unchanged when `id` already exists.
    @discardableResult
    public func createSession(title: String, parentID: String? = nil, id: String = UUID().uuidString,
                              createdAt: Date = Date()) throws -> ConversationSession {
        let title = try Self.sanitizeTitle(title)
        return try sync {
            if let parentID, try sessionRow(parentID) == nil { throw ConversationStoreError.sessionNotFound(parentID) }
            try conn.run("INSERT OR IGNORE INTO sessions (id, title, parent_session_id, started_at) VALUES (?, ?, ?, ?)",
                         [id, title, parentID, createdAt.timeIntervalSince1970])
            guard let session = try sessionRow(id) else { throw ConversationStoreError.sessionNotFound(id) }
            return session
        }
    }

    public func session(id: String) throws -> ConversationSession? {
        try sync { try sessionRow(id) }
    }

    func sessionRow(_ id: String) throws -> ConversationSession? {
        try conn.one("SELECT * FROM sessions WHERE id = ?", [id]).map(Self.session(from:))
    }

    static func session(from row: Row) -> ConversationSession {
        ConversationSession(id: row.string("id") ?? "", title: row.string("title") ?? "",
                            parentID: row.string("parent_session_id"),
                            createdAt: Date(timeIntervalSince1970: row.double("started_at") ?? 0),
                            archived: row.bool("archived"), lastPromptTokens: row.int("last_prompt_tokens"),
                            lastPromptTokensAtSeq: row.int("last_prompt_tokens_at_seq"))
    }

    static let summarySelect = """
    SELECT s.*, COALESCE(s.last_activity_at, s.started_at) AS last_active,
           COALESCE((SELECT REPLACE(REPLACE(SUBSTR(m.content, 1, 63), char(10), ' '), char(13), ' ')
                     FROM messages m WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                       AND m._compressed_summary = 0 ORDER BY m.seq LIMIT 1), '') AS preview_raw
    FROM sessions s
    """

    static func summary(from row: Row) -> ConversationListItem {
        ConversationListItem(session: session(from: row), lastActive: Date(timeIntervalSince1970: row.double("last_active") ?? 0),
                       messageCount: row.int("message_count") ?? 0, preview: shapePreview(row.string("preview_raw") ?? ""))
    }

    static func shapePreview(_ raw: String) -> String {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.count > previewMaxChars ? String(text.prefix(previewMaxChars)) + "..." : text
    }

    /// Sessions most recently active first.
    public func sessions(archive: ArchiveFilter = .unarchived, limit: Int = 50, offset: Int = 0) throws -> [ConversationListItem] {
        let filter: String
        switch archive {
        case .unarchived: filter = " WHERE s.archived = 0"
        case .archived: filter = " WHERE s.archived = 1"
        case .all: filter = ""
        }
        return try sync {
            try conn.query(Self.summarySelect + filter + " ORDER BY last_active DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?",
                           [limit, offset]).map(Self.summary(from:))
        }
    }

    /// Strips control, zero-width and bidi characters and collapses whitespace; throws past `maxTitleLength`.
    public static func sanitizeTitle(_ title: String) throws -> String {
        let invisible = try NSRegularExpression(pattern: "[\\x{00}-\\x{08}\\x{0B}\\x{0C}\\x{0E}-\\x{1F}\\x{7F}\\x{200B}-\\x{200F}\\x{2028}-\\x{202E}\\x{2060}-\\x{2069}\\x{FEFF}\\x{FFF9}-\\x{FFFC}]")
        let stripped = invisible.stringByReplacingMatches(in: title, range: NSRange(title.startIndex..., in: title), withTemplate: "")
        let cleaned = stripped.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard cleaned.count <= maxTitleLength else {
            throw ConversationStoreError.titleTooLong(length: cleaned.count, limit: maxTitleLength)
        }
        return cleaned
    }

    public func rename(sessionID: String, to title: String) throws {
        let title = try Self.sanitizeTitle(title)
        try sync {
            guard try conn.run("UPDATE sessions SET title = ? WHERE id = ?", [title, sessionID]) == 1 else {
                throw ConversationStoreError.sessionNotFound(sessionID)
            }
        }
    }

    /// Hides or restores a session in the default list. Its messages are kept and stay searchable.
    public func setArchived(sessionID: String, _ archived: Bool) throws {
        try sync {
            guard try conn.run("UPDATE sessions SET archived = ? WHERE id = ?", [archived, sessionID]) == 1 else {
                throw ConversationStoreError.sessionNotFound(sessionID)
            }
        }
    }

    /// The session and its ancestors, root first.
    public func lineage(of sessionID: String) throws -> [ConversationSession] {
        try sync { try lineageRows(sessionID) }
    }

    /// Parent walk with a cycle guard and the same 100-hop bound Hermes uses.
    func lineageRows(_ sessionID: String) throws -> [ConversationSession] {
        var chain: [ConversationSession] = []
        var seen = Set<String>()
        var current: String? = sessionID
        while let id = current, !seen.contains(id), chain.count < 100, let session = try sessionRow(id) {
            seen.insert(id)
            chain.append(session)
            current = session.parentID
        }
        return chain.reversed()
    }

    /// Deletes a session and its messages. Child sessions are kept and become roots.
    @discardableResult
    public func deleteSession(id: String) throws -> Bool {
        try sync {
            try writeRetryingFTS {
                try conn.transaction {
                    guard try conn.one("SELECT 1 AS hit FROM sessions WHERE id = ?", [id]) != nil else { return false }
                    try conn.run("UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?", [id])
                    try conn.run("DELETE FROM messages WHERE session_id = ?", [id])
                    try conn.run("DELETE FROM sessions WHERE id = ?", [id])
                    return true
                }
            }
        }
    }

    // MARK: Messages

    static func message(from row: Row) -> ConversationMessage {
        ConversationMessage(id: row.int64("id"), sessionID: row.string("session_id") ?? "", seq: row.int("seq") ?? 0,
                            role: MessageRole(rawValue: row.string("role") ?? "") ?? .user, content: row.string("content") ?? "",
                            toolName: row.string("tool_name"), toolCallID: row.string("tool_call_id"),
                            timestamp: Date(timeIntervalSince1970: row.double("timestamp") ?? 0),
                            active: row.bool("active"), isSummary: row.bool("_compressed_summary"))
    }

    @discardableResult
    public func append(sessionID: String, role: MessageRole, content: String, toolName: String? = nil,
                       toolCallID: String? = nil, timestamp: Date = Date()) throws -> ConversationMessage {
        try append(sessionID: sessionID, [MessageDraft(role: role, content: content, toolName: toolName,
                                                        toolCallID: toolCallID, timestamp: timestamp)])[0]
    }

    /// Appends `drafts` in one transaction: all land or none. Each gets the next seq in the session.
    @discardableResult
    public func append(sessionID: String, _ drafts: [MessageDraft]) throws -> [ConversationMessage] {
        guard !drafts.isEmpty else { return [] }
        return try sync {
            try writeRetryingFTS {
                try conn.transaction {
                    guard try conn.one("SELECT 1 AS hit FROM sessions WHERE id = ?", [sessionID]) != nil else {
                        throw ConversationStoreError.sessionNotFound(sessionID)
                    }
                    try checkResultsHaveLiveCalls(sessionID, drafts)
                    var seq = try nextSeq(sessionID)
                    var inserted: [ConversationMessage] = []
                    for draft in drafts {
                        inserted.append(try insertMessage(sessionID: sessionID, seq: seq, draft: draft, isSummary: false))
                        seq += 1
                    }
                    try conn.run("UPDATE sessions SET message_count = message_count + ?, last_activity_at = MAX(COALESCE(last_activity_at, 0), ?) WHERE id = ?",
                                 [drafts.count, drafts.map(\.timestamp.timeIntervalSince1970).max() ?? 0, sessionID])
                    return inserted
                }
            }
        }
    }

    /// A tool result must answer a call that is live: an earlier draft of the batch or an active assistant row.
    /// A result for an archived or unknown call would be an orphan no provider accepts.
    private func checkResultsHaveLiveCalls(_ sessionID: String, _ drafts: [MessageDraft]) throws {
        var batchCalls = Set<String>()
        for draft in drafts {
            guard let id = draft.toolCallID, !id.isEmpty else { continue }
            if draft.role == .assistant {
                batchCalls.insert(id)
            } else if draft.role == .tool, !batchCalls.contains(id),
                      try conn.one("SELECT 1 AS hit FROM messages WHERE session_id = ? AND active = 1 AND role = 'assistant' AND tool_call_id = ?",
                                   [sessionID, id]) == nil {
                throw ConversationStoreError.orphanToolResult(sessionID: sessionID, toolCallID: id)
            }
        }
    }

    func nextSeq(_ sessionID: String) throws -> Int {
        (try conn.one("SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM messages WHERE session_id = ?", [sessionID])?.int("next")) ?? 1
    }

    private func insertMessage(sessionID: String, seq: Int, draft: MessageDraft, isSummary: Bool) throws -> ConversationMessage {
        try conn.run("""
            INSERT INTO messages (session_id, seq, role, content, tool_name, tool_call_id, timestamp, active, _compressed_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """, [sessionID, seq, draft.role.rawValue, draft.content, draft.toolName, draft.toolCallID,
                  draft.timestamp.timeIntervalSince1970, isSummary])
        return ConversationMessage(id: conn.lastInsertRowID, sessionID: sessionID, seq: seq, role: draft.role,
                                   content: draft.content, toolName: draft.toolName, toolCallID: draft.toolCallID,
                                   timestamp: Date(timeIntervalSince1970: draft.timestamp.timeIntervalSince1970),
                                   active: true, isSummary: isSummary)
    }

    /// A write that fails on a corrupt FTS index detaches the index (search falls back to LIKE until the next open
    /// rebuilds it) and is retried once, so the transcript write is never lost to derived index damage.
    func writeRetryingFTS<T>(_ body: () throws -> T) throws -> T {
        do {
            return try body()
        } catch where fts.enabled && ConversationSchema.isFTSCorruption(error) {
            try ConversationSchema.enterFailOpen(conn)
            fts = FTSState(enabled: false, trigramAvailable: false, stale: true)
            return try body()
        }
    }

    /// Messages in seq order.
    public func messages(sessionID: String, scope: MessageScope = .active) throws -> [ConversationMessage] {
        try sync {
            try conn.query("SELECT * FROM messages WHERE session_id = ?\(scope.clause) ORDER BY seq", [sessionID])
                .map(Self.message(from:))
        }
    }

    /// Up to `radius` messages either side of `seq`, from what the person saw: live and compaction-archived rows,
    /// never the superseded copies compaction leaves (the anchor itself is always kept). Empty when `seq` is not in the session.
    public func window(sessionID: String, aroundSeq seq: Int, radius: Int = 5) throws -> MessageWindow {
        try sync { try windowRows(sessionID, seq, radius) }
    }

    /// `n + 1` as a SQL LIMIT; past Int.max it is -1, which SQLite reads as no limit.
    static func limitPlusOne(_ n: Int) -> Int {
        let (next, overflow) = n.addingReportingOverflow(1)
        return overflow ? -1 : next
    }

    static let shownClause = "(active = 1 OR compacted = 1 OR seq = ?)"

    func windowRows(_ sessionID: String, _ seq: Int, _ radius: Int) throws -> MessageWindow {
        let radius = max(radius, 0)
        guard try conn.one("SELECT 1 AS hit FROM messages WHERE session_id = ? AND seq = ?", [sessionID, seq]) != nil else {
            return MessageWindow(messages: [], messagesBefore: 0, messagesAfter: 0)
        }
        let before = try conn.query("SELECT * FROM messages WHERE session_id = ? AND seq <= ? AND \(Self.shownClause) ORDER BY seq DESC LIMIT ?",
                                    [sessionID, seq, seq, Self.limitPlusOne(radius)])
        let after = try conn.query("SELECT * FROM messages WHERE session_id = ? AND seq > ? AND \(Self.shownClause) ORDER BY seq ASC LIMIT ?",
                                   [sessionID, seq, seq, radius])
        return MessageWindow(messages: (before.reversed() + after).map(Self.message(from:)),
                             messagesBefore: max(0, before.count - 1), messagesAfter: after.count)
    }

    /// The live context to send to the model, refused when it is too long to reload safely. `maxMessages` 0 disables the guard.
    public func resumeMessages(sessionID: String,
                               maxMessages: Int = ConversationStore.defaultMaxResumeMessages) throws -> [ConversationMessage] {
        guard maxMessages >= 0 else { throw ConversationStoreError.negativeLimit(maxMessages) }
        return try sync {
            if maxMessages > 0 {
                let count = try conn.one("SELECT COUNT(*) AS n FROM (SELECT 1 FROM messages WHERE session_id = ? AND active = 1 LIMIT ?)",
                                         [sessionID, Self.limitPlusOne(maxMessages)])?.int("n") ?? 0
                if count > maxMessages { throw ConversationStoreError.resumeTooLarge(count: count, limit: maxMessages) }
            }
            return try conn.query("SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY seq", [sessionID])
                .map(Self.message(from:))
        }
    }

    /// The live seq that now carries the message stored at `seq`: `seq` itself while live, else the single live copy
    /// compaction re-sequenced it into (same role, content and timestamp, higher seq). Nil when neither exists or the copy is ambiguous.
    public func resolveActiveSeq(sessionID: String, seq: Int) throws -> Int? {
        try sync {
            guard let origin = try conn.one("SELECT active FROM messages WHERE session_id = ? AND seq = ?", [sessionID, seq]) else {
                return nil
            }
            if origin.bool("active") { return seq }
            let copies = try conn.query("""
                SELECT c.seq FROM messages c JOIN messages o ON o.session_id = c.session_id AND o.seq = ?
                WHERE c.session_id = ? AND c.active = 1 AND c.seq > o.seq AND c.role = o.role
                  AND c.content IS o.content AND c.timestamp = o.timestamp
                """, [seq, sessionID])
            return copies.count == 1 ? copies[0].int("seq") : nil
        }
    }

    // MARK: Compaction

    /// In-place compaction of one contiguous range. See `compact(sessionID:seqRanges:summary:role:timestamp:)`.
    @discardableResult
    public func compact(sessionID: String, seqRange: ClosedRange<Int>, summary: String, role: MessageRole = .user,
                        timestamp: Date = Date()) throws -> CompactionResult {
        try compact(sessionID: sessionID, seqRanges: [seqRange], summary: summary, role: role, timestamp: timestamp)
    }

    /// In-place compaction of `seqRanges`, built on `applyCompaction` so the same tool-pairing guards apply: the
    /// active messages in the ranges become archived history (inactive, still searchable) and `summary` takes their
    /// place at the first archived position. Live messages before that position keep their seqs; seqs are unique and
    /// only grow, so the summary gets a new seq and every live message after it is re-sequenced behind it (the result
    /// maps old seq to new). Their originals are superseded so search returns each message once. Throws instead of
    /// archiving a tool call still waiting for its result or splitting a call from its live result.
    @discardableResult
    public func compact(sessionID: String, seqRanges: [ClosedRange<Int>], summary: String, role: MessageRole = .user,
                        timestamp: Date = Date()) throws -> CompactionResult {
        try sync {
            try writeRetryingFTS {
                try conn.transaction {
                    guard try conn.one("SELECT 1 AS hit FROM sessions WHERE id = ?", [sessionID]) != nil else {
                        throw ConversationStoreError.sessionNotFound(sessionID)
                    }
                    let active = try conn.query("SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY seq", [sessionID])
                        .map(Self.message(from:))
                    let summaryRow = ConversationMessage(sessionID: sessionID, seq: 0, role: role, content: summary,
                                                         timestamp: timestamp, isSummary: true)
                    var newActive: [ConversationMessage] = []
                    var placed = false
                    for row in active {
                        if seqRanges.contains(where: { $0.contains(row.seq) }) {
                            if !placed { newActive.append(summaryRow) }
                            placed = true
                        } else {
                            newActive.append(row)
                        }
                    }
                    guard placed else { throw ConversationStoreError.nothingToCompact(sessionID: sessionID, seqRanges: seqRanges) }
                    let applied = try applyCompactionInTransaction(sessionID: sessionID, newActive: newActive,
                                                                   watermark: active.last?.seq ?? 0, timestamp: timestamp,
                                                                   keepUnchangedPrefix: true)
                    var stored = summaryRow
                    stored.seq = applied.summarySeqs[0]
                    stored.timestamp = Date(timeIntervalSince1970: timestamp.timeIntervalSince1970)
                    stored.id = try conn.one("SELECT id FROM messages WHERE session_id = ? AND seq = ?", [sessionID, stored.seq])?.int64("id")
                    return CompactionResult(summary: stored, resequenced: applied.oldToNew.merging(applied.lateSeqs) { a, _ in a })
                }
            }
        }
    }

    /// Hermes's in-place compaction: the compressor hands over the session's new live transcript and it replaces the
    /// active rows in one transaction.
    /// - A message with seq > 0 is carried: it must be an active row of this session. Carried unchanged, its old copy is
    ///   flagged superseded (hidden from search and windows). Carried rewritten (content, role, tool name or call id
    ///   differ), the original is archived instead, so its text stays searchable and visible in display history.
    /// - A message with seq <= 0 is new (a summary, a restated request) and gets `timestamp`.
    /// - Active rows up to `watermark` (the highest seq the compressor saw) that `newActive` leaves out are archived.
    ///   Active rows above it arrived while the summary was being written; they are copied after `newActive`, in order,
    ///   and stay live.
    /// - Every live tool result must follow a live assistant call with its call id, a live call whose result exists
    ///   must keep that result live, and a call still waiting for its result cannot be archived; otherwise it throws.
    /// - `watermark` must be at least the highest carried seq, or the late rows would land out of order; it throws.
    /// - An empty `newActive` would archive everything up to the watermark, so it throws unless `allowEmpty`.
    /// Everything lands in order with fresh seqs, and the usage anchor is cleared.
    @discardableResult
    public func applyCompaction(sessionID: String, newActive: [ConversationMessage], watermark: Int, timestamp: Date = Date(),
                                allowEmpty: Bool = false) throws -> CompactionApplied {
        guard !newActive.isEmpty || allowEmpty else { throw ConversationStoreError.emptyCompaction(sessionID: sessionID) }
        return try sync {
            try writeRetryingFTS {
                try conn.transaction {
                    try applyCompactionInTransaction(sessionID: sessionID, newActive: newActive, watermark: watermark,
                                                     timestamp: timestamp, keepUnchangedPrefix: false)
                }
            }
        }
    }

    /// The body of `applyCompaction` on the caller's transaction. `keepUnchangedPrefix` leaves the leading carried rows
    /// that match the live transcript row for row exactly where they are, instead of re-copying them.
    private func applyCompactionInTransaction(sessionID: String, newActive: [ConversationMessage], watermark: Int, timestamp: Date,
                                              keepUnchangedPrefix: Bool) throws -> CompactionApplied {
        guard watermark >= 0 else { throw ConversationStoreError.invalidWatermark(watermark: watermark, highestCarried: nil) }
        if let foreign = newActive.first(where: { $0.sessionID != sessionID }) {
            throw ConversationStoreError.foreignMessage(sessionID: sessionID, messageSessionID: foreign.sessionID)
        }
        guard try conn.one("SELECT 1 AS hit FROM sessions WHERE id = ?", [sessionID]) != nil else {
            throw ConversationStoreError.sessionNotFound(sessionID)
        }
        let activeRows = try conn.query("SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY seq", [sessionID])
            .map(Self.message(from:))
        let original = Dictionary(uniqueKeysWithValues: activeRows.map { ($0.seq, $0) })
        var carried = Set<Int>()
        for message in newActive where message.seq > 0 {
            guard original[message.seq] != nil, carried.insert(message.seq).inserted else {
                throw ConversationStoreError.notActive(sessionID: sessionID, seq: message.seq)
            }
        }
        if let highest = carried.max(), watermark < highest {
            throw ConversationStoreError.invalidWatermark(watermark: watermark, highestCarried: highest)
        }
        let late = activeRows.filter { $0.seq > watermark && !carried.contains($0.seq) }
        let lateSeqs = Set(late.map(\.seq))
        try Self.checkToolPairing(live: newActive + late, session: activeRows)
        let kept = keepUnchangedPrefix
            ? zip(newActive, activeRows).prefix { $0.seq == $1.seq && Self.sameMessage($0, $1) }.count : 0
        var archivedSeqs: [Int] = []
        for row in activeRows.dropFirst(kept) {
            let rewritten = newActive.first { $0.seq == row.seq }.map { !Self.sameMessage($0, row) }
            let archive = rewritten ?? !lateSeqs.contains(row.seq)
            if archive && rewritten == nil { archivedSeqs.append(row.seq) }
            try conn.run("UPDATE messages SET active = 0, compacted = ? WHERE session_id = ? AND seq = ?", [archive, sessionID, row.seq])
        }
        var seq = try nextSeq(sessionID) - 1
        var oldToNew: [Int: Int] = [:]
        var lateToNew: [Int: Int] = [:]
        var summarySeqs: [Int] = []
        for message in newActive.dropFirst(kept) + late {
            seq += 1
            let isNew = message.seq <= 0
            let draft = MessageDraft(role: message.role, content: message.content, toolName: message.toolName,
                                     toolCallID: message.toolCallID, timestamp: isNew ? timestamp : message.timestamp)
            _ = try insertMessage(sessionID: sessionID, seq: seq, draft: draft, isSummary: message.isSummary)
            if lateSeqs.contains(message.seq) { lateToNew[message.seq] = seq } else if !isNew { oldToNew[message.seq] = seq }
            if message.isSummary && isNew { summarySeqs.append(seq) }
        }
        try conn.run("""
            UPDATE sessions SET message_count = ?, last_prompt_tokens = NULL, last_prompt_tokens_at_seq = NULL,
                last_activity_at = MAX(COALESCE(last_activity_at, 0), ?)
            WHERE id = ?
            """, [newActive.count + late.count, timestamp.timeIntervalSince1970, sessionID])
        return CompactionApplied(oldToNew: oldToNew, summarySeqs: summarySeqs, archivedSeqs: archivedSeqs, lateSeqs: lateToNew)
    }

    /// A tool call or its result, as opposed to a turn of conversation.
    static func isToolTraffic(_ message: ConversationMessage) -> Bool {
        message.role == .tool || (message.role == .assistant && !(message.toolCallID ?? "").isEmpty)
    }

    static func sameMessage(_ a: ConversationMessage, _ b: ConversationMessage) -> Bool {
        a.role == b.role && a.content == b.content && a.toolName == b.toolName && a.toolCallID == b.toolCallID
    }

    /// Every live tool result follows a live assistant call with its call id, and a live call whose result exists in
    /// the session keeps a live result after it. A provider rejects a transcript that breaks either rule. An unanswered
    /// call still in flight (nothing but tool calls and results after it) stays live: archived, its result would arrive
    /// as an orphan. An unanswered call followed by a user message or assistant text was interrupted, as Hermes treats
    /// it, and may be archived; a result that arrives for it later is refused by `append` and dropped.
    static func checkToolPairing(live: [ConversationMessage], session: [ConversationMessage]) throws {
        let answered = Set(session.filter { $0.role == .tool }.compactMap(\.toolCallID))
        let liveCalls = Set(live.filter { $0.role == .assistant }.compactMap(\.toolCallID))
        for (i, row) in session.enumerated() where row.role == .assistant {
            guard let id = row.toolCallID, !id.isEmpty, !answered.contains(id), !liveCalls.contains(id) else { continue }
            let interrupted = session[(i + 1)...].contains { !Self.isToolTraffic($0) }
            if !interrupted { throw ConversationStoreError.brokenToolPairing(toolCallID: id) }
        }
        var calls = Set<String>()
        var results = Set<String>()
        for message in live {
            guard let id = message.toolCallID, !id.isEmpty else { continue }
            switch message.role {
            case .assistant: calls.insert(id)
            case .tool:
                guard calls.contains(id) else { throw ConversationStoreError.brokenToolPairing(toolCallID: id) }
                results.insert(id)
            default: break
            }
        }
        if let orphan = calls.first(where: { answered.contains($0) && !results.contains($0) }) {
            throw ConversationStoreError.brokenToolPairing(toolCallID: orphan)
        }
    }

    // MARK: Usage

    /// Records the provider's last reported prompt tokens and the seq of the newest message they covered.
    public func recordUsage(sessionID: String, promptTokens: Int, atSeq seq: Int) throws {
        try sync {
            guard try conn.run("UPDATE sessions SET last_prompt_tokens = ?, last_prompt_tokens_at_seq = ? WHERE id = ?",
                               [promptTokens, seq, sessionID]) == 1 else {
                throw ConversationStoreError.sessionNotFound(sessionID)
            }
        }
    }
}

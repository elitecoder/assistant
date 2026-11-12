// Ported from Hermes Agent (MIT): tools/session_search_tool.py, hermes_state_search.py (get_anchored_view), hermes_state_titles.py (resolve_session_by_title), tools/ansi_strip.py.
import Foundation

/// Arguments the model passes to `session_search`. Decodes leniently: numbers may arrive as strings and
/// `exclude_session_ids` as one id or a list.
public struct SessionSearchRequest: Decodable, Equatable, Sendable {
    public var query: String?
    public var limit: Int?
    public var sort: String?
    public var detail: String?
    public var after: String?
    public var before: String?
    public var excludeSessionIDs: [String]?
    public var sessionID: String?
    /// Raw `around_seq`; validated when the scroll shape runs so a bad value comes back as a tool error.
    public var aroundSeq: String?
    public var window: Int?
    public var roleFilter: String?

    public init(query: String? = nil, limit: Int? = nil, sort: String? = nil, detail: String? = nil, after: String? = nil,
                before: String? = nil, excludeSessionIDs: [String]? = nil, sessionID: String? = nil, aroundSeq: Int? = nil,
                window: Int? = nil, roleFilter: String? = nil) {
        self.query = query
        self.limit = limit
        self.sort = sort
        self.detail = detail
        self.after = after
        self.before = before
        self.excludeSessionIDs = excludeSessionIDs
        self.sessionID = sessionID
        self.aroundSeq = aroundSeq.map(String.init)
        self.window = window
        self.roleFilter = roleFilter
    }

    enum CodingKeys: String, CodingKey {
        case query, limit, sort, detail, after, before, window
        case excludeSessionIDs = "exclude_session_ids", sessionID = "session_id", aroundSeq = "around_seq", roleFilter = "role_filter"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func text(_ key: CodingKeys) throws -> String? {
            guard c.contains(key), try !c.decodeNil(forKey: key) else { return nil }
            if let s = try? c.decode(String.self, forKey: key) { return s }
            if let i = try? c.decode(Int.self, forKey: key) { return String(i) }
            if let d = try? c.decode(Double.self, forKey: key) { return Self.text(d) }
            if let b = try? c.decode(Bool.self, forKey: key) { return String(b) }
            throw SessionSearchArgumentError(message: "\(key.stringValue) must be a string or a number")
        }
        query = try text(.query)
        limit = try text(.limit).flatMap { Int($0) }
        sort = try text(.sort)
        detail = try text(.detail)
        after = try text(.after)
        before = try text(.before)
        sessionID = try text(.sessionID)
        aroundSeq = try text(.aroundSeq)
        window = try text(.window).flatMap { Int($0) }
        roleFilter = try text(.roleFilter)
        if var list = try? c.nestedUnkeyedContainer(forKey: .excludeSessionIDs) {
            var ids: [String] = []
            while !list.isAtEnd {
                if let s = try? list.decode(String.self) { ids.append(s) }
                else if let i = try? list.decode(Int.self) { ids.append(String(i)) }
                else if let d = try? list.decode(Double.self) { ids.append(Self.text(d)) }
                else { _ = try? list.decode(Skipped.self) }
            }
            excludeSessionIDs = ids
        } else {
            excludeSessionIDs = try text(.excludeSessionIDs).map { [$0] }
        }
    }

    /// A whole number arrives as `1.0` from some callers; it reads as `1`.
    private static func text(_ d: Double) -> String {
        d.rounded() == d && abs(d) < 9e18 ? String(Int(d)) : String(d)
    }

    private struct Skipped: Decodable {}
}

struct SessionSearchArgumentError: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

/// One message as the tool returns it.
public struct ShapedMessage: Encodable, Equatable, Sendable {
    public var seq: Int
    public var role: MessageRole
    public var content: String
    public var timestamp: Double
    public var toolName: String?
    public var toolCallID: String?
    public var anchor: Bool?
    public var contentTruncated: Bool?
    public var originalContentChars: Int?

    enum CodingKeys: String, CodingKey {
        case seq, role, content, timestamp, anchor
        case toolName = "tool_name", toolCallID = "tool_call_id", contentTruncated = "content_truncated"
        case originalContentChars = "original_content_chars"
    }
}

public struct SessionMeta: Encodable, Equatable, Sendable {
    public var when: String
    public var title: String?
}

public struct DiscoveryEntry: Encodable, Equatable, Sendable {
    public var sessionID: String
    public var when: String
    public var title: String?
    public var matchedRole: String
    public var matchSeq: Int?
    public var snippet: String
    public var bookendStart: [ShapedMessage]
    public var messages: [ShapedMessage]
    public var bookendEnd: [ShapedMessage]
    public var messagesBefore: Int
    public var messagesAfter: Int
    public var detail: String
    public var parentSessionID: String?

    enum CodingKeys: String, CodingKey {
        case when, title, snippet, messages, detail
        case sessionID = "session_id", matchedRole = "matched_role", matchSeq = "match_seq", bookendStart = "bookend_start"
        case bookendEnd = "bookend_end", messagesBefore = "messages_before", messagesAfter = "messages_after"
        case parentSessionID = "parent_session_id"
    }
}

public struct DiscoveryPayload: Encodable, Equatable, Sendable {
    public let success = true
    public let mode = "discover"
    public var query: String
    public var detail: String
    public var results: [DiscoveryEntry]
    public var count: Int
    public var sessionsSearched: Int?
    public var hint: String?
    public var message: String?

    enum CodingKeys: String, CodingKey {
        case success, mode, query, detail, results, count, hint, message
        case sessionsSearched = "sessions_searched"
    }
}

public struct ScrollPayload: Encodable, Equatable, Sendable {
    public let success = true
    public let mode = "scroll"
    public var sessionID: String
    public var aroundSeq: Int
    public var sessionMeta: SessionMeta
    public var window: Int
    public var messages: [ShapedMessage]
    public var messagesBefore: Int
    public var messagesAfter: Int
    public var hint: String

    enum CodingKeys: String, CodingKey {
        case success, mode, window, messages, hint
        case sessionID = "session_id", aroundSeq = "around_seq", sessionMeta = "session_meta"
        case messagesBefore = "messages_before", messagesAfter = "messages_after"
    }
}

public struct ReadPayload: Encodable, Equatable, Sendable {
    public let success = true
    public let mode = "read"
    public var sessionID: String
    public var sessionMeta: SessionMeta
    public var messageCount: Int
    public var truncated: Bool
    public var messages: [ShapedMessage]
    public var message: String?

    enum CodingKeys: String, CodingKey {
        case success, mode, truncated, messages, message
        case sessionID = "session_id", sessionMeta = "session_meta", messageCount = "message_count"
    }
}

public struct BrowseEntry: Encodable, Equatable, Sendable {
    public var sessionID: String
    public var title: String?
    public var startedAt: Double
    public var lastActive: Double
    public var messageCount: Int
    public var preview: String

    enum CodingKeys: String, CodingKey {
        case title, preview
        case sessionID = "session_id", startedAt = "started_at", lastActive = "last_active", messageCount = "message_count"
    }
}

public struct BrowsePayload: Encodable, Equatable, Sendable {
    public let success = true
    public let mode = "browse"
    public var results: [BrowseEntry]
    public var count: Int
    public var message: String

    enum CodingKeys: String, CodingKey { case success, mode, results, count, message }
}

public enum SessionSearchPayload: Equatable, Sendable {
    case discover(DiscoveryPayload)
    case scroll(ScrollPayload)
    case read(ReadPayload)
    case browse(BrowsePayload)
    case error(String)
}

struct ErrorPayload: Encodable {
    let error: String
    let success = false
}

/// What the tool returns: the typed payload for the app, and `text`, the JSON tool result for the model.
public struct SessionSearchResult: Equatable, Sendable {
    public var payload: SessionSearchPayload
    public var text: String

    init(_ payload: SessionSearchPayload) {
        self.payload = payload
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        encoder.nonConformingFloatEncodingStrategy = .convertToString(positiveInfinity: "Infinity", negativeInfinity: "-Infinity", nan: "NaN")
        func encode<T: Encodable>(_ value: T) -> String {
            if let data = try? encoder.encode(value) { return String(decoding: data, as: UTF8.self) }
            return #"{"error":"session_search could not encode its result","success":false}"#
        }
        switch payload {
        case .discover(let p): text = encode(p)
        case .scroll(let p): text = encode(p)
        case .read(let p): text = encode(p)
        case .browse(let p): text = encode(p)
        case .error(let message): text = encode(ErrorPayload(error: message))
        }
    }
}

/// The `session_search` tool: long-term recall over stored conversations. The shape is picked by the arguments:
/// `query` = discovery, `session_id` + `around_seq` = scroll, `session_id` alone = read, nothing = browse.
public enum SessionSearchTool {
    public static let name = "session_search"

    public static let description = """
    Search or read past Assistant conversations (FTS5), or scroll inside one. Four shapes, picked by args: \
    `query` = discovery (top-N matching sessions, top result fully hydrated); `session_id` + `around_seq` = scroll \
    (window of messages around an anchor); `session_id` alone = read a whole session; no args = browse recent \
    sessions. Results are actual stored messages, no LLM. Searches conversation history ONLY — when the user gave \
    a direct source (URL, file, contact, live system), inspect that first; never conclude 'not found' from history \
    alone. Use for questions about past conversations: 'what did we do about X', 'where did we leave Y'.
    """

    /// JSON Schema for the tool's input.
    public static let inputSchema = """
    {"type":"object","properties":{\
    "query":{"type":"string","description":"Search query (discovery shape). Keywords, phrases, or boolean expressions to find in past sessions. Omit to browse recent sessions. Ignored when session_id + around_seq are set (scroll shape)."},\
    "limit":{"type":"integer","description":"Max sessions to return. Discovery defaults to 3; browse/no-args defaults to 10. Max 10. Bump discovery to 5–10 when the topic likely spans several sessions and you want to pick the right one to scroll into."},\
    "sort":{"type":"string","enum":["newest","oldest"],"description":"Discovery shape only. Temporal bias on top of FTS5 ranking: omit for relevance-only (exploratory recall), 'newest' for \\"where did we leave X\\", 'oldest' for \\"how did X start\\"."},\
    "detail":{"type":"string","enum":["adaptive","full"],"default":"adaptive","description":"Discovery shape only. 'adaptive' (default) fully hydrates the top-ranked result and returns only the exact anchor message for lower-ranked results. 'full' returns bookends and the complete anchored window for every result."},\
    "after":{"type":"string","description":"Discovery shape only. Inclusive lower bound on session start time. ISO date/datetime (e.g. 2026-06-01) or relative duration (7d, 24h, 2w = within the last N). Use only when the user names a time frame. sort is a ranking bias, not a bound."},\
    "before":{"type":"string","description":"Discovery shape only. Exclusive upper bound on session start time. ISO date/datetime (a date-only value is midnight UTC that day) or relative duration (7d = older than a week). Use only when the user names a time frame."},\
    "exclude_session_ids":{"type":"array","items":{"type":"string"},"description":"Discovery shape only. Session ids already inspected this task. Those sessions are omitted so a later query explores instead of repeating the same hit. Cap 20."},\
    "session_id":{"type":"string","description":"Scroll or read shape. Session to read inside. Use the session_id returned from a prior discovery call. Pair with around_seq to scroll."},\
    "around_seq":{"type":"integer","description":"Scroll shape. Message seq to center the window on — use match_seq from a discovery result, or any seq from a prior window."},\
    "window":{"type":"integer","default":5,"description":"Scroll shape only. Messages to return on each side of the anchor (anchor itself always included). Clamped to [1, 20]. Default 5."},\
    "role_filter":{"type":"string","description":"Optional. Comma-separated roles to include. Discovery defaults to 'user,assistant' (tool output is usually noise). Pass 'user,assistant,tool' to include tool output (debugging tool behaviour) or 'tool' to search tool output only."}\
    },"required":[]}
    """

    static let readMaxContent = 2_000
    static let scrollMaxContent = 4_000
    static let discoverScanLimit = 300
    static let excludeSessionIDsCap = 20
    static let noMatchMessage = "No matching sessions found. FTS5 ANDs all terms by default — broaden with OR (`alpha OR beta`), "
        + "exact-match with quoted phrases, exclude with NOT, or prefix-match with `deploy*`."
    static let discoverHint = "To read more around a compact result, scroll: session_search(session_id=..., around_seq=match_seq)."
    static let scrollHint = "Scroll forward: re-call with around_seq = the LAST message's seq; backward: the FIRST message's seq "
        + "(the boundary message repeats as an orientation marker). messages_before/messages_after < window means you've hit "
        + "that end of the session."

    /// Runs the tool on its raw JSON input.
    public static func run(json: Data, store: ConversationStore, currentSessionID: String?, now: Date = Date(),
                           timeZone: TimeZone = .current) -> SessionSearchResult {
        let request: SessionSearchRequest
        do {
            request = try JSONDecoder().decode(SessionSearchRequest.self, from: json.isEmpty ? Data("{}".utf8) : json)
        } catch let error as SessionSearchArgumentError {
            return SessionSearchResult(.error(error.message))
        } catch {
            return SessionSearchResult(.error("session_search arguments must be a JSON object"))
        }
        return run(request, store: store, currentSessionID: currentSessionID, now: now, timeZone: timeZone)
    }

    public static func run(_ request: SessionSearchRequest, store: ConversationStore, currentSessionID: String?,
                           now: Date = Date(), timeZone: TimeZone = .current) -> SessionSearchResult {
        let context = Context(store: store, current: currentSessionID, now: now, timeZone: timeZone)
        do {
            return SessionSearchResult(try context.dispatch(request))
        } catch {
            return SessionSearchResult(.error("Search failed: \(error)"))
        }
    }

    static func clamp(_ value: Int?, default fallback: Int, _ lo: Int, _ hi: Int) -> Int {
        max(lo, min(value ?? fallback, hi))
    }

    private static let relativeBound = try! NSRegularExpression(pattern: "^(\\d+)\\s*(h|d|w)$", options: .caseInsensitive)

    /// ISO date or datetime (a bare date is midnight UTC), or a relative duration: "7d", "24h", "2w" = now minus N.
    static func parseBound(_ value: String?, now: Date) throws -> Date? {
        guard let text = value.map(FTSQuery.trimmed), !text.isEmpty else { return nil }
        let range = NSRange(text.startIndex..., in: text)
        if let m = relativeBound.firstMatch(in: text, range: range),
           let n = Range(m.range(at: 1), in: text).flatMap({ Double(text[$0]) }),
           let unit = Range(m.range(at: 2), in: text).map({ text[$0].lowercased() }) {
            let seconds: Double = unit == "h" ? 3600 : unit == "d" ? 86_400 : 604_800
            return Date(timeIntervalSince1970: (now.timeIntervalSince1970 - n * seconds).rounded(.towardZero))
        }
        if let date = isoDate(text) { return date }
        throw BoundError(value: text)
    }

    struct BoundError: Error, CustomStringConvertible {
        let value: String
        var description: String {
            "invalid time bound: '\(value)' (expected ISO date/datetime like 2026-07-01, or a relative duration like 7d, 24h, 2w)"
        }
    }

    /// What Python's `datetime.fromisoformat` reads: `YYYY-MM-DD` or `YYYYMMDD`, then optionally any one separator
    /// character and `HH[:MM[:SS[.fff]]]` (colons optional), then `Z` or `±HH[:MM[:SS]]`. No zone means UTC.
    private static let isoPattern = try! NSRegularExpression(pattern:
        "^(\\d{4})(-?)(\\d{2})\\2(\\d{2})(?:.(\\d{2})(?::?(\\d{2})(?::?(\\d{2})(?:[.,](\\d+))?)?)?"
        + "(?:(Z)|([+-])(\\d{2})(?::?(\\d{2})(?::?(\\d{2}))?)?)?)?$")

    static func isoDate(_ text: String) -> Date? {
        guard let m = isoPattern.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) else { return nil }
        func group(_ i: Int) -> String? { Range(m.range(at: i), in: text).map { String(text[$0]) } }
        func number(_ i: Int) -> Int { group(i).flatMap { Int($0) } ?? 0 }
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = TimeZone(identifier: "UTC")!
        let parts = DateComponents(year: number(1), month: number(3), day: number(4))
        guard parts.isValidDate(in: utc), let day = utc.date(from: parts) else { return nil }
        let (hour, minute, second) = (number(5), number(6), number(7))
        let fractionDigits = group(8) ?? ""
        let midnightNext = hour == 24 && minute == 0 && second == 0 && fractionDigits.allSatisfy { $0 == "0" }
        guard hour < 24 || midnightNext, minute < 60, second < 60 else { return nil }
        let fraction = fractionDigits.isEmpty ? 0 : Double("0." + fractionDigits.prefix(6)) ?? 0
        var offset = 0
        if let sign = group(10) {
            let (oh, om, os) = (number(11), number(12), number(13))
            guard om < 60, os < 60, oh * 3600 + om * 60 + os < 86_400 else { return nil }
            offset = (sign == "-" ? -1 : 1) * (oh * 3600 + om * 60 + os)
        }
        return day.addingTimeInterval(Double(hour * 3600 + minute * 60 + second - offset) + fraction)
    }

    private static let ansiEscape = try! NSRegularExpression(pattern:
        "\\x{1B}(?:\\[[\\x{30}-\\x{3F}]*[\\x{20}-\\x{2F}]*[\\x{40}-\\x{7E}]|\\][\\s\\S]*?(?:\\x{07}|\\x{1B}\\\\)|[PX^_][\\s\\S]*?(?:\\x{1B}\\\\)"
        + "|[\\x{20}-\\x{2F}]+[\\x{30}-\\x{7E}]|[\\x{30}-\\x{7E}])|\\x{9B}[\\x{30}-\\x{3F}]*[\\x{20}-\\x{2F}]*[\\x{40}-\\x{7E}]"
        + "|\\x{9D}[\\s\\S]*?(?:\\x{07}|\\x{9C})|[\\x{80}-\\x{9F}]")

    /// Archived terminal output carries ANSI escapes the model would otherwise copy.
    static func stripANSI(_ text: String) -> String {
        guard text.unicodeScalars.contains(where: { $0.value == 0x1B || (0x80...0x9F).contains($0.value) }) else { return text }
        return ansiEscape.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "")
    }

    static func shape(_ m: ConversationMessage, anchor: Int? = nil, maxContent: Int? = nil) -> ShapedMessage {
        var content = stripANSI(m.content)
        var truncated: Bool?
        var original: Int?
        if let maxContent, content.count > maxContent {
            original = content.count
            content = String(content.prefix(maxContent)) + "…"
            truncated = true
        }
        return ShapedMessage(seq: m.seq, role: m.role, content: content, timestamp: m.timestamp.timeIntervalSince1970,
                             toolName: m.toolName?.isEmpty == false ? m.toolName : nil,
                             toolCallID: m.toolCallID?.isEmpty == false ? m.toolCallID : nil,
                             anchor: anchor != nil && m.seq == anchor ? true : nil, contentTruncated: truncated,
                             originalContentChars: original)
    }

    struct Context {
        let store: ConversationStore
        let current: String?
        let now: Date
        let timeZone: TimeZone

        func when(_ date: Date) -> String {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = timeZone
            f.dateFormat = "MMMM dd, yyyy 'at' hh:mm a"
            return f.string(from: date)
        }

        func meta(_ s: ConversationSession) -> SessionMeta {
            SessionMeta(when: when(s.createdAt), title: s.title.isEmpty ? nil : s.title)
        }

        func dispatch(_ r: SessionSearchRequest) throws -> SessionSearchPayload {
            if let sid = r.sessionID.map(FTSQuery.trimmed), !sid.isEmpty {
                if let around = r.aroundSeq {
                    guard let seq = Int(FTSQuery.trimmed(around)) else { return .error("scroll requires integer around_seq") }
                    return try scroll(sid, seq, window: r.window)
                }
                return try read(sid)
            }
            let query = FTSQuery.trimmed(r.query ?? "")
            if query.isEmpty {
                return try browse(limit: clamp(r.limit, default: 10, 1, 10))
            }
            let after: Date?
            let before: Date?
            do {
                after = try parseBound(r.after, now: now)
                before = try parseBound(r.before, now: now)
            } catch {
                return .error(String(describing: error))
            }
            let sort = SearchSort(rawValue: FTSQuery.trimmed(r.sort ?? "").lowercased())
            let roles = (r.roleFilter ?? "").split(separator: ",").compactMap { MessageRole(rawValue: FTSQuery.trimmed(String($0))) }
            let detail = FTSQuery.trimmed(r.detail ?? "").lowercased() == "full" ? "full" : "adaptive"
            return try discover(query, roles: roles.isEmpty ? [.user, .assistant] : roles, limit: clamp(r.limit, default: 3, 1, 10),
                                sort: sort, detail: detail, after: after, before: before,
                                excluded: normalizeExcluded(r.excludeSessionIDs ?? []))
        }

        func normalizeExcluded(_ raw: [String]) -> Set<String> {
            var out: [String] = []
            for id in raw.map(FTSQuery.trimmed) where !id.isEmpty && !out.contains(id) {
                out.append(id)
                if out.count >= excludeSessionIDsCap { break }
            }
            return Set(out)
        }

        func browse(limit: Int) throws -> SessionSearchPayload {
            let rows = try store.sessions(archive: .all, limit: limit + 15).filter { $0.session.id != current }.prefix(limit)
            let results = rows.map {
                BrowseEntry(sessionID: $0.session.id, title: $0.session.title.isEmpty ? nil : $0.session.title,
                            startedAt: $0.session.createdAt.timeIntervalSince1970, lastActive: $0.lastActive.timeIntervalSince1970,
                            messageCount: $0.messageCount, preview: $0.preview)
            }
            return .browse(BrowsePayload(results: results, count: results.count, message:
                "Showing \(results.count) most recent sessions. Pass a query= to search, or session_id+around_seq to scroll."))
        }

        func read(_ sid: String, head: Int = 20, tail: Int = 10) throws -> SessionSearchPayload {
            guard let session = try store.session(id: sid) else { return .error("session_id not found: \(sid)") }
            let shaped = try store.messages(sessionID: sid).map { shape($0, maxContent: readMaxContent) }
            let truncated = shaped.count > head + tail
            return .read(ReadPayload(sessionID: sid, sessionMeta: meta(session), messageCount: shaped.count, truncated: truncated,
                                     messages: truncated ? Array(shaped.prefix(head) + shaped.suffix(tail)) : shaped,
                                     message: truncated ? "Session has \(shaped.count) messages; showing first \(head) + last \(tail). "
                                        + "Pass around_seq (any seq above) to scroll the middle." : nil))
        }

        /// The anchor is in the caller's live context when it is a stored row of the current session that compaction has not archived.
        func anchorInLiveContext(_ sid: String, _ seq: Int) throws -> Bool {
            guard sid == current, let state = try store.rowState(sessionID: sid, seq: seq) else { return false }
            return !(!state.active && state.compacted)
        }

        func scroll(_ sid: String, _ seq: Int, window: Int?) throws -> SessionSearchPayload {
            let window = clamp(window, default: 5, 1, 20)
            if current != nil, try anchorInLiveContext(sid, seq) {
                return .error("scroll rejected: anchor lives in the current session (already in your active context)")
            }
            guard let session = try store.session(id: sid) else { return .error("session_id not found: \(sid)") }
            let view = try store.window(sessionID: sid, aroundSeq: seq, radius: window)
            guard !view.messages.isEmpty else { return .error("around_seq \(seq) not in session_id \(sid)") }
            return .scroll(ScrollPayload(sessionID: sid, aroundSeq: seq, sessionMeta: meta(session), window: window,
                                         messages: view.messages.map { shape($0, anchor: seq, maxContent: scrollMaxContent) },
                                         messagesBefore: view.messagesBefore, messagesAfter: view.messagesAfter, hint: scrollHint))
        }

        func bookends(_ messages: [ConversationMessage]) -> [ShapedMessage] {
            messages.filter { !$0.isSummary }.map { shape($0, maxContent: 1_200) }
        }

        func titleMatch(_ query: String, after: Date?, before: Date?, excluded: Set<String>) throws -> DiscoveryEntry? {
            let title = query.trimmingCharacters(in: CharacterSet(charactersIn: "`'\""))
            guard !title.isEmpty else { return nil }
            let candidates = try store.sessionsTitled(title).filter { candidate in
                candidate.id != current && !excluded.contains(candidate.id)
                    && after.map { candidate.createdAt >= $0 } ?? true && before.map { candidate.createdAt < $0 } ?? true
            }
            // Titles are not unique here; a title shared by several sessions identifies none of them.
            guard candidates.count == 1, let session = candidates.first else { return nil }
            let messages = try store.messages(sessionID: session.id)
            let anchor = messages.first?.seq
            let view = try anchor.map { try store.anchoredView(sessionID: session.id, aroundSeq: $0) }
            let shownTitle = session.title.isEmpty ? title : session.title
            return DiscoveryEntry(sessionID: session.id, when: when(session.createdAt), title: shownTitle, matchedRole: "session_title",
                                  matchSeq: anchor, snippet: "Session title matched: \(shownTitle)",
                                  bookendStart: bookends(view?.bookendStart ?? Array(messages.prefix(3))),
                                  messages: (view?.window ?? Array(messages.prefix(5))).map { shape($0, anchor: anchor, maxContent: 4_000) },
                                  bookendEnd: bookends(view?.bookendEnd ?? Array(messages.suffix(3))),
                                  messagesBefore: view?.messagesBefore ?? 0,
                                  messagesAfter: view?.messagesAfter ?? max(messages.count - 5, 0), detail: "full",
                                  parentSessionID: session.parentID)
        }

        func hydrate(_ hit: SearchHit, full: Bool) throws -> DiscoveryEntry? {
            guard let session = try store.session(id: hit.sessionID) else { return nil }
            let view = try store.anchoredView(sessionID: hit.sessionID, aroundSeq: hit.seq)
            return DiscoveryEntry(sessionID: hit.sessionID, when: when(session.createdAt), title: session.title.isEmpty ? nil : session.title,
                                  matchedRole: hit.role.rawValue, matchSeq: hit.seq, snippet: hit.snippet,
                                  bookendStart: full ? bookends(view.bookendStart) : [],
                                  messages: view.window.filter { full || $0.seq == hit.seq }.map { shape($0, anchor: hit.seq, maxContent: 4_000) },
                                  bookendEnd: full ? bookends(view.bookendEnd) : [],
                                  messagesBefore: view.messagesBefore, messagesAfter: view.messagesAfter,
                                  detail: full ? "full" : "compact", parentSessionID: session.parentID)
        }

        func discover(_ query: String, roles: [MessageRole], limit: Int, sort: SearchSort?, detail: String, after: Date?,
                      before: Date?, excluded: Set<String>) throws -> SessionSearchPayload {
            let titleResult = try titleMatch(query, after: after, before: before, excluded: excluded)
            let hits = try store.search(query, options: SearchOptions(roles: roles, limit: discoverScanLimit, sort: sort,
                                                                      startedAfter: after, startedBefore: before, includeContext: false))
            if hits.isEmpty && titleResult == nil {
                return .discover(DiscoveryPayload(query: query, detail: detail, results: [], count: 0, message: noMatchMessage))
            }
            var results = titleResult.map { [$0] } ?? []
            var seen: Set<String> = titleResult.map { [$0.sessionID] } ?? []
            var picked: [SearchHit] = []
            for hit in hits {
                if seen.count >= limit { break }
                if excluded.contains(hit.sessionID) || seen.contains(hit.sessionID) { continue }
                let compactedHit = hit.compacted && !hit.active
                if hit.sessionID == current && !compactedHit { continue }
                if try store.isAppContextRow(messageID: hit.messageID) { continue }
                seen.insert(hit.sessionID)
                picked.append(hit)
            }
            for hit in picked {
                if let entry = try hydrate(hit, full: detail == "full" || results.isEmpty) { results.append(entry) }
            }
            return .discover(DiscoveryPayload(query: query, detail: detail, results: results, count: results.count,
                                              sessionsSearched: seen.count, hint: discoverHint))
        }
    }
}

/// A search hit's surroundings: a window filtered to user and assistant turns (the anchor always kept),
/// plus the session's first and last turns outside it, so one read shows both the goal and the resolution.
struct AnchoredView {
    var window: [ConversationMessage]
    var messagesBefore: Int
    var messagesAfter: Int
    var bookendStart: [ConversationMessage]
    var bookendEnd: [ConversationMessage]
}

extension ConversationStore {
    func anchoredView(sessionID: String, aroundSeq seq: Int, radius: Int = 5, bookend: Int = 3,
                      keepRoles: [MessageRole] = [.user, .assistant]) throws -> AnchoredView {
        try sync {
            let primitive = try windowRows(sessionID, seq, radius)
            guard let first = primitive.messages.first, let last = primitive.messages.last else {
                return AnchoredView(window: [], messagesBefore: 0, messagesAfter: 0, bookendStart: [], bookendEnd: [])
            }
            let roles = keepRoles.map(\.rawValue)
            let marks = roles.map { _ in "?" }.joined(separator: ",")
            func bookendRows(_ op: String, _ boundary: Int, _ order: String) throws -> [ConversationMessage] {
                try conn.query("""
                    SELECT * FROM messages WHERE session_id = ? AND seq \(op) ? AND role IN (\(marks)) AND length(content) > 0
                      AND (active = 1 OR compacted = 1)
                    ORDER BY seq \(order) LIMIT ?
                    """, [sessionID, boundary] + roles + [bookend]).map(Self.message(from:))
            }
            return AnchoredView(window: primitive.messages.filter { $0.seq == seq || keepRoles.contains($0.role) },
                                messagesBefore: primitive.messagesBefore, messagesAfter: primitive.messagesAfter,
                                bookendStart: try bookendRows("<", first.seq, "ASC"),
                                bookendEnd: Array(try bookendRows(">", last.seq, "DESC").reversed()))
        }
    }

    func rowState(sessionID: String, seq: Int) throws -> (active: Bool, compacted: Bool)? {
        try sync {
            try conn.one("SELECT active, compacted FROM messages WHERE session_id = ? AND seq = ?", [sessionID, seq])
                .map { ($0.bool("active"), $0.bool("compacted")) }
        }
    }

    /// Sessions whose title equals `title` ignoring case, newest first.
    func sessionsTitled(_ title: String) throws -> [ConversationSession] {
        try sync {
            try conn.query("SELECT * FROM sessions WHERE title <> '' AND length(title) = ? ORDER BY started_at DESC",
                           [title.unicodeScalars.count]).map(Self.session(from:))
                .filter { $0.title.compare(title, options: [.caseInsensitive]) == .orderedSame }
        }
    }
}

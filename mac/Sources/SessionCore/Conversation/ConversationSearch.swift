// Ported from Hermes Agent (MIT): hermes_state_search.py (search_messages, query sanitizing and routing).
import Foundation

public enum SearchSort: String, Sendable {
    case newest, oldest
}

public struct SearchOptions: Sendable {
    /// Roles to match; nil matches every role. Including `.tool` searches full tool output with a slower substring scan.
    public var roles: [MessageRole]?
    public var limit: Int
    public var offset: Int
    /// nil ranks by BM25 relevance; `.newest`/`.oldest` order by time, then relevance.
    public var sort: SearchSort?
    /// Live and compaction-archived messages are always searchable; this also returns the superseded copies compaction leaves.
    public var includeSuperseded: Bool
    /// Inclusive lower bound on the session's start.
    public var startedAfter: Date?
    /// Exclusive upper bound on the session's start.
    public var startedBefore: Date?
    /// Attach the neighboring messages to each hit.
    public var includeContext: Bool

    public init(roles: [MessageRole]? = nil, limit: Int = 20, offset: Int = 0, sort: SearchSort? = nil,
                includeSuperseded: Bool = false, startedAfter: Date? = nil, startedBefore: Date? = nil,
                includeContext: Bool = true) {
        self.roles = roles
        self.limit = limit
        self.offset = offset
        self.sort = sort
        self.includeSuperseded = includeSuperseded
        self.startedAfter = startedAfter
        self.startedBefore = startedBefore
        self.includeContext = includeContext
    }
}

public struct SearchContextLine: Equatable, Sendable {
    public var role: MessageRole
    public var content: String
}

public struct SearchHit: Equatable, Sendable {
    public var messageID: Int64
    public var sessionID: String
    public var seq: Int
    public var role: MessageRole
    /// Matched text with `>>>` and `<<<` around the hits.
    public var snippet: String
    public var timestamp: Date
    public var toolName: String?
    public var active: Bool
    public var compacted: Bool
    public var sessionTitle: String
    public var sessionStartedAt: Date
    /// The hit and its neighbors, each cut to 200 characters. Empty unless requested.
    public var context: [SearchContextLine]
}

enum FTSQuery {
    /// Hard safety bound on raw query text, far above any real query; the term cap does the real limiting.
    static let maxQueryChars = 65_536
    private static let operators: Set<String> = ["AND", "OR", "NOT"]
    private static let cjkRanges: [ClosedRange<UInt32>] = [
        0x4E00...0x9FFF, 0x3400...0x4DBF, 0x20000...0x2A6DF, 0x3000...0x303F, 0x3040...0x309F, 0x30A0...0x30FF, 0xAC00...0xD7AF,
    ]

    private static func regex(_ pattern: String) -> NSRegularExpression {
        try! NSRegularExpression(pattern: pattern)
    }

    private static let quotedPhrase = regex("\"[^\"]*\"")
    private static let compoundTerm = regex("\\b(\\w+(?:[._-]\\w+)+)\\b")
    private static let likeToken = regex("\"[^\"]+\"|\\S+")

    private static func replace(_ re: NSRegularExpression, in text: String, with template: String) -> String {
        re.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: template)
    }

    static func trimmed(_ text: String) -> String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    static func tokens(_ text: String) -> [String] { text.split(whereSeparator: \.isWhitespace).map(String.init) }

    static func likeTokens(_ text: String) -> [String] {
        likeToken.matches(in: text, range: NSRange(text.startIndex..., in: text)).compactMap {
            Range($0.range, in: text).map { String(text[$0]) }
        }
    }

    /// Units (terms and operators) a query keeps. FTS5 flattens AND/OR chains but nests NOT up to depth 256, and the
    /// substring fallback's SQL nests about one level per term up to 1,000; 240 stays under both.
    static let maxUnits = 240

    /// Makes user text safe for FTS5 MATCH: keeps balanced quoted phrases, strips characters the query grammar
    /// rejects, drops dangling or doubled operators and quotes hyphenated or dotted terms so they match as phrases.
    /// Outside a quoted phrase FTS5 accepts only letters, digits, `_`, non-ASCII text and a trailing `*`, so every
    /// other ASCII character becomes a space (so `-rf` is the word `rf`: FTS5, like Hermes, has no exclusion syntax) and
    /// `x*y` splits into the prefix `x*` and the word `y`. A query of nothing but operator words searches for those words.
    static func sanitize(_ query: String) -> String {
        var sanitized = bounded(query).replacingOccurrences(of: "\u{0}", with: " ")
        var held: [String] = []
        var rebuilt = ""
        var cursor = sanitized.startIndex
        for match in quotedPhrase.matches(in: sanitized, range: NSRange(sanitized.startIndex..., in: sanitized)) {
            guard let r = Range(match.range, in: sanitized) else { continue }
            rebuilt += sanitized[cursor..<r.lowerBound]
            let phrase = String(sanitized[r])
            if hasWordCharacter(phrase) || phrase.unicodeScalars.contains(where: { !$0.isASCII && !$0.properties.isWhitespace }) {
                rebuilt += " \u{0}Q\(held.count)\u{0} "
                held.append(phrase)
            } else {
                rebuilt += " "
            }
            cursor = r.upperBound
        }
        sanitized = rebuilt + sanitized[cursor...]
        sanitized = sanitized.replacingOccurrences(of: "\"", with: " ")
        let cjk = containsCJK(sanitized)
        if sanitized.contains("%") && !cjk {
            sanitized = sanitized.replacingOccurrences(of: "%", with: " ")
        }
        sanitized = replace(compoundTerm, in: sanitized, with: "\"$1\"")
        var neutral = ""
        var inQuote = false
        for ch in sanitized {
            if ch == "\"" {
                neutral += inQuote ? "\" " : " \""
                inQuote.toggle()
            } else if inQuote || !ch.isASCII || ch.isLetter || ch.isNumber || ch.isWhitespace || "_*\u{0}".contains(ch) || (cjk && ch == "%") {
                neutral.append(ch)
            } else {
                neutral.append(" ")
            }
        }
        var units: [String] = []
        for token in tokens(neutral) {
            if token.hasPrefix("\"") || token.hasPrefix("\u{0}") {
                units.append(token)
            } else {
                let pieces = token.split(separator: "*", omittingEmptySubsequences: false)
                for (i, piece) in pieces.enumerated() where !piece.isEmpty {
                    units.append(String(piece) + (i < pieces.count - 1 ? "*" : ""))
                }
            }
        }
        if !units.isEmpty && units.allSatisfy(operators.contains) {
            units = units.map { "\"\($0)\"" }
        }
        units = cleanOperators(capped(cleanOperators(units)))
        var result = units.joined(separator: " ")
        for (i, phrase) in held.enumerated() {
            result = result.replacingOccurrences(of: "\u{0}Q\(i)\u{0}", with: phrase)
        }
        return trimmed(result)
    }

    /// The query cut to `maxQueryChars`. A cut inside a quoted phrase keeps that phrase's words unquoted rather than leave a stray quote.
    static func bounded(_ query: String) -> String {
        guard query.unicodeScalars.count > maxQueryChars else { return query }
        var cut = String(String.UnicodeScalarView(query.unicodeScalars.prefix(maxQueryChars)))
        if cut.filter({ $0 == "\"" }).count % 2 == 1, let open = cut.lastIndex(of: "\"") {
            cut.replaceSubrange(open...open, with: " ")
        }
        return cut
    }

    /// Cuts a query to `maxUnits`, dropping AND-ed terms first, then OR alternatives, then NOT clauses, each from the
    /// end and never the first term, so a NOT keeps its operand whenever anything can be dropped instead.
    static func capped(_ units: [String]) -> [String] {
        guard units.count > maxUnits else { return units }
        var entries: [(op: String?, term: String)] = []
        var pending: String?
        for unit in units {
            if operators.contains(unit) {
                pending = unit
            } else {
                entries.append((pending, unit))
                pending = nil
            }
        }
        var size = entries.reduce(0) { $0 + ($1.op == nil ? 1 : 2) }
        for rank in 0...2 {
            var i = entries.count - 1
            while size > maxUnits && i > 0 {
                let op = entries[i].op
                if (op == "OR" ? 1 : op == "NOT" ? 2 : 0) == rank {
                    size -= op == nil ? 1 : 2
                    entries.remove(at: i)
                }
                i -= 1
            }
        }
        return entries.flatMap { [$0.op, $0.term].compactMap { $0 } }
    }

    static func hasWordCharacter(_ text: String) -> Bool {
        text.unicodeScalars.contains(where: CharacterSet.alphanumerics.contains)
    }

    /// FTS5 operators are uppercase only. A leading, trailing or doubled operator is a syntax error; of a doubled pair the later one is kept.
    static func cleanOperators(_ units: [String]) -> [String] {
        var out: [String] = []
        for unit in units {
            if operators.contains(unit) {
                if let last = out.last, operators.contains(last) { out.removeLast() }
                if out.isEmpty { continue }
            }
            out.append(unit)
        }
        while let last = out.last, operators.contains(last) { out.removeLast() }
        return out
    }

    /// A unit without its quotes and stars: the literal text it matches.
    static func bareTerm(_ unit: String) -> String {
        trimmed(unit.replacingOccurrences(of: "\"", with: "").replacingOccurrences(of: "*", with: ""))
    }

    static func searchUnits(_ query: String) -> [String] {
        likeTokens(query).filter { !operators.contains($0) }
    }

    /// A unit as an FTS5 string: phrases stay as they are, anything else is quoted with a trailing `*` kept outside the quotes.
    static func quoteUnit(_ unit: String) -> String? {
        if unit.count >= 2 && unit.hasPrefix("\"") && unit.hasSuffix("\"") { return unit }
        let bare = bareTerm(unit)
        return bare.isEmpty ? nil : "\"" + bare + "\"" + (unit.hasSuffix("*") ? "*" : "")
    }

    /// Every term quoted: the FTS5 form that always parses, for when a query still fails to.
    static func quotedAll(_ query: String) -> String {
        searchUnits(query).compactMap(quoteUnit).joined(separator: " ")
    }

    /// A term made only of symbols (emoji, dingbats) that the word tokenizer drops and the trigram index cannot match.
    static func hasSymbolOnlyTerm(_ query: String) -> Bool {
        searchUnits(query).map(bareTerm).contains { !$0.isEmpty && !hasWordCharacter($0) }
    }

    static func isCJK(_ scalar: Unicode.Scalar) -> Bool { cjkRanges.contains { $0.contains(scalar.value) } }
    static func containsCJK(_ text: String) -> Bool { text.unicodeScalars.contains(where: isCJK) }
    static func countCJK(_ text: String) -> Int { text.unicodeScalars.filter(isCJK).count }


    /// Quotes every non-operator term for the trigram index, which matches substrings, so prefix stars are dropped.
    static func quoteTokens(_ text: String) -> String {
        likeTokens(text).compactMap { token -> String? in
            if operators.contains(token) { return token }
            let bare = bareTerm(token)
            return bare.isEmpty ? nil : "\"" + bare + "\""
        }.joined(separator: " ")
    }

    /// The implicit-AND query as an any-term OR query; nil for a single term or explicit OR/NOT.
    static func orRelaxed(_ query: String) -> String? {
        var units: [String] = []
        for token in likeTokens(query) {
            if token == "OR" || token == "NOT" { return nil }
            if token != "AND" { units.append(token) }
        }
        return units.count >= 2 ? units.joined(separator: " OR ") : nil
    }

    /// Every non-operator token has at least 3 characters, the minimum a trigram can match.
    static func trigramEligible(_ query: String) -> Bool {
        let terms = searchUnits(query).map(bareTerm)
        return !terms.isEmpty && terms.allSatisfy { $0.unicodeScalars.count >= 3 }
    }

    static func hasShortCJKTerm(_ terms: [String]) -> Bool {
        terms.contains { containsCJK($0) && countCJK($0) < 3 }
    }

    static func escapeLike(_ text: String) -> String {
        text.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "%", with: "\\%")
            .replacingOccurrences(of: "_", with: "\\_")
    }

    static let likeAnyColumn = "(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\')"
    static let likeCoalescedColumn = "(COALESCE(m.content, '') LIKE ? ESCAPE '\\' OR COALESCE(m.tool_name, '') LIKE ? ESCAPE '\\')"

    /// SQLite refuses a LIKE pattern over 50,000 bytes; a longer term is cut to its first 12,000 characters, or fewer
    /// until its escaped form fits.
    static let maxLikePatternBytes = 49_000
    static let maxLikeTermChars = 12_000

    static func likeParams(_ term: String) -> [SQLBindable] {
        var cut = String(term.prefix(maxLikeTermChars))
        while "%\(escapeLike(cut))%".utf8.count > maxLikePatternBytes { cut = String(cut.prefix(cut.count / 2)) }
        let pattern = "%\(escapeLike(cut))%"
        return [pattern, pattern]
    }

    /// The supported FTS boolean subset as LIKE predicates: terms in an OR group are ANDed and NOT negates the next term.
    /// Operators are uppercase only, as in FTS5, so "can not connect" is three words.
    static func compileLikeBoolean(_ query: String) -> (predicate: String, params: [SQLBindable], snippetTerm: String?) {
        var groups: [[(term: String, negated: Bool)]] = [[]]
        var negateNext = false
        for token in likeTokens(query) {
            switch token {
            case "OR":
                if !(groups.last?.isEmpty ?? true) { groups.append([]) }
                negateNext = false
            case "AND", "NEAR":
                continue
            case "NOT":
                negateNext = true
            default:
                let term = trimmed(token.trimmingCharacters(in: CharacterSet(charactersIn: "\"")).trimmingCharacters(in: CharacterSet(charactersIn: "*")))
                if !term.isEmpty {
                    groups[groups.count - 1].append((term, negateNext))
                    negateNext = false
                }
            }
        }
        var compiled: [String] = []
        var params: [SQLBindable] = []
        var snippetTerm: String?
        for group in groups where group.contains(where: { !$0.negated }) {
            var clauses: [String] = []
            for (term, negated) in group {
                clauses.append(negated ? "NOT \(likeCoalescedColumn)" : likeCoalescedColumn)
                params += likeParams(term)
                if snippetTerm == nil && !negated { snippetTerm = term }
            }
            compiled.append("(\(clauses.joined(separator: " AND ")))")
        }
        return (compiled.joined(separator: " OR "), params, snippetTerm)
    }
}

extension ConversationStore {
    /// Full-text search over every session: keywords, `"phrases"`, AND/OR/NOT and `prefix*`. User text never
    /// raises an FTS syntax error. An exact query that misses is retried as a substring match and then as any-term OR.
    public func search(_ query: String, options: SearchOptions = SearchOptions()) throws -> [SearchHit] {
        try sync { try searchRows(query, options) }
    }

    private func filterClauses(_ options: SearchOptions) -> ([String], [SQLBindable]) {
        var where_: [String] = []
        var params: [SQLBindable] = []
        if !options.includeSuperseded { where_.append("(m.active = 1 OR m.compacted = 1)") }
        if let roles = options.roles, !roles.isEmpty {
            where_.append("m.role IN (\(roles.map { _ in "?" }.joined(separator: ",")))")
            params += roles.map(\.rawValue)
        }
        if let after = options.startedAfter {
            where_.append("s.started_at >= ?")
            params.append(after.timeIntervalSince1970)
        }
        if let before = options.startedBefore {
            where_.append("s.started_at < ?")
            params.append(before.timeIntervalSince1970)
        }
        return (where_, params)
    }

    private func selectSQL(snippet: String, from: String, where_: [String], orderBy: String) -> String {
        """
        SELECT m.id, m.session_id, m.seq, m.role, \(snippet) AS snippet, m.timestamp, m.tool_name, m.active, m.compacted,
               s.title AS session_title, s.started_at AS session_started
        FROM \(from)
        JOIN sessions s ON s.id = m.session_id
        WHERE \(where_.joined(separator: " AND "))
        \(orderBy)
        LIMIT ? OFFSET ?
        """
    }

    private static func hit(from row: Row) -> SearchHit {
        SearchHit(messageID: row.int64("id") ?? 0, sessionID: row.string("session_id") ?? "", seq: row.int("seq") ?? 0,
                  role: MessageRole(rawValue: row.string("role") ?? "") ?? .user, snippet: row.string("snippet") ?? "",
                  timestamp: Date(timeIntervalSince1970: row.double("timestamp") ?? 0), toolName: row.string("tool_name"),
                  active: row.bool("active"), compacted: row.bool("compacted"), sessionTitle: row.string("session_title") ?? "",
                  sessionStartedAt: Date(timeIntervalSince1970: row.double("session_started") ?? 0), context: [])
    }

    private enum MatchOutcome {
        case rows([SearchHit])
        case unusable
    }

    /// One MATCH against `table`. A query the index cannot run comes back unusable so the caller falls back;
    /// a corrupt index is detached (search answers from LIKE) and also comes back unusable.
    private func match(_ table: String, _ matchQuery: String, _ options: SearchOptions) throws -> MatchOutcome {
        var (where_, params) = filterClauses(options)
        where_.insert("\(table) MATCH ?", at: 0)
        params.insert(matchQuery, at: 0)
        let orderBy: String
        switch options.sort {
        case .newest: orderBy = "ORDER BY m.timestamp DESC, rank"
        case .oldest: orderBy = "ORDER BY m.timestamp ASC, rank"
        case nil: orderBy = "ORDER BY rank"
        }
        let sql = selectSQL(snippet: "snippet(\(table), -1, '>>>', '<<<', '...', 40)",
                            from: "\(table) JOIN messages m ON m.id = \(table).rowid", where_: where_, orderBy: orderBy)
        do {
            return .rows(try conn.query(sql, params + [options.limit, options.offset]).map(Self.hit(from:)))
        } catch let error as SQLiteError {
            guard error.isCorruption else { return .unusable }
            guard ConversationSchema.isFTSCorruption(error) else { throw error }
            try ConversationSchema.enterFailOpen(conn)
            fts = FTSState(enabled: false, trigramAvailable: false, stale: true)
            return .unusable
        }
    }

    /// LIKE is case-insensitive for ASCII, so the snippet anchor is found the same way.
    private func likeRows(_ where_: [String], _ params: [SQLBindable], snippetTerm: String, orderBy: String,
                          _ options: SearchOptions) throws -> [SearchHit] {
        let sql = selectSQL(snippet: "substr(m.content, max(1, instr(lower(m.content), lower(?)) - 40), 120)", from: "messages m",
                            where_: where_, orderBy: orderBy)
        return try conn.query(sql, [snippetTerm] + params + [options.limit, options.offset]).map(Self.hit(from:))
    }

    /// Canonical-row search used for tool output (the word index keeps only a prefix of it), while the FTS index is
    /// detached, and as the last resort when no FTS form of the query parses.
    private func likeFallback(_ query: String, _ options: SearchOptions) throws -> [SearchHit] {
        let compiled = FTSQuery.compileLikeBoolean(query)
        guard !compiled.predicate.isEmpty, let term = compiled.snippetTerm else { return [] }
        let (filters, filterParams) = filterClauses(options)
        let order = options.sort == .oldest ? "ASC" : "DESC"
        return try likeRows(["(\(compiled.predicate))"] + filters, compiled.params + filterParams, snippetTerm: term,
                            orderBy: "ORDER BY m.timestamp \(order), m.id \(order)", options)
    }

    /// CJK text: the word tokenizer splits it into single characters, so terms of 3+ CJK characters use the
    /// trigram index and anything shorter falls back to a substring scan with one clause per term.
    private func searchCJK(_ query: String, _ options: SearchOptions) throws -> [SearchHit] {
        let terms = FTSQuery.searchUnits(query).map(FTSQuery.bareTerm).filter { !$0.isEmpty }
        guard !terms.isEmpty else { return [] }
        let trigramRoute = FTSQuery.countCJK(terms.joined()) >= 3 && !FTSQuery.hasShortCJKTerm(terms) && fts.trigramAvailable
        if trigramRoute, case .rows(let hits) = try match("messages_fts_trigram", FTSQuery.quoteTokens(query), options) {
            return hits
        }
        let (filters, filterParams) = filterClauses(options)
        let anyTerm = "(" + Array(repeating: FTSQuery.likeAnyColumn, count: terms.count).joined(separator: " OR ") + ")"
        return try likeRows([anyTerm] + filters, terms.flatMap(FTSQuery.likeParams) + filterParams, snippetTerm: terms[0],
                            orderBy: "ORDER BY m.timestamp DESC", options)
    }

    /// Another process sharing the file may have detached a damaged index; its breadcrumb is checked before each search.
    private func refreshStaleState() throws {
        guard fts.enabled, !fts.stale,
              try conn.one("SELECT 1 AS hit FROM state_meta WHERE key = ?", [ConversationSchema.staleKey]) != nil else { return }
        fts = FTSState(enabled: false, trigramAvailable: false, stale: true)
    }

    func searchRows(_ rawQuery: String, _ options: SearchOptions) throws -> [SearchHit] {
        guard !FTSQuery.trimmed(rawQuery).isEmpty else { return [] }
        let query = FTSQuery.sanitize(rawQuery)
        guard !query.isEmpty else { return [] }
        try refreshStaleState()
        if options.roles?.contains(.tool) ?? false || fts.stale {
            return try attachContext(likeFallback(query, options), options)
        }
        guard fts.enabled else { return [] }
        let isCJK = FTSQuery.containsCJK(query)
        var hits: [SearchHit]
        if isCJK {
            hits = try searchCJK(query, options)
        } else {
            hits = try wordIndexRows(query, options)
        }
        // The word tokenizer puts no boundary between Latin and adjacent CJK, so a Latin miss retries the substring index.
        if hits.isEmpty && !isCJK && fts.trigramAvailable && FTSQuery.trigramEligible(query),
           case .rows(let rows) = try match("messages_fts_trigram", FTSQuery.quoteTokens(query), options) {
            hits = rows
        }
        // A paraphrased multi-word query misses a message lacking even one word; once everything missed, match any term.
        if hits.isEmpty && !isCJK && !fts.stale, let relaxed = FTSQuery.orRelaxed(query),
           case .rows(let rows) = try match("messages_fts", relaxed, options) {
            hits = rows
        }
        // Emoji and other symbols are dropped by the word tokenizer and too short for the trigram index.
        if hits.isEmpty && FTSQuery.hasSymbolOnlyTerm(query) {
            hits = try likeFallback(query, options)
        }
        return try attachContext(hits, options)
    }

    /// The word-index MATCH. A form FTS5 cannot parse is retried with every term quoted (a form that always parses);
    /// a detached index answers from a substring scan, so a syntax error never reads as "nothing matched".
    func wordIndexRows(_ query: String, _ options: SearchOptions) throws -> [SearchHit] {
        for form in [query, FTSQuery.quotedAll(query)] where !form.isEmpty && !fts.stale {
            if case .rows(let rows) = try match("messages_fts", form, options) {
                return rows
            }
        }
        return try likeFallback(query, options)
    }

    private func attachContext(_ hits: [SearchHit], _ options: SearchOptions) throws -> [SearchHit] {
        guard options.includeContext else { return hits }
        return try hits.map { hit in
            var hit = hit
            hit.context = try conn.query("""
                SELECT role, content FROM messages WHERE session_id = ? AND seq IN (
                    ?, (SELECT MAX(seq) FROM messages WHERE session_id = ? AND seq < ? AND (active = 1 OR compacted = 1)),
                    (SELECT MIN(seq) FROM messages WHERE session_id = ? AND seq > ? AND (active = 1 OR compacted = 1)))
                ORDER BY seq
                """, [hit.sessionID, hit.seq, hit.sessionID, hit.seq, hit.sessionID, hit.seq]).map {
                SearchContextLine(role: MessageRole(rawValue: $0.string("role") ?? "") ?? .user,
                                  content: String(($0.string("content") ?? "").prefix(200)))
            }
            return hit
        }
    }
}

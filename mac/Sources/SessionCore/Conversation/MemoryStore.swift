// Ported from Hermes Agent (MIT) tools/memory_tool_store.py and utils.py (atomic_write_text).
import Foundation

public enum MemoryTarget: String, CaseIterable, Sendable {
    case memory, user

    public var fileName: String { self == .user ? "USER.md" : "MEMORY.md" }

    /// The title line of the block this store renders into the system prompt.
    public var blockHeader: String {
        self == .user ? "USER PROFILE (who the user is)" : "MEMORY (your personal notes)"
    }
}

/// One item of a memory `operations` batch. Fields stay optional so malformed calls get Hermes's error text.
public struct MemoryOperation: Equatable, Sendable {
    public var action: String?
    public var content: String?
    public var oldText: String?
    public var newText: String?
    /// The full entry a staged write was reviewed against; when set, only that exact entry qualifies.
    public var matchedEntry: String?
    /// A per-operation target. Only nil or the call's own target is accepted.
    public var target: String?

    public init(action: String?, content: String? = nil, oldText: String? = nil, newText: String? = nil,
                matchedEntry: String? = nil, target: String? = nil) {
        self.action = action
        self.content = content
        self.oldText = oldText
        self.newText = newText
        self.matchedEntry = matchedEntry
        self.target = target
    }

    /// Parses a model-supplied operation. A model-supplied `matched_entry` is ignored: only staging pins it.
    public init(json: MemoryJSON) {
        func text(_ key: String) -> String? {
            switch json[key] {
            case nil, .null?: return nil
            case let value?: return value.string ?? value.pythonDisplay
            }
        }
        self.init(action: text("action"), content: json["content"]?.string, oldText: json["old_text"]?.string,
                  newText: json["new_text"]?.string, target: text("target"))
    }

    /// A staged operation read back from a pending record, where `matched_entry` was pinned by the store.
    init(pending json: MemoryJSON) {
        self.init(json: json)
        matchedEntry = json["matched_entry"]?.string
    }

    public var json: MemoryJSON {
        let fields: [(String, String?)] = [("action", action), ("content", content), ("old_text", oldText),
                                           ("new_text", newText), ("target", target), ("matched_entry", matchedEntry)]
        return .object(fields.compactMap { key, value in value.map { (key, .string($0)) } })
    }

    /// Python's `op.get("content") or op.get("new_text") or ""`.
    var effectiveContent: String {
        if let content, !content.isEmpty { return content }
        return newText ?? ""
    }
}

/// Bounded, file-backed curated memory: MEMORY.md (agent notes) and USER.md (user profile).
///
/// Create one per session and call `loadFromDisk()` at session start. That captures the frozen prompt
/// snapshot; writes during the session go to disk but never change `formatForSystemPrompt`.
public final class MemoryStore {
    public static let entryDelimiter = "\n§\n"
    public static let defaultMemoryCharLimit = 2200
    public static let defaultUserCharLimit = 1375
    /// Failed consolidation attempts allowed per turn before a terminal "stop retrying" result.
    public static let maxConsolidationFailuresPerTurn = 3
    public static let patchDropHint =
        "To drop text inside an entry, patch a longer span that contains it; 'remove' deletes the whole entry."

    public static var defaultFolder: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".assistant/mac-memory", isDirectory: true)
    }

    public let folder: URL
    public let memoryCharLimit: Int
    public let userCharLimit: Int
    public let memoryEnabled: Bool
    public let userProfileEnabled: Bool
    public private(set) var memoryEntries: [String] = []
    public private(set) var userEntries: [String] = []
    /// Why the last call was refused (`over_budget`, `no_match`, `scan_blocked`, ...), or `none` after a success.
    /// Never shown to the model.
    public private(set) var lastFailureClass = "other"

    private var snapshot: [MemoryTarget: String] = [.memory: "", .user: ""]
    private var consolidationFailures = 0
    var now: () -> Date = Date.init

    public init(folder: URL = MemoryStore.defaultFolder, memoryCharLimit: Int = MemoryStore.defaultMemoryCharLimit,
                userCharLimit: Int = MemoryStore.defaultUserCharLimit, memoryEnabled: Bool = true,
                userProfileEnabled: Bool = true) {
        self.folder = folder
        self.memoryCharLimit = memoryCharLimit
        self.userCharLimit = userCharLimit
        self.memoryEnabled = memoryEnabled
        self.userProfileEnabled = userProfileEnabled
    }

    public func path(for target: MemoryTarget) -> URL { folder.appendingPathComponent(target.fileName) }

    public func entries(for target: MemoryTarget) -> [String] { target == .user ? userEntries : memoryEntries }

    public func charLimit(for target: MemoryTarget) -> Int { target == .user ? userCharLimit : memoryCharLimit }

    public func isEnabled(_ target: MemoryTarget) -> Bool { target == .user ? userProfileEnabled : memoryEnabled }

    /// Call at the start of each user turn.
    public func resetConsolidationFailures() { consolidationFailures = 0 }

    // MARK: Session snapshot

    /// Loads both files and freezes the prompt snapshot. Entries that trip the threat scan are replaced by a
    /// `[BLOCKED: ...]` line in the snapshot only; the live lists keep the raw text so the entry can be removed.
    public func loadFromDisk() {
        for target in MemoryTarget.allCases {
            let path = path(for: target)
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let entries = Self.deduplicated(Self.parseEntries(Self.readRaw(path).raw))
            setEntries(target, entries)
            snapshot[target] = renderBlock(target, entries.map { sanitize($0, fileName: path.lastPathComponent, target) })
        }
    }

    /// The block frozen at `loadFromDisk()`, or nil when that store was empty.
    public func formatForSystemPrompt(_ target: MemoryTarget) -> String? {
        let block = snapshot[target] ?? ""
        return block.isEmpty ? nil : block
    }

    /// The enabled, non-empty blocks in prompt order (memory, then user profile).
    public var systemPromptBlocks: [String] {
        MemoryTarget.allCases.filter(isEnabled).compactMap(formatForSystemPrompt)
    }

    private func sanitize(_ entry: String, fileName: String, _ target: MemoryTarget) -> String {
        let findings = MemoryThreats.scan(entry)
        guard !findings.isEmpty else { return entry }
        return "[BLOCKED: \(fileName) entry contained threat pattern(s): \(findings.joined(separator: ", ")). "
            + "Removed from system prompt; use memory(action=remove, target='\(target.rawValue)') "
            + "to delete the original.]"
    }

    func renderBlock(_ target: MemoryTarget, _ entries: [String]) -> String {
        guard !entries.isEmpty else { return "" }
        let content = entries.joined(separator: Self.entryDelimiter)
        let separator = String(repeating: "═", count: 46)
        return "\(separator)\n\(target.blockHeader) [\(usagePercent(target, content.scalarCount))]\n\(separator)\n\(content)"
    }

    // MARK: Usage text

    func charCount(_ target: MemoryTarget) -> Int { entries(for: target).joined(separator: Self.entryDelimiter).scalarCount }

    func usage(_ target: MemoryTarget) -> String {
        "\(groupedNumber(charCount(target)))/\(groupedNumber(charLimit(for: target)))"
    }

    func usagePercent(_ target: MemoryTarget, _ current: Int) -> String {
        let limit = charLimit(for: target)
        let percent = limit > 0 ? min(100, Int((Double(current) / Double(limit)) * 100)) : 0
        return "\(percent)% — \(groupedNumber(current))/\(groupedNumber(limit)) chars"
    }

    // MARK: Actions

    /// Appends an entry. An exact duplicate succeeds without adding.
    public func add(_ target: MemoryTarget, _ content: String) -> MemoryResult {
        lastFailureClass = "other"
        let content = content.pythonStripped
        if content.isEmpty { return fail("Content cannot be empty.", "missing_content") }
        // Text longer than the whole store is refused by the limit check, so it skips the scan.
        if content.scalarCount <= charLimit(for: target), let scan = MemoryThreats.firstThreatMessage(content) {
            return fail(scan, "scan_blocked")
        }
        if Self.splitsOnReload(content) { return fail(Self.delimiterMessage, "invalid_args") }
        // Appending never clobbers foreign text, so the drift guard is skipped; a failed read still refuses.
        return mutate(target, skipDrift: true) { entries, limit in
            if entries.containsExactly(content) {
                return .done(self.successResponse(target, "Entry already exists (no duplicate added)."))
            }
            if (entries + [content]).joined(separator: Self.entryDelimiter).scalarCount > limit {
                return .done(self.addOverflow(target, content, limit))
            }
            return .write(entries + [content], "Entry added.", [])
        }
    }

    /// Replaces the WHOLE entry that `oldText` locates. Kept for staged writes; the tool offers `patch` instead.
    public func replace(_ target: MemoryTarget, oldText: String, newContent: String,
                        matchedEntry: String? = nil) -> MemoryResult {
        lastFailureClass = "other"
        let newContent = newContent.pythonStripped
        let oldText = oldText.pythonStripped
        if oldText.isEmpty { return fail("old_text cannot be empty.", "missing_old_text") }
        if newContent.isEmpty { return fail("new_content cannot be empty. Use 'remove' to delete entries.", "missing_content") }
        if newContent.scalarCount <= charLimit(for: target), let scan = MemoryThreats.firstThreatMessage(newContent) {
            return fail(scan, "scan_blocked")
        }
        if Self.splitsOnReload(newContent) { return fail(Self.delimiterMessage, "invalid_args") }
        return edit(target, oldText, newContent, matchedEntry)
    }

    /// Replaces one span inside the entry containing `oldText` and keeps the rest of the entry.
    public func patch(_ target: MemoryTarget, oldText: String, newText: String,
                      matchedEntry: String? = nil) -> MemoryResult {
        lastFailureClass = "other"
        if oldText.pythonStripped.isEmpty {
            return fail("old_text cannot be empty.", "missing_old_text")
        }
        if newText.pythonStripped.isEmpty {
            return fail("new_text cannot be empty. \(Self.patchDropHint)", "missing_content")
        }
        return mutate(target) { entries, limit in
            let idx: Int
            switch self.locate(entries, oldText, "patch", matchedEntry) {
            case let .failure(result): return .done(result)
            case let .index(i): idx = i
            }
            let before = entries[idx]
            let after: String
            switch Self.patchEntry(before, oldText, newText, limit) {
            case let .failure(message, failureClass): return .done(self.fail(message, failureClass))
            case let .success(patched): after = patched
            }
            var patched = entries
            patched[idx] = after
            let newTotal = patched.joined(separator: Self.entryDelimiter).scalarCount
            if newTotal > limit {
                return .done(self.failureWithEntries(target,
                    "Patch would put memory at \(groupedNumber(newTotal))/\(groupedNumber(limit)) chars. Shorten the new "
                        + "text, or 'remove' other stale or less important entries to make room "
                        + "(see current_entries below), then retry — all in this turn."))
            }
            return .write(patched, "Entry patched.",
                          [("patched_entry_before", .string(before)), ("patched_entry_after", .string(after))])
        }
    }

    /// Removes the entry containing `oldText`.
    public func remove(_ target: MemoryTarget, oldText: String, matchedEntry: String? = nil) -> MemoryResult {
        lastFailureClass = "other"
        let oldText = oldText.pythonStripped
        if oldText.isEmpty { return fail("old_text cannot be empty.", "missing_old_text") }
        return edit(target, oldText, nil, matchedEntry)
    }

    /// `{"success": true, "matched_entry": <full entry>}` for the entry `oldText` selects now, read under the lock.
    public func resolveEntry(_ target: MemoryTarget, oldText: String, verb: String) -> MemoryResult {
        lastFailureClass = "other"
        return mutate(target, skipDrift: true) { entries, _ in
            switch self.locate(entries, oldText.pythonStripped, verb) {
            case let .failure(result): return .done(result)
            case let .index(i): return .done(MemoryResult([("success", .bool(true)), ("matched_entry", .string(entries[i]))]))
            }
        }
    }

    /// Applies add/patch/remove (and staged replace) operations all-or-nothing, checking the limit on the final state.
    public func applyBatch(_ target: MemoryTarget, _ operations: [MemoryOperation]) -> MemoryResult {
        lastFailureClass = "other"
        return batch(target, operations, commit: true)
    }

    /// Dry run of `applyBatch`: on success, `matched_entries` lists per operation the entry it selects now.
    public func resolveBatchEntries(_ target: MemoryTarget, _ operations: [MemoryOperation]) -> MemoryResult {
        lastFailureClass = "other"
        return batch(target, operations, commit: false)
    }

    private func edit(_ target: MemoryTarget, _ oldText: String, _ newContent: String?,
                      _ matchedEntry: String?) -> MemoryResult {
        mutate(target) { entries, limit in
            let idx: Int
            switch self.locate(entries, oldText, newContent == nil ? "remove" : "replace", matchedEntry) {
            case let .failure(result): return .done(result)
            case let .index(i): idx = i
            }
            var replaced = entries
            guard let newContent else {
                replaced.remove(at: idx)
                return .write(replaced, "Entry removed.", [("removed_entry", .string(entries[idx]))])
            }
            replaced[idx] = newContent
            let newTotal = replaced.joined(separator: Self.entryDelimiter).scalarCount
            if newTotal > limit {
                return .done(self.failureWithEntries(target,
                    "Replacement \(Self.overLimit(newTotal, limit)). Shorten the new content, or retry as ONE "
                        + "'operations' batch with this replace plus removes/shortenings of other stale entries "
                        + "(see current_entries below) — the limit is checked only on the batch result."))
            }
            return .write(replaced, "Entry replaced.", [("replaced_entry", .string(entries[idx]))])
        }
    }

    private enum BatchStep {
        case failed(String, String, closest: [String]?)
        case applied(previous: String?, patched: String?)
    }

    private func batchStep(_ working: inout [String], _ act: String?, _ content: String, _ oldText: String,
                           _ pos: String, _ matchedEntry: String?, _ limit: Int) -> BatchStep {
        if act == "add" {
            if content.isEmpty { return .failed("\(pos): content is required.", "missing_content", closest: nil) }
            if !working.containsExactly(content) { working.append(content) }
            return .applied(previous: nil, patched: nil)
        }
        guard let act, ["replace", "remove", "patch"].contains(act) else {
            return .failed("\(pos): unknown action. Use add, patch, or remove.", "invalid_args", closest: nil)
        }
        if oldText.pythonStripped.isEmpty {
            return .failed("\(pos): old_text is required.", "missing_old_text", closest: nil)
        }
        if act == "replace" && content.isEmpty {
            return .failed("\(pos): content is required — set this operation object's own 'content' to the COMPLETE "
                + "new entry (or use action='remove' to delete).", "missing_content", closest: nil)
        }
        if act == "patch" && content.pythonStripped.isEmpty {
            return .failed("\(pos): content is required — set this operation object's own 'content' to the new "
                + "text for the old_text span. \(Self.patchDropHint)", "missing_content", closest: nil)
        }
        let idx: Int
        if let matchedEntry {
            switch Self.pinnedIndex(working, matchedEntry, oldText) {
            case let .failure(message, failureClass): return .failed("\(pos): \(message)", failureClass, closest: nil)
            case let .success(pinned): idx = pinned
            }
        } else {
            let (found, ambiguous) = Self.findUniqueMatch(working, oldText)
            if ambiguous {
                return .failed("\(pos): '\(MemoryText.echo(oldText))' matched multiple distinct entries -- be more specific.",
                               "ambiguous", closest: nil)
            }
            guard let found else {
                let miss = Self.noMatchError(working, oldText, act)
                return .failed("\(pos): \(miss.message)", "no_match", closest: miss.closest)
            }
            idx = found
        }
        let previous = working[idx]
        if act != "patch" {
            if act == "replace" { working[idx] = content } else { working.remove(at: idx) }
            return .applied(previous: previous, patched: nil)
        }
        switch Self.patchEntry(previous, oldText, content, limit) {
        case let .failure(message, failureClass): return .failed("\(pos): \(message)", failureClass, closest: nil)
        case let .success(patched):
            working[idx] = patched
            return .applied(previous: previous, patched: patched)
        }
    }

    private func batch(_ target: MemoryTarget, _ operations: [MemoryOperation], commit: Bool) -> MemoryResult {
        if let error = targetError(target.rawValue, operations) { return error }
        if operations.isEmpty { return fail("operations list is empty.", "invalid_args") }
        // Scan every add/replace before touching disk: one poisoned op rejects the batch. Hermes scans only the
        // `content` key; the `new_text` alias is scanned here too because the op writes it.
        for (i, op) in operations.enumerated() where op.action == "add" || op.action == "replace" {
            let content = op.effectiveContent
            if !content.isEmpty, content.scalarCount <= charLimit(for: target),
               let scan = MemoryThreats.firstThreatMessage(content) {
                return fail("Operation \(i + 1): \(scan)", "scan_blocked")
            }
            if Self.splitsOnReload(content) { return fail("Operation \(i + 1): \(Self.delimiterMessage)", "invalid_args") }
        }
        return mutate(target, skipDrift: !commit) { entries, limit in
            var working = entries
            var matched: [String?] = []
            var produced: [(Int, String)] = []
            for (i, op) in operations.enumerated() {
                var content = op.effectiveContent
                var oldText = op.oldText ?? ""
                if op.action != "patch" {
                    content = content.pythonStripped
                    oldText = oldText.pythonStripped
                }
                let pos = "Operation \(i + 1) (\(op.action.flatMap { $0.isEmpty ? nil : $0 } ?? "unknown"))"
                switch self.batchStep(&working, op.action, content, oldText, pos, op.matchedEntry, limit) {
                case let .failed(message, failureClass, closest):
                    return .done(self.batchFailure(target, message, failureClass, closest: closest))
                case let .applied(previous, patched):
                    matched.append(previous)
                    if let patched { produced.append((i + 1, patched)) }
                }
            }
            if !entries.isEmpty && working.isEmpty && !(operations.count == 1 && operations[0].action == "remove") {
                return .done(self.batchFailure(target,
                    "Refusing to empty \(target.fileName): this batch would remove every entry from a "
                        + "previously non-empty store. Keep at least one entry — merge overlapping "
                        + "entries into a shorter one instead of removing the last one. To delete the "
                        + "final entry deliberately, remove it alone in a one-item batch.", "would_empty"))
            }
            let newTotal = working.joined(separator: Self.entryDelimiter).scalarCount
            // A remove-only batch or a lone duplicate add never grows the store, so excess left by a lowered
            // limit does not block it.
            let grows = !(operations.allSatisfy { $0.action == "remove" } || (operations.count == 1 && working.isExactly(entries)))
            if grows && newTotal > limit {
                if operations.count == 1 && operations[0].action == "add" {
                    return .done(self.addOverflow(target,
                        operations[0].effectiveContent.pythonStripped, limit))
                }
                return .done(self.batchFailure(target,
                    "Applying all \(operations.count) operations \(Self.overLimit(newTotal, limit)) more. Add "
                        + "removes or shortening patches of other stale entries to the same batch, then retry.",
                    "over_budget"))
            }
            if !commit {
                return .done(MemoryResult([("success", .bool(true)),
                                           ("matched_entries", .array(matched.map { $0.map(MemoryJSON.string) ?? .null }))]))
            }
            var replaced: [(String, MemoryJSON)] = [], removed: [(String, MemoryJSON)] = [],
                patchedBefore: [(String, MemoryJSON)] = []
            for (i, (op, previous)) in zip(operations, matched).enumerated() {
                guard let previous else { continue }
                let item = (String(i + 1), MemoryJSON.string(previous))
                switch op.action {
                case "replace": replaced.append(item)
                case "patch": patchedBefore.append(item)
                case "remove": removed.append(item)
                default: break
                }
            }
            var extra: [(String, MemoryJSON)] = []
            if !replaced.isEmpty { extra.append(("replaced_entries", .object(replaced))) }
            if !patchedBefore.isEmpty {
                extra.append(("patched_entries", .object(patchedBefore)))
                extra.append(("patched_entries_after", .object(produced.map { (String($0.0), .string($0.1)) })))
            }
            if !removed.isEmpty { extra.append(("removed_entries", .object(removed))) }
            return .write(working, "Applied \(operations.count) operation(s).", extra)
        }
    }

    /// Per-call target validation shared with the tool: disabled stores and mixed per-operation targets.
    func targetError(_ target: String, _ operations: [MemoryOperation]?) -> MemoryResult? {
        for (index, op) in (operations ?? []).enumerated() where op.target != nil && op.target != target {
            return fail("Operation \(index + 1) names a different target than the one at the top level. "
                + "One call writes one store: retry with separate calls for different stores. "
                + "Nothing was written or staged.", "invalid_args")
        }
        guard let resolved = MemoryTarget(rawValue: target), !isEnabled(resolved) else { return nil }
        lastFailureClass = "disabled"
        return MemoryResult([("success", .bool(false)),
                             ("error", .string("Built-in \(resolved.fileName) writes are disabled in memory config.")),
                             ("target", .string(target))])
    }

    // MARK: Locked read-modify-write

    private enum Mutation {
        case done(MemoryResult)
        case write([String], String, [(String, MemoryJSON)])
    }

    /// Locks, re-reads from disk, runs `body`, then persists. Refuses when the file exists but can't be read, and
    /// (unless `skipDrift`) when an entry is larger than the whole limit and the write would add or rewrite text.
    private func mutate(_ target: MemoryTarget, skipDrift: Bool = false,
                        _ body: ([String], Int) -> Mutation) -> MemoryResult {
        let path = path(for: target)
        do {
            return try Self.withFileLock(path) {
                let (raw, readOK) = Self.readRaw(path)
                if !readOK { return self.readFailedError(path) }
                let oversized = !skipDrift && self.hasOversizedEntry(target, raw)
                let before = Self.deduplicated(Self.parseEntries(raw))
                self.setEntries(target, before)
                self.lastFailureClass = "other"
                switch body(before, self.charLimit(for: target)) {
                case let .done(result):
                    if oversized && !result.success && self.lastFailureClass == "over_budget" {
                        return self.driftError(path, self.driftBackup(target, raw))
                    }
                    return result
                case .write(let entries, let message, var extra):
                    if oversized {
                        if !Set(entries.map(\.exactKey)).isSubset(of: Set(before.map(\.exactKey))) {
                            return self.driftError(path, self.driftBackup(target, raw))
                        }
                        let limit = self.charLimit(for: target)
                        if before.contains(where: { $0.scalarCount > limit && !entries.containsExactly($0) }) {
                            let backup = self.driftBackup(target, raw)
                            if backup.hasSuffix(Self.backupFailed) { return self.driftError(path, backup) }
                            extra.append(("drift_backup", .string(backup)))
                        }
                    }
                    do {
                        try Self.atomicWrite(path, entries.joined(separator: Self.entryDelimiter))
                    } catch {
                        return self.fail("Failed to write memory file \(path.path): \(error.localizedDescription)", "other")
                    }
                    self.setEntries(target, entries)
                    return self.successResponse(target, message, extra)
                }
            }
        } catch {
            return fail("Failed to lock memory file \(path.path): \(error.localizedDescription)", "other")
        }
    }

    private enum Located {
        case index(Int)
        case failure(MemoryResult)
    }

    private func locate(_ entries: [String], _ oldText: String, _ verb: String, _ matchedEntry: String? = nil) -> Located {
        if let matchedEntry {
            switch Self.pinnedIndex(entries, matchedEntry, oldText) {
            case let .failure(message, failureClass): return .failure(fail(message, failureClass))
            case let .success(idx): return .index(idx)
            }
        }
        let (idx, ambiguous) = Self.findUniqueMatch(entries, oldText)
        if ambiguous {
            let previews = Self.substringMatches(entries, oldText).map {
                entries[$0].scalarPrefix(80) + (entries[$0].scalarCount > 80 ? "..." : "")
            }
            return .failure(fail("Multiple entries matched '\(MemoryText.echo(oldText))'. Be more specific.", "ambiguous",
                                 [("matches", previews.json)]))
        }
        guard let idx else {
            let miss = Self.noMatchError(entries, oldText, verb)
            var extra: [(String, MemoryJSON)] = miss.closest.map { [("closest_entries", $0.json)] } ?? []
            extra.append(("current_entries", entries.json))
            return .failure(consolidationFailure(fail(miss.message, "no_match", extra)))
        }
        return .index(idx)
    }

    // MARK: Results

    func fail(_ message: String, _ failureClass: String, _ extra: [(String, MemoryJSON)] = []) -> MemoryResult {
        lastFailureClass = failureClass
        return MemoryResult([("success", .bool(false)), ("error", .string(message))] + extra)
    }

    /// A successful write is terminal and omits the entries list so the model doesn't re-issue the same ops.
    private func successResponse(_ target: MemoryTarget, _ message: String?,
                                 _ extra: [(String, MemoryJSON)] = []) -> MemoryResult {
        consolidationFailures = 0
        lastFailureClass = "none"
        var fields: [(String, MemoryJSON)] = [
            ("success", .bool(true)), ("done", .bool(true)), ("target", .string(target.rawValue)),
            ("usage", .string(usagePercent(target, charCount(target)))),
            ("entry_count", .int(entries(for: target).count)),
        ]
        if let message { fields.append(("message", .string(message))) }
        fields += extra
        fields.append(("note", .string("Write saved. This update is complete — do not repeat it.")))
        return MemoryResult(fields)
    }

    private func consolidationFailure(_ response: MemoryResult) -> MemoryResult {
        consolidationFailures += 1
        if consolidationFailures <= Self.maxConsolidationFailuresPerTurn { return response }
        lastFailureClass = "retry_cap"
        return MemoryResult([
            ("success", .bool(false)), ("done", .bool(true)),
            ("error", .string("Memory consolidation failed \(consolidationFailures) times this turn. Stop retrying "
                + "memory calls — leave memory unchanged for now and continue with your reply to the user. "
                + "The fact can be saved in a later turn.")),
        ])
    }

    private func failureWithEntries(_ target: MemoryTarget, _ message: String) -> MemoryResult {
        consolidationFailure(fail(message, "over_budget", [("current_entries", entries(for: target).json),
                                                           ("usage", .string(usage(target)))]))
    }

    private func addOverflow(_ target: MemoryTarget, _ content: String, _ limit: Int) -> MemoryResult {
        let over = (entries(for: target) + [content]).joined(separator: Self.entryDelimiter).scalarCount - limit
        return failureWithEntries(target,
            "Memory at \(groupedNumber(charCount(target)))/\(groupedNumber(limit)) chars; adding this entry "
                + "(\(content.scalarCount) chars) would exceed the limit by \(groupedNumber(over)) chars. Retry as ONE "
                + "'operations' batch that removes or shortens (patch) stale entries from current_entries below to free "
                + "at least \(groupedNumber(over)) chars AND adds this entry — the limit is checked only on the batch result.")
    }

    private func batchFailure(_ target: MemoryTarget, _ message: String, _ failureClass: String,
                              closest: [String]? = nil) -> MemoryResult {
        var extra: [(String, MemoryJSON)] = [("usage", .string(usage(target)))]
        if let closest, !closest.isEmpty { extra.append(("closest_entries", closest.json)) }
        return consolidationFailure(fail(message + " No operations were applied (batch is all-or-nothing).",
                                         failureClass, extra))
    }

    private func readFailedError(_ path: URL) -> MemoryResult {
        fail("Refusing to write \(path.lastPathComponent): the file exists on disk but could not be read right now "
            + "(temporarily locked by another program, a permission change, invalid/corrupt text encoding, "
            + "or a filesystem error). Treating an unreadable file as empty and saving would wipe existing "
            + "memory, so the write is refused. Nothing was changed — retry in a moment.", "read_failed")
    }

    private func driftError(_ path: URL, _ backup: String) -> MemoryResult {
        let target = path.lastPathComponent == "USER.md" ? "user" : "memory"
        return fail("Refusing to write \(path.lastPathComponent): it holds an entry larger than the whole store's char limit, "
            + "i.e. free-form text written without § delimiters (the patch tool, a shell append, a manual "
            + "edit). Rewriting it would lose that text. A snapshot was saved to \(backup). Resolve the drift first — "
            + "either rewrite the file as a clean §-delimited list of entries, or move the extra "
            + "content out — then retry. This guard exists to prevent silent data loss (issue #26045).", "drift", [
                ("drift_backup", .string(backup)),
                ("remediation", .string("Open the .bak file, integrate the missing entries into the memory tool with one "
                    + "add op per entry via memory(target='\(target)', operations=[{action: 'add', content: ...}]), "
                    + "then remove or rewrite the original file to a clean state.")),
            ])
    }

    private static let backupFailed = " (BACKUP FAILED — file unchanged on disk)"

    /// True when one entry is longer than the whole limit: free text appended without delimiters, or an entry
    /// written before the limit was lowered.
    private func hasOversizedEntry(_ target: MemoryTarget, _ raw: String) -> Bool {
        (Self.parseEntries(raw).map(\.scalarCount).max() ?? 0) > charLimit(for: target)
    }

    /// `<file>.bak.<unix time>[.<n>]`, created exclusively so two snapshots in one second never collide.
    private func driftBackup(_ target: MemoryTarget, _ raw: String) -> String {
        let base = path(for: target).path + ".bak.\(Int(now().timeIntervalSince1970))"
        var n = 0
        while true {
            let candidate = base + (n == 0 ? "" : ".\(n)")
            let fd = open(candidate, O_WRONLY | O_CREAT | O_EXCL, 0o600)
            if fd < 0 {
                if errno == EEXIST { n += 1; continue }
                return candidate + Self.backupFailed
            }
            let data = Data(raw.utf8)
            let written = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
            close(fd)
            return written == data.count ? candidate : candidate + Self.backupFailed
        }
    }

    private func setEntries(_ target: MemoryTarget, _ entries: [String]) {
        if target == .user { userEntries = entries } else { memoryEntries = entries }
    }

    // MARK: Matching

    private enum Patched {
        case success(String)
        case failure(String, String)
    }

    private enum Pinned {
        case success(Int)
        case failure(String, String)
    }

    static let delimiterMessage = "Content cannot contain the entry separator (a line holding only '§') or end with a "
        + "line holding only '§': it would split into several entries when the file is read back. "
        + "Use one operation per entry."

    /// True when the text, written between two other entries, would not read back as one entry.
    static func splitsOnReload(_ entry: String) -> Bool {
        let text = MemoryText.normalizeNewlines(entry).pythonStripped
        return MemoryText.contains(text, entryDelimiter) || text.unicodeScalars.suffix(2).elementsEqual("\n§".unicodeScalars)
    }

    /// Splices `newText` over the single occurrence of `oldText`; refuses repeated spans, no-ops, threats (a result over
    /// `limit` skips the scan because the caller refuses it), and
    /// results that would split into several entries.
    private static func patchEntry(_ entry: String, _ oldText: String, _ newText: String, _ limit: Int) -> Patched {
        let count = spanOccurrences(entry, oldText)
        if count > 1 {
            return .failure("'\(MemoryText.echo(oldText))' appears \(count) times in the matched entry, and patch edits "
                + "one span. Include more surrounding text so old_text occurs once.", "ambiguous")
        }
        let patched = MemoryText.replaceFirst(entry, oldText, newText)
        if patched.isExactly(entry) {
            return .failure("Patch is a no-op: '\(MemoryText.echo(oldText))' -> '\(MemoryText.echo(newText))' leaves the "
                + "entry unchanged.", "invalid_args")
        }
        if patched.scalarCount <= limit, let scan = MemoryThreats.firstThreatMessage(patched) {
            return .failure(scan, "scan_blocked")
        }
        if splitsOnReload(patched) { return .failure(delimiterMessage, "invalid_args") }
        return .success(patched)
    }

    /// The pinned entry's index. It must still be in memory and still contain `oldText`.
    private static func pinnedIndex(_ entries: [String], _ matchedEntry: String, _ oldText: String) -> Pinned {
        guard let idx = entries.firstExactIndex(of: matchedEntry) else {
            return .failure(staleEntryMessage(matchedEntry), "stale_entry")
        }
        if substringMatches([matchedEntry], oldText).isEmpty {
            return .failure("The pinned entry '\(MemoryText.echo(matchedEntry))' does not contain old_text "
                + "'\(MemoryText.echo(oldText))', so this write was not applied.", "stale_entry")
        }
        return .success(idx)
    }

    /// Occurrences including overlapping ones: '00' occurs twice in '1000'.
    static func spanOccurrences(_ text: String, _ span: String) -> Int {
        let hay = Array(text.unicodeScalars), needle = Array(span.unicodeScalars)
        guard !needle.isEmpty, hay.count >= needle.count else { return 0 }
        return (0...(hay.count - needle.count)).filter { hay[$0..<($0 + needle.count)].elementsEqual(needle) }.count
    }

    private static let quoteFold: Set<Unicode.Scalar> = Set("\"`\u{2018}\u{2019}\u{201A}\u{201B}\u{201C}\u{201D}\u{201E}\u{201F}".unicodeScalars)
    private static let dashFold: Set<Unicode.Scalar> = Set("\u{2010}\u{2011}\u{2012}\u{2013}\u{2014}\u{2015}\u{2212}".unicodeScalars)

    /// Folds every quote/backtick to "'", every dash/minus to "-", a literal backslash-n and whitespace runs to one space.
    static func normalizeForMatch(_ text: String) -> String {
        var words: [[Unicode.Scalar]] = [[]]
        var previousBackslash = false
        for scalar in text.unicodeScalars {
            if previousBackslash {
                previousBackslash = false
                if scalar == "n" {
                    words.append([])
                    continue
                }
                words[words.count - 1].append("\\")
            }
            if scalar == "\\" {
                previousBackslash = true
            } else if MemoryText.isSpace(scalar) {
                words.append([])
            } else {
                words[words.count - 1].append(quoteFold.contains(scalar) ? "'" : dashFold.contains(scalar) ? "-" : scalar)
            }
        }
        if previousBackslash { words[words.count - 1].append("\\") }
        return words.filter { !$0.isEmpty }.map(MemoryText.string).joined(separator: " ")
    }

    private static func isAlphanumeric(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.properties.generalCategory {
        case .uppercaseLetter, .lowercaseLetter, .titlecaseLetter, .modifierLetter, .otherLetter,
             .decimalNumber, .letterNumber, .otherNumber: return true
        default: return false
        }
    }

    /// Entries containing `oldText`: exact substring first, else typography-folded.
    static func substringMatches(_ entries: [String], _ oldText: String) -> [Int] {
        let exact = entries.indices.filter { MemoryText.contains(entries[$0], oldText) }
        if !exact.isEmpty { return exact }
        let needle = normalizeForMatch(oldText)
        guard needle.unicodeScalars.contains(where: isAlphanumeric) else { return [] }
        return entries.indices.filter { MemoryText.contains(normalizeForMatch(entries[$0]), needle) }
    }

    /// A whole-entry match wins over substring matches; matches with distinct texts are ambiguous.
    static func findUniqueMatch(_ entries: [String], _ oldText: String) -> (Int?, Bool) {
        let exact = entries.indices.filter { entries[$0].isExactly(oldText) }
        let matches = exact.isEmpty ? substringMatches(entries, oldText) : exact
        if Set(matches.map { entries[$0].exactKey }).count > 1 { return (nil, true) }
        return (matches.first, false)
    }

    private static let wordPattern = try! NSRegularExpression(pattern: #"\w+"#)

    private static func words(_ text: String) -> Set<String> {
        let lower = text.lowercased()
        let ns = lower as NSString
        return Set(wordPattern.matches(in: lower, range: NSRange(location: 0, length: ns.length)).map { ns.substring(with: $0.range) })
    }

    /// The zero-match message, plus up to three entries sharing at least 30% of `oldText`'s words.
    static func noMatchError(_ entries: [String], _ oldText: String, _ verb: String) -> (message: String, closest: [String]?) {
        let wanted = words(oldText)
        var scored: [(score: Double, index: Int)] = []
        if !wanted.isEmpty {
            for i in entries.indices {
                scored.append((Double(wanted.intersection(words(entries[i])).count) / Double(wanted.count), i))
            }
        }
        scored.sort { $0.score != $1.score ? $0.score > $1.score : $0.index < $1.index }
        let closest = scored.prefix(3).filter { $0.0 >= 0.3 }.map { entries[$0.1] }
        let hint = oldText.unicodeScalars.contains("§")
            ? " old_text must come from ONE entry ('§' separates entries); use one op per entry." : ""
        let message = "No entry matched '\(MemoryText.echo(oldText))'.\(hint) Retry the \(verb) with old_text copied verbatim "
            + "from the intended entry" + (closest.isEmpty ? "." : " (closest_entries below).")
        return (message, closest.isEmpty ? nil : closest)
    }

    private static func overLimit(_ newTotal: Int, _ limit: Int) -> String {
        "would put memory at \(groupedNumber(newTotal))/\(groupedNumber(limit)) chars: free at least \(groupedNumber(newTotal - limit)) chars"
    }

    private static func staleEntryMessage(_ entry: String) -> String {
        "Entry changed since it was staged, so this write was not applied: '\(MemoryText.echo(entry))' is no longer "
            + "in memory as reviewed. Recreate the change against the current entry or reject it; the "
            + "pending record has been preserved."
    }

    // MARK: Files

    /// Stripped, non-empty entries. Line endings are normalized first, as Python's text mode does, and the split is on
    /// the full delimiter so a bare "§" inside an entry survives.
    static func parseEntries(_ raw: String) -> [String] {
        MemoryText.split(MemoryText.normalizeNewlines(raw), on: entryDelimiter)
            .map(\.pythonStripped)
            .filter { !$0.isEmpty }
    }

    static func deduplicated(_ entries: [String]) -> [String] {
        var seen = Set<[UInt8]>()
        return entries.filter { seen.insert($0.exactKey).inserted }
    }

    /// `readOK` is false only when the file exists but can't be read or isn't valid UTF-8. A leading BOM is dropped.
    static func readRaw(_ path: URL) -> (raw: String, readOK: Bool) {
        guard FileManager.default.fileExists(atPath: path.path) else { return ("", true) }
        guard var data = try? Data(contentsOf: path) else { return ("", false) }
        if data.starts(with: [0xEF, 0xBB, 0xBF]) { data.removeFirst(3) }
        guard let text = String(validating: data, as: UTF8.self) else { return ("", false) }
        return (text, true)
    }

    /// The file a write lands on: a symlink's final target, even when that target doesn't exist yet, else the path.
    static func publishPath(_ path: URL) -> URL {
        var current = path.path
        for _ in 0..<40 {
            guard let destination = try? FileManager.default.destinationOfSymbolicLink(atPath: current) else { break }
            current = destination.hasPrefix("/") ? destination
                : (current as NSString).deletingLastPathComponent + "/" + destination
        }
        return URL(fileURLWithPath: current).standardizedFileURL
    }

    /// Temp file beside the target, fsync, then rename: readers never see a partial file. A symlink, dangling or not,
    /// is kept and its target written, as Hermes's `atomic_replace` does; only the link's own folder is created.
    static func atomicWrite(_ path: URL, _ content: String) throws {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        let target = publishPath(path)
        let directory = target.deletingLastPathComponent()
        var template = Array(directory.appendingPathComponent(".mem_XXXXXX.tmp").path.utf8CString)
        let fd = mkstemps(&template, 4)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        let tempPath = String(cString: template)
        let bytes = Array(content.utf8)
        var offset = 0
        var failure: Int32 = 0
        while offset < bytes.count {
            let n = bytes.withUnsafeBytes { write(fd, $0.baseAddress! + offset, bytes.count - offset) }
            if n <= 0 {
                failure = n < 0 ? errno : EIO
                break
            }
            offset += n
        }
        if failure == 0 && fsync(fd) != 0 { failure = errno }
        close(fd)
        if failure == 0 && rename(tempPath, target.path) != 0 { failure = errno }
        if failure != 0 {
            unlink(tempPath)
            throw POSIXError(POSIXErrorCode(rawValue: failure) ?? .EIO)
        }
    }

    /// Exclusive `flock` on `<file>.lock`, so the memory file itself can still be replaced by rename.
    static func withFileLock<T>(_ path: URL, _ body: () -> T) throws -> T {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        let fd = open(path.path + ".lock", O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        // The creation mode passes through the umask and doesn't fix an older loose lock file.
        fchmod(fd, 0o600)
        guard flock(fd, LOCK_EX) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { flock(fd, LOCK_UN) }
        return body()
    }
}

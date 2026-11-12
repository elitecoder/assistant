// Ported from Hermes Agent (MIT) tools/memory_tool.py, tools/write_approval.py, and agent/prompt_builder.py (build_memory_guidance).
import Foundation

/// Who is writing: a live turn, or an unattended background review that may only add.
public enum MemoryWriteOrigin: String, Sendable {
    case foreground
    case backgroundReview = "background_review"
}

/// Hermes's `memory.write_approval`. Off by default: writes land directly.
public struct MemoryWriteApproval {
    public var enabled: Bool
    /// Asks the person inline. Return true to save, false to deny, nil (or no handler) to stage for later review.
    /// Arguments are a title ("Save to memory: add to memory") and the text being saved.
    public var prompt: ((_ title: String, _ detail: String) -> Bool?)?
    /// How the staged message tells the model where review happens.
    public var reviewHint: String

    public init(enabled: Bool = false, prompt: ((String, String) -> Bool?)? = nil,
                reviewHint: String = "review with /memory pending") {
        self.enabled = enabled
        self.prompt = prompt
        self.reviewHint = reviewHint
    }
}

/// A write held for approval, stored as `<folder>/pending/memory/<id>.json`.
public struct MemoryPendingWrite: Equatable, Sendable {
    public var id: String
    public var action: String
    public var summary: String
    public var origin: String
    public var createdAt: Double
    /// The exact call to replay on approval: `{action, target, content, old_text, matched_entry}` or a batch.
    public var payload: MemoryJSON
}

public final class MemoryPendingStore {
    public let directory: URL
    var now: () -> Date = Date.init

    public init(directory: URL) { self.directory = directory }

    public func stage(_ payload: MemoryJSON, summary: String, origin: MemoryWriteOrigin) throws -> MemoryPendingWrite {
        let record = MemoryPendingWrite(id: String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(8)),
                                        action: payload["action"]?.string ?? "",
                                        summary: summary.pythonStripped,
                                        origin: origin.rawValue, createdAt: now().timeIntervalSince1970, payload: payload)
        let json = MemoryJSON.object([
            ("id", .string(record.id)), ("subsystem", .string("memory")), ("action", .string(record.action)),
            ("summary", .string(record.summary)), ("origin", .string(record.origin)),
            ("created_at", .double(record.createdAt)), ("payload", payload),
        ])
        try MemoryStore.atomicWrite(directory.appendingPathComponent("\(record.id).json"), json.encoded)
        return record
    }

    /// Every pending write, oldest first. Unreadable records are skipped.
    public func list() -> [MemoryPendingWrite] {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.pathExtension == "json" }.compactMap(read).sorted { $0.createdAt < $1.createdAt }
    }

    public func get(_ id: String) -> MemoryPendingWrite? { recordURL(id).flatMap(read) }

    @discardableResult
    public func discard(_ id: String) -> Bool {
        guard let url = recordURL(id) else { return false }
        return (try? FileManager.default.removeItem(at: url)) != nil
    }

    /// Ids are the lowercase hex this store generates; anything else (a path, "..") names no record.
    private func recordURL(_ id: String) -> URL? {
        guard !id.isEmpty, id.count <= 32, id.unicodeScalars.allSatisfy({ ("0"..."9").contains($0) || ("a"..."f").contains($0) })
        else { return nil }
        return directory.appendingPathComponent("\(id).json")
    }

    private func read(_ url: URL) -> MemoryPendingWrite? {
        guard let raw = try? String(contentsOf: url, encoding: .utf8), let json = MemoryJSON.parse(raw),
              let id = json["id"]?.string, let payload = json["payload"] else { return nil }
        return MemoryPendingWrite(id: id, action: json["action"]?.string ?? "", summary: json["summary"]?.string ?? "",
                                  origin: json["origin"]?.string ?? "foreground",
                                  createdAt: json["created_at"]?.number ?? 0, payload: payload)
    }
}

/// The `memory` tool the model calls: argument validation, approval gates, and Hermes's result JSON.
public final class MemoryTool {
    public static let name = "memory"

    public static let description =
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        + "injected into every future turn, so keep entries compact and high-signal.\n\n"
        + "HOW: make ALL your changes to one target in ONE call via an 'operations' array (each "
        + "item: {action, content?, old_text?}). The batch applies atomically and the char limit is "
        + "checked only on the FINAL result — so a single call can remove/patch stale entries "
        + "to free room AND add new ones, even when an add alone would overflow. The response "
        + "reports current/limit chars and confirms completion; one batch call finishes the "
        + "update, so don't repeat it.\n\n"
        + "WHEN: only for facts that apply to EVERY session regardless of task: who the user "
        + "is, stable environment facts, standing conventions with no task home. Anything "
        + "learned while doing a task (procedures, pitfalls, and the user's preferences and "
        + "corrections for that kind of work) belongs in the task's skill via skill_manage, "
        + "where it loads only when relevant; memory is injected into every turn and must "
        + "stay small.\n\n"
        + "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
        + "removes or shortens enough stale entries and adds the new one together.\n\n"
        + targetsText + "\n\n"
        + "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        + "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        + "procedures belong in a skill, not memory."

    private static let targetsText = "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        + "notes (environment, conventions, tool quirks, lessons)."

    /// The JSON Schema of the tool's parameters.
    public static let parameters = parameters(targets: ["memory", "user"],
                                              targetDescription: "Which memory store: 'memory' for personal notes, 'user' for user profile.")

    public static var parametersJSON: String { parameters.encoded }

    private static func parameters(targets: [String], targetDescription: String) -> MemoryJSON {
        .object([
            ("type", .string("object")),
            ("properties", .object([
                ("target", .object([
                    ("type", .string("string")), ("enum", targets.json), ("description", .string(targetDescription)),
                ])),
                ("operations", .object([
                    ("type", .string("array")),
                    ("description", .string("Changes applied atomically in one call against the final char budget; "
                        + "a single change is a one-item list.")),
                    ("items", .object([
                        ("type", .string("object")),
                        ("properties", .object([
                            ("action", .object([("type", .string("string")), ("enum", ["add", "patch", "remove"].json)])),
                            ("content", .object([
                                ("type", .string("string")),
                                ("description", .string("Required for 'add' and 'patch'. For 'add', the new entry. For 'patch', "
                                    + "the text that replaces old_text; the rest of the entry is kept.")),
                            ])),
                            ("old_text", .object([
                                ("type", .string("string")),
                                ("description", .string("REQUIRED for 'patch' and 'remove': text from one entry. 'patch' replaces "
                                    + "exactly this span (it must occur once in the entry); 'remove' deletes the "
                                    + "whole entry. Omit for 'add'.")),
                            ])),
                        ])),
                        ("required", ["action"].json),
                        ("additionalProperties", .bool(false)),
                    ])),
                ])),
            ])),
            ("required", ["target", "operations"].json),
        ])
    }

    /// Description and parameters narrowed to the enabled stores, as Hermes advertises them.
    public static func schema(memoryEnabled: Bool = true, userProfileEnabled: Bool = true) -> (description: String, parameters: MemoryJSON) {
        let targets = zip(["memory", "user"], [memoryEnabled, userProfileEnabled]).filter(\.1).map(\.0)
        let narrowed: (String, String)?
        switch targets {
        case ["memory"]:
            narrowed = ("The enabled built-in store: 'memory' for personal notes.",
                        "TARGET: only 'memory' is enabled for personal notes (environment, conventions, "
                            + "tool quirks, lessons).")
        case ["user"]:
            narrowed = ("The enabled built-in store: 'user' for user profile.",
                        "TARGET: only 'user' is enabled for user profile facts (name, role, preferences, style).")
        default: narrowed = nil
        }
        let targetDescription = narrowed?.0 ?? "Which memory store: 'memory' for personal notes, 'user' for user profile."
        let text = narrowed.map { description.replacingOccurrences(of: targetsText, with: $0.1) } ?? description
        return (text, parameters(targets: targets, targetDescription: targetDescription))
    }

    /// The system-prompt guidance Hermes adds when the memory tool is loaded; empty when both stores are off.
    public static func guidance(memoryEnabled: Bool = true, profileEnabled: Bool = true,
                                skillManageAvailable: Bool = true) -> String {
        if !memoryEnabled && !profileEnabled { return "" }
        let frame = memoryEnabled
            ? "You have persistent memory, carried across sessions and loaded "
                + "into each new session's context; the memory tool's schema defines what belongs there. "
            : "You have a persistent user profile, carried across sessions and "
                + "loaded into each new session's context; save durable facts about the user with the "
                + "memory tool (target='user') — the built-in notes store is disabled, so never target='memory'. "
        let skillRouting = skillManageAvailable
            ? "Skills come first: when you learn something while doing a task — a "
                + "procedure, a pitfall, and the user's preferences and corrections "
                + "for that kind of work — record it in the skill you used or built "
                + "for the task (skill_manage), where it loads only when relevant. "
            : "Task-specific knowledge — procedures, pitfalls, and the user's preferences "
                + "and corrections for that kind of work — belongs in skills, not in memory, "
                + "even when skill writing is unavailable. "
        return frame + skillRouting
            + "Memory is the narrow exception for facts that apply to EVERY "
            + "session regardless of task (who the user is, environment facts, "
            + "standing conventions with no task home); it has a hard character "
            + "budget, so when it fills, replace or consolidate stale entries "
            + "rather than skipping the save. Write entries as declarative facts, "
            + "not instructions to yourself: 'User prefers concise responses' ✓ — "
            + "'Always respond concisely' ✗ (imperative phrasing gets re-read as "
            + "a directive in later sessions and can override the user's current "
            + "request). A fact stale within a week belongs in session history; "
            + "procedures and workflows belong in skills."
    }

    static let replaceRemoved = "'replace' is not available; use 'patch'. old_text is the exact span to change and "
        + "content is its new text; the rest of the entry is kept. To rewrite a whole entry, "
        + "set old_text to the whole entry. Nothing was written."

    static let maxToolErrorChars = 2048

    public let store: MemoryStore?
    public var writeApproval: MemoryWriteApproval
    public let pending: MemoryPendingStore

    public init(store: MemoryStore?, writeApproval: MemoryWriteApproval = MemoryWriteApproval(),
                pending: MemoryPendingStore? = nil) {
        self.store = store
        self.writeApproval = writeApproval
        self.pending = pending ?? MemoryPendingStore(directory: (store?.folder ?? MemoryStore.defaultFolder)
            .appendingPathComponent("pending/memory", isDirectory: true))
    }

    // MARK: Tool entry point

    /// Handles a tool call whose arguments arrive as a JSON object string; returns the JSON result text.
    public func call(argumentsJSON: String, origin: MemoryWriteOrigin = .foreground) -> String {
        call(MemoryJSON.parse(argumentsJSON) ?? .object([]), origin: origin)
    }

    public func call(_ arguments: MemoryJSON, origin: MemoryWriteOrigin = .foreground) -> String {
        guard let store else {
            return Self.toolError("Memory is not available. It may be disabled in config or this environment.")
        }
        var action = ""
        if let value = arguments["action"], Self.truthy(value) { action = value.string ?? value.pythonDisplay }
        var content = arguments["content"]?.string
        let oldText = arguments["old_text"]?.string
        if (content ?? "").isEmpty, let newText = arguments["new_text"]?.string, !newText.isEmpty { content = newText }

        let rawOperations = arguments["operations"]
        let operations = rawOperations?.array.map { $0.map(MemoryOperation.init(json:)) }
        if let error = Self.targetError(store, arguments["target"], operations) { return error.json }
        let target = MemoryTarget(rawValue: arguments["target"]?.string ?? "") ?? .memory

        if let rawOperations, Self.truthy(rawOperations) {
            guard let operations else {
                return Self.toolError("operations must be a list of {action, content?, old_text?} objects.")
            }
            if operations.contains(where: { $0.action == "replace" }) { return Self.toolError(Self.replaceRemoved) }
            let payload = Self.batchPayload(target, operations)
            if let denied = backgroundDeleteGate(store, payload, origin: origin) { return denied }
            if let gated = writeGate(store, summary: "apply \(operations.count) op(s) to \(Self.label(target))",
                                     detail: operations.map(Self.opLine).joined(separator: "\n"), payload: payload, origin: origin) {
                return gated
            }
            return store.applyBatch(target, operations).json
        }
        if action.isEmpty {
            return Self.toolError("Missing required parameter: 'operations', a list of {action: add/patch/remove, "
                + "content?, old_text?} changes; a single change is a one-item list.")
        }
        if action == "replace" { return Self.toolError(Self.replaceRemoved) }
        guard ["add", "patch", "remove"].contains(action) else {
            return Self.toolError("Unknown action '\(action)'. Use: add, patch, remove")
        }
        if let error = validateSingleOp(store, action, target, content, oldText) { return error }
        let payload = Self.singlePayload(action, target, content, oldText)
        if let denied = backgroundDeleteGate(store, payload, origin: origin) { return denied }
        let (summary, detail) = Self.gateText(action, Self.label(target), content, oldText)
        if let gated = writeGate(store, summary: summary, detail: detail, payload: payload, origin: origin) { return gated }
        return Self.apply(store, action, target, content ?? "", oldText ?? "", nil).json
    }

    // MARK: Pending writes

    /// Applies a staged write and deletes the record when it succeeds.
    public func approvePending(_ id: String) -> MemoryResult {
        guard let record = pending.get(id) else {
            return MemoryResult([("success", .bool(false)), ("error", .string("No pending memory write with id '\(id)'."))])
        }
        guard let store else {
            return MemoryResult([("success", .bool(false)), ("error", .string("memory store unavailable"))])
        }
        let result = Self.applyPending(record.payload, store)
        if result.success { pending.discard(id) }
        return result
    }

    @discardableResult
    public func rejectPending(_ id: String) -> Bool { pending.discard(id) }

    /// Replays a staged payload past the gate. A destructive op applies only to its pinned entry.
    public static func applyPending(_ payload: MemoryJSON, _ store: MemoryStore) -> MemoryResult {
        let action = payload["action"]?.string
        let operations = payload["operations"]?.array.map { $0.map(MemoryOperation.init(pending:)) }
        if let error = targetError(store, payload["target"], operations) { return error }
        let target = MemoryTarget(rawValue: payload["target"]?.string ?? "") ?? .memory
        let destructive = action == "batch"
            ? (operations ?? []).filter { ["replace", "remove", "patch"].contains($0.action ?? "") }.map(\.matchedEntry)
            : ["replace", "remove", "patch"].contains(action ?? "") ? [payload["matched_entry"]?.string] : []
        if destructive.contains(where: { ($0 ?? "").isEmpty }) {
            return MemoryResult([("success", .bool(false)), ("error", .string(
                "This destructive pending write predates entry pinning and cannot be "
                    + "verified; nothing was applied. Reject it and recreate the change."))])
        }
        if action == "batch" { return store.applyBatch(target, operations ?? []) }
        guard let action, ["add", "replace", "remove", "patch"].contains(action) else {
            return MemoryResult([("success", .bool(false)), ("error", .string("Unknown staged action '\(action ?? "None")'."))])
        }
        return apply(store, action, target, payload["content"]?.string ?? "", payload["old_text"]?.string ?? "",
                     payload["matched_entry"]?.string)
    }

    // MARK: Gates

    private func validateSingleOp(_ store: MemoryStore, _ action: String, _ target: MemoryTarget,
                                  _ content: String?, _ oldText: String?) -> String? {
        if action == "add" && (content ?? "").isEmpty {
            return Self.toolError("Content is required for 'add' action.")
        }
        if (action == "remove" || action == "patch") && (oldText ?? "").isEmpty {
            return MemoryResult([
                ("success", .bool(false)),
                ("error", .string("'\(action)' needs old_text -- a short unique substring of the entry "
                    + "to \(action). None was provided. Reissue the \(action) with old_text "
                    + "set to part of one of the current_entries below.")),
                ("current_entries", store.entries(for: target).json), ("usage", .string(store.usage(target))),
            ]).json
        }
        if action == "patch" && (content ?? "").isEmpty {
            return Self.toolError("content (the new text for the old_text span) is required for 'patch' action. "
                + MemoryStore.patchDropHint)
        }
        return nil
    }

    /// Unattended background reviews may add, but patch/remove are staged for approval even with the gate off.
    private func backgroundDeleteGate(_ store: MemoryStore, _ payload: MemoryJSON, origin: MemoryWriteOrigin) -> String? {
        guard origin == .backgroundReview else { return nil }
        let ops = Self.payloadOperations(payload)
        guard ops.contains(where: { ["replace", "remove", "patch"].contains($0.action ?? "") }) else { return nil }
        let isBatch = payload["action"]?.string == "batch"
        let kind = isBatch ? "batch" : payload["action"]?.string ?? ""
        var pinned = payload
        if let unmatched = Self.pinMatchedEntries(store, &pinned) { return unmatched }
        do {
            let header = "background review consolidation (\(kind) on \(payload["target"]?.string ?? "memory"))"
            let summary = Self.stagedSummary(header, Self.pinnedOperations(pinned), limit: Self.limit(store, payload))
            let record = try pending.stage(pinned, summary: summary, origin: origin)
            return MemoryResult([
                ("success", .bool(true)), ("staged", .bool(true)), ("proposal_staged", .bool(true)),
                ("pending_id", .string(record.id)),
                ("message", .string("Background review may not delete memory entries unattended. The proposed "
                    + "\(kind) was staged for your approval — review it with /memory pending (approve to apply, "
                    + "discard to drop).")),
            ]).json
        } catch {
            return Self.toolError("Background review may not delete memory entries ('patch'/'remove', including in a "
                + "batch); 'add' is still available.")
        }
    }

    /// nil to proceed with the write; otherwise the denial or staged result to return.
    private func writeGate(_ store: MemoryStore, summary: String, detail: String, payload: MemoryJSON,
                           origin: MemoryWriteOrigin) -> String? {
        guard writeApproval.enabled else { return nil }
        if origin == .foreground, let prompt = writeApproval.prompt {
            let header = summary.pythonStripped.isEmpty ? "Save to memory?" : summary
            let shown = detail.pythonStripped
            switch prompt("Save to memory: \(header)", shown.isEmpty ? header : shown) {
            case true?: return nil
            case false?: return Self.toolError("Memory write denied by user. The change was not saved.")
            case nil: break
            }
        }
        var pinned = payload
        if let unmatched = Self.pinMatchedEntries(store, &pinned) { return unmatched }
        let message = "Staged for approval (memory.write_approval is on). Not yet saved — \(writeApproval.reviewHint)."
        let staged = Self.stagedSummary(summary, Self.pinnedOperations(pinned), limit: Self.limit(store, payload))
        guard let record = try? pending.stage(pinned, summary: staged, origin: origin) else {
            return Self.toolError("Failed to stage the memory write for approval. Nothing was saved.")
        }
        return MemoryResult([("success", .bool(true)), ("staged", .bool(true)), ("pending_id", .string(record.id)),
                             ("message", .string(message))]).json
    }

    /// Records on each destructive op the FULL entry it selects now, so approval applies to exactly that entry.
    private static func pinMatchedEntries(_ store: MemoryStore, _ payload: inout MemoryJSON) -> String? {
        let target = MemoryTarget(rawValue: payload["target"]?.string ?? "") ?? .memory
        let action = payload["action"]?.string ?? ""
        if action == "batch" {
            let ops = payloadOperations(payload)
            let result = store.resolveBatchEntries(target, ops)
            guard result.success else { return result.json }
            let entries = result["matched_entries"]?.array ?? []
            let pinned = zip(ops, entries).map { op, entry -> MemoryJSON in
                var op = op
                if let entry = entry.string { op.matchedEntry = entry }
                return op.json
            }
            payload = batchPayload(target, ops, pinned: pinned)
            return nil
        }
        guard ["replace", "remove", "patch"].contains(action) else { return nil }
        let result = store.resolveEntry(target, oldText: payload["old_text"]?.string ?? "", verb: action)
        guard result.success, case let .object(fields) = payload else { return result.json }
        payload = .object(fields + [("matched_entry", result["matched_entry"] ?? .null)])
        return nil
    }

    // MARK: Helpers

    static func targetError(_ store: MemoryStore, _ target: MemoryJSON?, _ operations: [MemoryOperation]?) -> MemoryResult? {
        switch target {
        case nil, .null?:
            return MemoryResult([("success", .bool(false)), ("error", .string(
                "Missing required target. Retry with target='memory' or target='user' at the top level. "
                    + "For a batch, remove target from every operation; use separate calls for different stores. "
                    + "Nothing was written or staged."))])
        case let value?:
            guard let name = value.string, MemoryTarget(rawValue: name) != nil else {
                return MemoryResult([("success", .bool(false)), ("error", .string(
                    boundErrorText("Invalid memory target '\(value.pythonDisplay)'. Use 'memory' or 'user'.")))])
            }
            return store.targetError(name, operations)
        }
    }

    private static func apply(_ store: MemoryStore, _ action: String, _ target: MemoryTarget, _ content: String,
                              _ oldText: String, _ matchedEntry: String?) -> MemoryResult {
        switch action {
        case "add": return store.add(target, content)
        case "replace": return store.replace(target, oldText: oldText, newContent: content, matchedEntry: matchedEntry)
        case "remove": return store.remove(target, oldText: oldText, matchedEntry: matchedEntry)
        default: return store.patch(target, oldText: oldText, newText: content, matchedEntry: matchedEntry)
        }
    }

    private static func gateText(_ action: String, _ label: String, _ content: String?, _ oldText: String?) -> (String, String) {
        let content = content ?? "", oldText = oldText ?? ""
        switch action {
        case "add": return ("add to \(label)", content)
        case "remove": return ("remove from \(label)", oldText)
        case "replace": return ("replace in \(label)", "entry matching: \(oldText)\nwhole entry becomes: \(content)")
        default:
            return ("patch in \(label)",
                    "entry matching: \(oldText)\nspan '\(oldText)' becomes: \(content) (rest of the entry is kept)")
        }
    }

    private static func opLine(_ op: MemoryOperation) -> String {
        switch op.action {
        case "remove": return "- remove: \(op.oldText ?? "")"
        case "patch": return "- patch span '\(op.oldText ?? "")' -> '\(op.effectiveContent)' (rest of the entry is kept)"
        default: return "- \(op.action ?? "?"): \(op.effectiveContent)"
        }
    }

    /// A pending record's summary: every operation, the full entry each remove, patch, or replace was pinned to, and
    /// the full new text. Model text is cut only past `limit`, the store's size, since longer text can't be applied.
    static func stagedSummary(_ header: String, _ pinnedOps: [MemoryOperation], limit: Int) -> String {
        func text(_ value: String?) -> String { "'\(MemoryText.echo(value ?? "", limit: limit))'" }
        func entry(_ op: MemoryOperation) -> String { op.matchedEntry.map { " in entry '\($0)'" } ?? "" }
        let lines = pinnedOps.map { op -> String in
            switch op.action {
            case "remove":
                return "remove \(text(op.oldText))" + (op.matchedEntry.map { " (deletes the whole entry '\($0)')" } ?? "")
            case "patch": return "patch \(text(op.oldText)) -> \(text(op.effectiveContent))\(entry(op))"
            case "replace": return "replace \(text(op.oldText)) -> \(text(op.effectiveContent))\(entry(op))"
            default: return "\(op.action ?? "?") \(text(op.effectiveContent))"
            }
        }
        return "\(header): " + lines.joined(separator: "; ")
    }

    /// The operations of a payload after pinning, with each `matched_entry` the store recorded.
    private static func pinnedOperations(_ payload: MemoryJSON) -> [MemoryOperation] {
        if payload["action"]?.string == "batch" {
            return (payload["operations"]?.array ?? []).map(MemoryOperation.init(pending:))
        }
        return [MemoryOperation(pending: payload)]
    }

    private static func label(_ target: MemoryTarget) -> String { target == .user ? "user profile" : "memory" }

    private static func limit(_ store: MemoryStore, _ payload: MemoryJSON) -> Int {
        store.charLimit(for: MemoryTarget(rawValue: payload["target"]?.string ?? "") ?? .memory)
    }

    private static func singlePayload(_ action: String, _ target: MemoryTarget, _ content: String?, _ oldText: String?) -> MemoryJSON {
        .object([("action", .string(action)), ("target", .string(target.rawValue)),
                 ("content", content.map(MemoryJSON.string) ?? .null), ("old_text", oldText.map(MemoryJSON.string) ?? .null)])
    }

    private static func batchPayload(_ target: MemoryTarget, _ ops: [MemoryOperation], pinned: [MemoryJSON]? = nil) -> MemoryJSON {
        .object([("action", .string("batch")), ("target", .string(target.rawValue)),
                 ("operations", .array(pinned ?? ops.map(\.json)))])
    }

    private static func payloadOperations(_ payload: MemoryJSON) -> [MemoryOperation] {
        if payload["action"]?.string == "batch" {
            return (payload["operations"]?.array ?? []).map(MemoryOperation.init(json:))
        }
        return [MemoryOperation(action: payload["action"]?.string, content: payload["content"]?.string,
                                oldText: payload["old_text"]?.string)]
    }

    /// Python truthiness for the `operations` argument: an empty list or empty string falls through to `action`.
    private static func truthy(_ value: MemoryJSON) -> Bool {
        switch value {
        case .null: return false
        case let .bool(b): return b
        case let .int(i): return i != 0
        case let .double(d): return d != 0
        case let .string(s): return !s.isEmpty
        case let .array(a): return !a.isEmpty
        case let .object(o): return !o.isEmpty
        }
    }


    static func boundErrorText(_ text: String) -> String {
        text.scalarCount <= maxToolErrorChars ? text : text.scalarPrefix(maxToolErrorChars) + "… [truncated]"
    }

    /// Hermes's `tool_error(message, success=False)`.
    static func toolError(_ message: String) -> String {
        MemoryResult([("error", .string(boundErrorText(message))), ("success", .bool(false))]).json
    }
}

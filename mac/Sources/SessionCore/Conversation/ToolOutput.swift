// Ported from Hermes Agent (MIT) agent/context_compressor.py (tool-result summaries and prune passes) and agent/context_compressor_tail.py (lean tail demotion).
import Foundation

/// Tool-result stubs and the deterministic prune passes that run before any summary call.
enum ToolOutput {
    static let pruneMinChars = 200
    static let maxTailMessageFloor = 8
    static let pressureKeepRecentMessages = 3
    static let leanTailKeepToolRounds = 6
    static let leanTailDemoteMinChars = 1_500

    struct CallInfo {
        let name: String
        let arguments: String
    }

    static func callsByID(_ messages: [ConversationMessage]) -> [String: CallInfo] {
        var map: [String: CallInfo] = [:]
        for message in messages where message.isToolCall {
            map[message.toolCallID ?? ""] = CallInfo(name: message.toolName ?? "unknown", arguments: message.content)
        }
        return map
    }

    // MARK: One-line summaries

    /// A `[tool] ... (N chars)` summary, or a lean-tail demotion stub.
    static func isSummaryStub(_ content: String) -> Bool {
        (content.hasPrefix("[") && content.contains(" chars)") && content.scalarCount < 400) || isDemotedStub(content)
    }

    private static let demotedStubPattern = Regex.compile(#"^\[[^\s\]]* output demoted at compaction — [\d,]+ chars preserved in session history\."#)

    static func isDemotedStub(_ content: String) -> Bool {
        content.scalarCount < 1_000 && Regex.matches(demotedStubPattern, content)
    }

    /// The recovery stub that replaces a demoted tail tool result.
    static func leanRecoveryStub(toolName: String, contentLength: Int, sessionID: String) -> String {
        let hint = sessionID.isEmpty ? "" : " Recover with session_search(query=..., session_id='\(sessionID)')"
        let name = toolName.isEmpty ? "tool" : toolName
        return "[\(name) output demoted at compaction — \(CompressionText.grouped(contentLength)) chars preserved in session history.\(hint)]"
    }

    static func summarize(toolName: String, arguments: String, content: String, terminalTools: Set<String>) -> String {
        let args = jsonObject(arguments)
        if let refused = summarizeRefused(toolName: toolName, args: args, rawArguments: arguments, content: content) {
            return refused
        }
        let contentLength = content.scalarCount
        let lineCount = content.trimmed.isEmpty ? 0 : content.newlineCount + 1
        if terminalTools.contains(toolName) {
            var command = stringArg(args, "command") ?? (args == nil ? arguments.trimmed : "")
            if command.scalarCount > 80 { command = command.scalarPrefix(77) + "..." }
            let exitCode = Regex.firstGroup(exitCodePattern, content) ?? "?"
            return "[\(toolName)] ran `\(command)` -> exit \(exitCode), \(lineCount) lines output"
        }
        let firstArgs = orderedKeys(arguments).prefix(2).compactMap { key in
            args?[key].map { " \(key)=\(pythonString($0).scalarPrefix(40))" }
        }.joined()
        return "[\(toolName)]\(firstArgs) (\(CompressionText.grouped(contentLength)) chars result)" + failureSuffix(content)
    }

    private static let exitCodePattern = Regex.compile(#""exit_code"\s*:\s*(-?\d+)"#)

    /// A call an approval guard refused stays a refusal, never a record of the action.
    private static func summarizeRefused(toolName: String, args: [String: Any]?, rawArguments: String,
                                         content: String) -> String? {
        let payload = jsonObject(content) ?? [:]
        let status = payload["status"] as? String
        var error = payload["error"] as? String ?? ""
        if error.isEmpty && content.trimmingLeadingWhitespace().hasPrefix("BLOCKED") {
            error = content.trimmed
        }
        var outcome: String
        if status == "pending_approval" {
            outcome = "awaiting the user's approval, not run"
        } else if !error.isEmpty && (status == "blocked" || error.trimmingLeadingWhitespace().hasPrefix("BLOCKED")) {
            outcome = "BLOCKED, not run"
            if error.contains("NOT consented") {
                outcome += "; the user did NOT consent, do not retry or reach the same outcome another way"
            }
        } else {
            return nil
        }
        var target = stringArg(args, "command") ?? stringArg(args, "path") ?? (args == nil ? rawArguments.trimmed : "")
        if !target.isEmpty {
            target = " `\(target.scalarCount <= 60 ? target : target.scalarPrefix(57) + "...")`"
        }
        return "[\(toolName)]\(target) \(outcome)".scalarPrefix(pruneMinChars - 1)
    }

    static func failureSuffix(_ content: String) -> String {
        let payload = jsonObject(content) ?? [:]
        let error = payload["error"].flatMap { $0 is NSNull ? nil : $0 }
        let errorText = error.map { String(describing: $0) } ?? ""
        let hasError = !(error == nil || errorText.isEmpty || (error as? Bool) == false)
        if !hasError && (payload["success"] as? Bool) != false { return "" }
        let preview = hasError ? errorText.collapsedWhitespace.scalarPrefix(80) : ""
        return preview.isEmpty ? " FAILED" : " FAILED: \(preview)"
    }

    private static func stringArg(_ args: [String: Any]?, _ key: String) -> String? {
        guard let value = args?[key], !(value is NSNull) else { return nil }
        return value as? String ?? String(describing: value)
    }

    /// Top-level keys of a JSON object in source order (Python dicts keep insertion order; Foundation's do not).
    static func orderedKeys(_ json: String) -> [String] {
        var keys: [String] = []
        var depth = 0
        var expectKey = false
        var inString = false
        var escaped = false
        var current = ""
        for scalar in json.unicodeScalars {
            if inString {
                if escaped {
                    escaped = false
                } else if scalar == "\\" {
                    escaped = true
                } else if scalar == "\"" {
                    inString = false
                    if depth == 1 && expectKey {
                        keys.append(decodeJSONString(current))
                        expectKey = false
                    }
                    continue
                }
                current.unicodeScalars.append(scalar)
                continue
            }
            switch scalar {
            case "\"":
                inString = true
                current = ""
            case "{", "[":
                depth += 1
                if depth == 1 { expectKey = scalar == "{" }
            case "}", "]":
                depth -= 1
            case ",":
                if depth == 1 { expectKey = true }
            default:
                break
            }
        }
        return keys
    }

    private static func decodeJSONString(_ body: String) -> String {
        guard let data = "\"\(body)\"".data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) as? String else { return body }
        return value
    }

    /// Python's `str(value)` for parsed JSON scalars.
    private static func pythonString(_ value: Any) -> String {
        if let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() { return number.boolValue ? "True" : "False" }
        if value is NSNull { return "None" }
        return value as? String ?? String(describing: value)
    }

    static func jsonObject(_ text: String) -> [String: Any]? {
        guard !text.isEmpty, let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) else { return nil }
        return object as? [String: Any]
    }

    // MARK: Prune passes

    /// Pass 1: keep the newest copy of identical tool results anywhere in the list; older copies back-reference it.
    static func dedupe(_ result: inout [ConversationMessage]) -> Int {
        var pruned = 0
        var seen: Set<UInt64> = []
        for index in result.indices.reversed() {
            let message = result[index]
            guard message.role == .tool, message.content.scalarCount >= pruneMinChars else { continue }
            let hash = FNV1a.hash(message.content)
            if seen.contains(hash) {
                result[index] = message.withContent(CompressionText.duplicateToolOutput)
                pruned += 1
            }
            seen.insert(hash)
        }
        return pruned
    }

    /// Replace the tool result at `index` with its one-line summary; true when it changed.
    static func demote(_ result: inout [ConversationMessage], at index: Int, calls: [String: CallInfo],
                       minPruneChars: Int, terminalTools: Set<String>) -> Bool {
        let message = result[index]
        guard message.role == .tool else { return false }
        let content = message.content
        if content.isEmpty || content == CompressionText.prunedToolPlaceholder || content.hasPrefix("[Duplicate tool output")
            || isSummaryStub(content) || content.scalarCount <= minPruneChars {
            return false
        }
        let call = calls[message.toolCallID ?? ""] ?? CallInfo(name: "unknown", arguments: "")
        result[index] = message.withContent(summarize(toolName: call.name, arguments: call.arguments, content: content,
                                                      terminalTools: terminalTools))
        return true
    }

    /// Lean mode: tail tool results older than the newest six rounds become recovery stubs.
    static func demoteStaleTailTools(_ messages: [ConversationMessage], tailStart: Int, sessionID: String) -> ([ConversationMessage], Int) {
        var protected: Set<Int> = []
        var roundsSeen = 0
        var previous: Int?
        let lower = max(0, tailStart)
        if lower < messages.count {
            for index in (lower..<messages.count).reversed() where messages[index].role == .tool {
                if previous == nil || previous! - index > 1 { roundsSeen += 1 }
                previous = index
                if roundsSeen > leanTailKeepToolRounds { break }
                protected.insert(index)
            }
        }
        var result = messages
        var demoted = 0
        for index in lower..<messages.count {
            let message = messages[index]
            guard message.role == .tool, !protected.contains(index) else { continue }
            if message.content.scalarCount < leanTailDemoteMinChars || isSummaryStub(message.content) { continue }
            result[index] = message.withContent(leanRecoveryStub(toolName: message.toolName ?? "",
                                                                 contentLength: message.content.scalarCount, sessionID: sessionID))
            demoted += 1
        }
        return (result, demoted)
    }
}

/// Thin NSRegularExpression wrappers; every pattern is compiled once.
enum Regex {
    static func compile(_ pattern: String, options: NSRegularExpression.Options = []) -> NSRegularExpression {
        // Patterns are literals in this module; a bad one is a programming error.
        try! NSRegularExpression(pattern: pattern, options: options)
    }

    static func matches(_ regex: NSRegularExpression, _ text: String) -> Bool {
        regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
    }

    static func firstGroup(_ regex: NSRegularExpression, _ text: String, group: Int = 1) -> String? {
        guard let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let range = Range(match.range(at: group), in: text) else { return nil }
        return String(text[range])
    }

    static func allMatches(_ regex: NSRegularExpression, _ text: String) -> [String] {
        regex.matches(in: text, range: NSRange(text.startIndex..., in: text)).compactMap {
            Range($0.range, in: text).map { String(text[$0]) }
        }
    }

    /// Replace every match with the closure's output; the closure gets the match's capture groups (index 0 is the whole match).
    static func replace(_ regex: NSRegularExpression, in text: String, with transform: ([String?]) -> String) -> String {
        let matches = regex.matches(in: text, range: NSRange(text.startIndex..., in: text))
        guard !matches.isEmpty else { return text }
        var out = ""
        var cursor = text.startIndex
        for match in matches {
            guard let whole = Range(match.range, in: text) else { continue }
            out += text[cursor..<whole.lowerBound]
            let groups = (0..<match.numberOfRanges).map { index -> String? in
                Range(match.range(at: index), in: text).map { String(text[$0]) }
            }
            out += transform(groups)
            cursor = whole.upperBound
        }
        out += text[cursor...]
        return out
    }
}

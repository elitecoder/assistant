// Ported from Hermes Agent (MIT) tools/memory_tool.py and tools/registry.py (tool_error, json.dumps result shape).
import Foundation

/// A JSON value that keeps object keys in insertion order, so tool results read like Hermes's `json.dumps` output.
public indirect enum MemoryJSON: Equatable, Sendable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([MemoryJSON])
    case object([(String, MemoryJSON)])

    public static func == (lhs: MemoryJSON, rhs: MemoryJSON) -> Bool {
        switch (lhs, rhs) {
        case (.null, .null): return true
        case let (.bool(a), .bool(b)): return a == b
        case let (.int(a), .int(b)): return a == b
        case let (.double(a), .double(b)): return a == b
        case let (.string(a), .string(b)): return a == b
        case let (.array(a), .array(b)): return a == b
        case let (.object(a), .object(b)):
            return a.count == b.count && zip(a, b).allSatisfy { $0.0 == $1.0 && $0.1 == $1.1 }
        default: return false
        }
    }

    public subscript(key: String) -> MemoryJSON? {
        guard case let .object(fields) = self else { return nil }
        return fields.first { $0.0 == key }?.1
    }

    public var string: String? {
        if case let .string(s) = self { return s }
        return nil
    }

    public var bool: Bool? {
        if case let .bool(b) = self { return b }
        return nil
    }

    public var int: Int? {
        if case let .int(i) = self { return i }
        return nil
    }

    public var number: Double? {
        switch self {
        case let .int(i): return Double(i)
        case let .double(d): return d
        default: return nil
        }
    }

    public var array: [MemoryJSON]? {
        if case let .array(a) = self { return a }
        return nil
    }

    public var strings: [String]? { array?.compactMap(\.string) }

    /// Python's `json.dumps(value, ensure_ascii=False)`: `", "` and `": "` separators, non-ASCII kept as is.
    public var encoded: String {
        switch self {
        case .null: return "null"
        case let .bool(b): return b ? "true" : "false"
        case let .int(i): return String(i)
        case let .double(d): return String(d)
        case let .string(s): return Self.quote(s)
        case let .array(items): return "[" + items.map(\.encoded).joined(separator: ", ") + "]"
        case let .object(fields):
            return "{" + fields.map { Self.quote($0.0) + ": " + $0.1.encoded }.joined(separator: ", ") + "}"
        }
    }

    static func quote(_ s: String) -> String {
        var out = "\""
        for scalar in s.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            case "\u{08}": out += "\\b"
            case "\u{0C}": out += "\\f"
            case let c where c.value < 0x20: out += String(format: "\\u%04x", c.value)
            default: out.unicodeScalars.append(scalar)
            }
        }
        return out + "\""
    }

    /// Converts a `JSONSerialization` value. Object key order is not preserved (Foundation drops it).
    public init(foundation value: Any?) {
        switch value {
        case nil, is NSNull: self = .null
        case let n as NSNumber where CFGetTypeID(n) == CFBooleanGetTypeID(): self = .bool(n.boolValue)
        case let n as NSNumber where Double(n.intValue) == n.doubleValue: self = .int(n.intValue)
        case let n as NSNumber: self = .double(n.doubleValue)
        case let s as String: self = .string(s)
        case let a as [Any]: self = .array(a.map { MemoryJSON(foundation: $0) })
        case let d as [String: Any]: self = .object(d.keys.sorted().map { ($0, MemoryJSON(foundation: d[$0])) })
        default: self = .string(String(describing: value!))
        }
    }

    public static func parse(_ text: String) -> MemoryJSON? {
        guard let data = text.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) else { return nil }
        return MemoryJSON(foundation: value)
    }

    /// The text Python's f-string `'{value}'` shows for a non-string argument.
    var pythonDisplay: String {
        switch self {
        case .null: return "None"
        case let .bool(b): return b ? "True" : "False"
        case let .int(i): return String(i)
        case let .double(d): return String(d)
        case let .string(s): return s
        default: return encoded
        }
    }
}

/// A tool or store result: an ordered JSON object such as `{"success": true, "done": true, ...}`.
public struct MemoryResult: Equatable, Sendable {
    public var fields: [(String, MemoryJSON)]

    public init(_ fields: [(String, MemoryJSON)]) { self.fields = fields }

    public static func == (lhs: MemoryResult, rhs: MemoryResult) -> Bool {
        MemoryJSON.object(lhs.fields) == MemoryJSON.object(rhs.fields)
    }

    public subscript(key: String) -> MemoryJSON? { fields.first { $0.0 == key }?.1 }

    public var success: Bool { self["success"]?.bool ?? false }
    public var error: String? { self["error"]?.string }
    public var message: String? { self["message"]?.string }
    public var json: String { MemoryJSON.object(fields).encoded }
}

extension Array where Element == String {
    var json: MemoryJSON { .array(map { .string($0) }) }
}

/// Python `len()` counts code points; Swift `count` counts grapheme clusters.
extension StringProtocol {
    var scalarCount: Int { unicodeScalars.count }

    func scalarPrefix(_ n: Int) -> String {
        var out = String.UnicodeScalarView()
        out.append(contentsOf: unicodeScalars.prefix(Swift.max(0, n)))
        return String(out)
    }
}

/// Python's `f"{n:,}"`: comma thousands separators regardless of locale.
func groupedNumber(_ n: Int) -> String {
    let digits = String(abs(n))
    var out = ""
    for (i, ch) in digits.enumerated() {
        if i > 0 && (digits.count - i) % 3 == 0 { out.append(",") }
        out.append(ch)
    }
    return (n < 0 ? "-" : "") + out
}

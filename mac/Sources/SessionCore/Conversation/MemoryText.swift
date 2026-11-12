// Ported from Hermes Agent (MIT) tools/memory_tool_store.py: Python str semantics (code points, strip, split, in, ==).
import Foundation

/// Text operations on Unicode scalars, matching Python `str`. Swift's `String` compares and splits by grapheme
/// cluster with canonical equivalence, so "\r\n" is one Character and "é" == "e\u{301}".
enum MemoryText {
    /// Python `str.isspace()`: Swift's whitespace set plus the FS/GS/RS/US separators U+001C–U+001F.
    static let pythonWhitespace: CharacterSet = CharacterSet.whitespacesAndNewlines
        .union(CharacterSet(charactersIn: "\u{0B}\u{0C}\u{1C}\u{1D}\u{1E}\u{1F}\u{85}"))

    static func isSpace(_ scalar: Unicode.Scalar) -> Bool { pythonWhitespace.contains(scalar) }

    /// Python text-mode reading: "\r\n" and a lone "\r" become "\n".
    static func normalizeNewlines(_ text: String) -> String {
        var out = String.UnicodeScalarView()
        var previousCR = false
        for scalar in text.unicodeScalars {
            if scalar == "\r" {
                out.append("\n")
                previousCR = true
                continue
            }
            if !(scalar == "\n" && previousCR) { out.append(scalar) }
            previousCR = false
        }
        return String(out)
    }

    static func split(_ text: String, on separator: String) -> [String] {
        let hay = Array(text.unicodeScalars), sep = Array(separator.unicodeScalars)
        var parts: [String] = [], start = 0, i = 0
        while i + sep.count <= hay.count {
            if hay[i..<(i + sep.count)].elementsEqual(sep) {
                parts.append(string(hay[start..<i]))
                i += sep.count
                start = i
            } else {
                i += 1
            }
        }
        parts.append(string(hay[start...]))
        return parts
    }

    static func firstRange(of needle: String, in text: String) -> Range<Int>? {
        let hay = Array(text.unicodeScalars), sub = Array(needle.unicodeScalars)
        if sub.isEmpty { return 0..<0 }
        guard hay.count >= sub.count else { return nil }
        for i in 0...(hay.count - sub.count) where hay[i..<(i + sub.count)].elementsEqual(sub) {
            return i..<(i + sub.count)
        }
        return nil
    }

    /// Python `needle in text`.
    static func contains(_ text: String, _ needle: String) -> Bool { firstRange(of: needle, in: text) != nil }

    /// Python `text.replace(old, new, 1)`.
    static func replaceFirst(_ text: String, _ old: String, _ new: String) -> String {
        guard let range = firstRange(of: old, in: text) else { return text }
        let hay = Array(text.unicodeScalars)
        return string(hay[..<range.lowerBound]) + new + string(hay[range.upperBound...])
    }

    /// Long model-supplied text is shortened before it is echoed back into a result.
    static func echo(_ text: String, limit: Int = 200) -> String {
        text.scalarCount <= limit ? text : text.scalarPrefix(limit) + "… [truncated]"
    }

    static func string<S: Sequence>(_ scalars: S) -> String where S.Element == Unicode.Scalar {
        var view = String.UnicodeScalarView()
        view.append(contentsOf: scalars)
        return String(view)
    }
}

extension String {
    /// Python `str.strip()`.
    var pythonStripped: String {
        let scalars = Array(unicodeScalars)
        guard let first = scalars.firstIndex(where: { !MemoryText.isSpace($0) }),
              let last = scalars.lastIndex(where: { !MemoryText.isSpace($0) }) else { return "" }
        return MemoryText.string(scalars[first...last])
    }

    /// Python `==`: same code points, no canonical equivalence.
    func isExactly(_ other: String) -> Bool { utf8.elementsEqual(other.utf8) }

    var exactKey: [UInt8] { Array(utf8) }
}

extension Array where Element == String {
    func containsExactly(_ text: String) -> Bool { contains { $0.isExactly(text) } }

    func firstExactIndex(of text: String) -> Int? { firstIndex { $0.isExactly(text) } }

    func isExactly(_ other: [String]) -> Bool { count == other.count && zip(self, other).allSatisfy { $0.isExactly($1) } }
}

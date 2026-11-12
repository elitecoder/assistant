import CryptoKit
import Foundation
import NaturalLanguage

extension String {
    func ifEmpty(_ fallback: String) -> String { isEmpty ? fallback : self }
}

public struct KokoroFile: Equatable {
    public let name: String
    public let url: URL
    public let sha256: String
}

public enum Kokoro {
    public static let files = [
        KokoroFile(name: "voices.npz",
                   url: URL(string: "https://github.com/mlalma/KokoroTestApp/raw/9dcd3b06468a3c1ecee6d09a33ca687c8e708566/Resources/voices.npz")!,
                   sha256: "56dbfa2f2970af2e395397020393d368c5f441d09b3de4e9b77f6222e790f10f"),
        KokoroFile(name: "kokoro-v1_0.safetensors",
                   url: URL(string: "https://github.com/mlalma/KokoroTestApp/raw/9dcd3b06468a3c1ecee6d09a33ca687c8e708566/Resources/kokoro-v1_0.safetensors")!,
                   sha256: "4e9ecdf03b8b6cf906070390237feda473dc13327cb8d56a43deaa374c02acd8"),
    ]

    public static var voicePack: KokoroFile { files[0] }
    public static var model: KokoroFile { files[1] }

    /// Built-in Kokoro voices: key in voices.npz, display name, language. Heart is the top-rated voice.
    public static let voices: [VoiceInfo] = [
        ("af_heart", "Kokoro Heart", "en-US", 5),
        ("af_bella", "Kokoro Bella", "en-US", 4),
        ("am_michael", "Kokoro Michael", "en-US", 4),
        ("bf_emma", "Kokoro Emma", "en-GB", 4),
        ("bm_george", "Kokoro George", "en-GB", 4),
    ].map { VoiceInfo(id: idPrefix + $0.0, name: $0.1, language: $0.2, quality: $0.3, novelty: false) }

    public static let idPrefix = "kokoro."

    public static func npzKey(_ voiceID: String) -> String? {
        voiceID.hasPrefix(idPrefix) ? String(voiceID.dropFirst(idPrefix.count)) + ".npy" : nil
    }

    public static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        while let chunk = try handle.read(upToCount: 4 << 20), !chunk.isEmpty { hash.update(data: chunk) }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static let speller: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .spellOut
        f.locale = Locale(identifier: "en_US")
        return f
    }()

    static func spell(_ digits: String) -> String {
        guard let n = Int(digits) else { return digits }
        return (speller.string(from: NSNumber(value: n)) ?? digits).replacingOccurrences(of: "-", with: " ")
    }

    static func replace(_ text: String, _ pattern: String, _ transform: ([String]) -> String) -> String {
        let regex = try! NSRegularExpression(pattern: pattern)
        var out = text
        for m in regex.matches(in: text, range: NSRange(text.startIndex..., in: text)).reversed() {
            let groups = (0..<m.numberOfRanges).map { i in
                Range(m.range(at: i), in: text).map { String(text[$0]) } ?? ""
            }
            out.replaceSubrange(Range(m.range, in: out)!, with: transform(groups))
        }
        return out
    }

    /// Writes numbers and symbols as words. Kokoro garbles or drops digits, %, $, and version numbers.
    public static func wordsForSpeech(_ text: String) -> String {
        var s = text.replacingOccurrences(of: #"(\d),(\d{3})"#, with: "$1$2", options: .regularExpression)
        s = replace(s, #"\$(\d+)\.(\d{2})\b"#) { g in
            let dollars = Int(g[1]) ?? 0, cents = Int(g[2]) ?? 0
            let d = dollars == 0 ? "" : spell(g[1]) + (dollars == 1 ? " dollar" : " dollars")
            let c = cents == 0 ? "" : spell(String(cents)) + (cents == 1 ? " cent" : " cents")
            return [d, c].filter { !$0.isEmpty }.joined(separator: " and ").ifEmpty("zero dollars")
        }
        s = replace(s, #"\$(\d+)"#) { g in spell(g[1]) + (g[1] == "1" ? " dollar" : " dollars") }
        s = replace(s, #"\b(\d+(?:\.\d+){2,})\b"#) { g in
            g[1].split(separator: ".").map { spell(String($0)) }.joined(separator: " point ")
        }
        s = replace(s, #"\b(\d+)\.(\d+)\b"#) { g in
            spell(g[1]) + " point " + g[2].map { spell(String($0)) }.joined(separator: " ")
        }
        s = replace(s, #"(\d+)\s?%"#) { g in spell(g[1]) + " percent" }
        s = replace(s, #"#(\d+)"#) { g in "number " + spell(g[1]) }
        s = replace(s, #"\b(\d+)\b"#) { g in spell(g[1]) }
        return s.replacingOccurrences(of: "&", with: " and ")
            .replacingOccurrences(of: #"[/\\|<>{}\[\]~^=+]"#, with: " ", options: .regularExpression)
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }

    /// Pieces to synthesize one at a time: sentences, with long ones split at commas.
    /// Kokoro mumbles the start of long inputs, and speech starts as soon as the first piece is ready.
    public static func chunks(_ text: String, maxLength: Int = 90) -> [String] {
        sentences(wordsForSpeech(text)).flatMap { sentence -> [String] in
            guard sentence.count > maxLength else { return [sentence] }
            var pieces: [String] = []
            var current = ""
            for clause in sentence.split(separator: ",", omittingEmptySubsequences: true) {
                let part = clause.trimmingCharacters(in: .whitespaces)
                if current.isEmpty {
                    current = part
                } else if current.count + part.count + 2 <= maxLength || part.count < 15 {
                    current += ", " + part
                } else {
                    pieces.append(current + ",")
                    current = part
                }
            }
            if !current.isEmpty { pieces.append(current) }
            return pieces
        }
    }

    /// Sentences to synthesize one at a time, so speech starts after the first one is ready.
    public static func sentences(_ text: String) -> [String] {
        let tokenizer = NLTokenizer(unit: .sentence)
        tokenizer.string = text
        var result: [String] = []
        tokenizer.enumerateTokens(in: text.startIndex..<text.endIndex) { range, _ in
            let sentence = text[range].trimmingCharacters(in: .whitespacesAndNewlines)
            if !sentence.isEmpty { result.append(sentence) }
            return true
        }
        return result
    }

    /// Splits text the model rejects as too long into two halves at the clause or word nearest the middle.
    public static func halves(_ text: String) -> [String] {
        let chars = Array(text)
        guard chars.count > 1 else { return [text] }
        let middle = chars.count / 2
        func nearest(_ matches: (Character) -> Bool) -> Int? {
            for radius in 0..<middle {
                for i in [middle - radius, middle + radius] where i > 0 && i < chars.count - 1 && matches(chars[i]) {
                    return i + 1
                }
            }
            return nil
        }
        let cut = nearest { ",;:—".contains($0) } ?? nearest { $0 == " " } ?? middle
        return [String(chars[..<cut]), String(chars[cut...])]
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }
}

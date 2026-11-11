import CryptoKit
import Foundation
import NaturalLanguage

public struct KokoroFile: Equatable {
    public let name: String
    public let url: URL
    public let sha256: String
}

public enum Kokoro {
    public static let files = [
        KokoroFile(name: "voices.npz",
                   url: URL(string: "https://github.com/mlalma/KokoroTestApp/raw/main/Resources/voices.npz")!,
                   sha256: "56dbfa2f2970af2e395397020393d368c5f441d09b3de4e9b77f6222e790f10f"),
        KokoroFile(name: "kokoro-v1_0.safetensors",
                   url: URL(string: "https://github.com/mlalma/KokoroTestApp/raw/main/Resources/kokoro-v1_0.safetensors")!,
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

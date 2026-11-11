import Foundation

public struct VoiceInfo: Equatable, Identifiable {
    public let id: String
    public let name: String
    public let language: String
    public let quality: Int
    public let novelty: Bool

    public init(id: String, name: String, language: String, quality: Int, novelty: Bool) {
        self.id = id
        self.name = name
        self.language = language
        self.quality = quality
        self.novelty = novelty
    }

    public var label: String {
        switch quality {
        case 3: "\(name) (Premium)"
        case 2: "\(name) (Enhanced)"
        default: name
        }
    }
}

public enum Voices {
    /// English voices an app can speak with, best first. Siri voices are listed by macOS but refused to apps.
    /// Ties in quality go to the system's default voice, then to US English.
    public static func usable(_ all: [VoiceInfo], systemDefault: String? = nil) -> [VoiceInfo] {
        func rank(_ v: VoiceInfo) -> (Int, Int, Int) {
            (v.quality, v.id == systemDefault ? 1 : 0, v.language == "en-US" ? 1 : 0)
        }
        return all.filter { $0.language.hasPrefix("en") && !$0.novelty && !$0.id.contains(".siri.") }
            .sorted { rank($0) != rank($1) ? rank($0) > rank($1) : $0.name < $1.name }
    }

    /// The operator's pick when it's still installed, else the best usable voice.
    public static func choose(_ all: [VoiceInfo], preferred: String?, systemDefault: String? = nil) -> VoiceInfo? {
        let voices = usable(all, systemDefault: systemDefault)
        return voices.first { $0.id == preferred } ?? voices.first
    }
}

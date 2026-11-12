import Foundation

public struct Microphone: Equatable, Identifiable {
    public let id: UInt32
    public let name: String
    public let builtIn: Bool
    public let virtual: Bool

    public init(id: UInt32, name: String, builtIn: Bool, virtual: Bool) {
        self.id = id
        self.name = name
        self.builtIn = builtIn
        self.virtual = virtual
    }
}

public enum Microphones {
    /// The microphone to listen with: the operator's pick, else the system default unless it's the
    /// built-in microphone with the lid closed, else the first real external microphone.
    /// Microphones in `excluding` already sent pure silence and are skipped.
    public static func choose(_ mics: [Microphone], preferred: String?, defaultID: UInt32?,
                              lidClosed: Bool, excluding: Set<UInt32> = []) -> Microphone? {
        let candidates = mics.filter { !excluding.contains($0.id) }
        if let preferred, let pick = candidates.first(where: { $0.name == preferred }) { return pick }
        let usable = candidates.filter { !$0.virtual && !($0.builtIn && lidClosed) }
        return usable.first { $0.id == defaultID } ?? usable.first { !$0.builtIn } ?? usable.first
    }

    /// A microphone format AVAudioEngine can tap. Right after access is granted, macOS can briefly
    /// report a device with a zero sample rate or no channels.
    public static func usable(sampleRate: Double, channels: UInt32) -> Bool {
        sampleRate.isFinite && sampleRate > 0 && channels > 0
    }
}

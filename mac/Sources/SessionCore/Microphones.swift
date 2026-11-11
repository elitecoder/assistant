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
    public static func choose(_ mics: [Microphone], preferred: String?, defaultID: UInt32?,
                              lidClosed: Bool) -> Microphone? {
        if let preferred, let pick = mics.first(where: { $0.name == preferred }) { return pick }
        let usable = mics.filter { !$0.virtual && !($0.builtIn && lidClosed) }
        return usable.first { $0.id == defaultID } ?? usable.first { !$0.builtIn } ?? usable.first
    }
}

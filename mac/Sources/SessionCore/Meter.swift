import Foundation

public enum Meter {
    static let floorDB: Float = -55

    /// Loudness of one audio buffer on a 0–1 scale, from -55 dB (silent) to 0 dB (full scale).
    public static func level(_ samples: UnsafeBufferPointer<Float>) -> Float {
        guard !samples.isEmpty else { return 0 }
        let meanSquare = samples.reduce(Float(0)) { $0 + $1 * $1 } / Float(samples.count)
        guard meanSquare > 0 else { return 0 }
        let db = 10 * log10(meanSquare)
        return min(1, max(0, (db - floorDB) / -floorDB))
    }

    /// The level of the loudest channel, so a microphone that records on its second channel still shows.
    public static func loudest(_ channels: [UnsafeBufferPointer<Float>]) -> Float {
        channels.map(level).max() ?? 0
    }

    /// All channels averaged into one, so speech on any channel reaches the recognizer.
    public static func mono(_ channels: [UnsafeBufferPointer<Float>], into out: UnsafeMutableBufferPointer<Float>) {
        guard let first = channels.first else { return }
        let scale = 1 / Float(channels.count)
        for i in 0..<min(out.count, first.count) {
            var sum: Float = 0
            for channel in channels where i < channel.count { sum += channel[i] }
            out[i] = sum * scale
        }
    }

    /// The newest `count` levels, padded with silence at the front.
    public static func window(_ levels: [Float], adding level: Float, count: Int) -> [Float] {
        let all = levels + [level]
        let recent = Array(all.suffix(count))
        return Array(repeating: 0, count: count - recent.count) + recent
    }
}

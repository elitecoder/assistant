import Foundation

/// Talkback speed: a multiplier the operator picks, applied to Kokoro and to Apple's voices.
public enum SpeechSpeed {
    public static let choices: [Double] = [1.0, 1.25, 1.5, 1.75, 2.0]
    public static let defaultValue = 1.0

    public static func clamped(_ value: Double) -> Double {
        value.isFinite ? min(2.0, max(1.0, value)) : defaultValue
    }

    /// How much faster Apple's voices talk at the default rate plus an offset, measured from the length of audio
    /// AVSpeechSynthesizer renders (Samantha, macOS 27). The rate isn't linear: the default rate × 1.5 plays at
    /// about 2.5× and × 2 at about 4×.
    static let measured: [(offset: Float, speedup: Double)] = [
        (0, 1), (0.025, 1.123), (0.0435, 1.237), (0.05, 1.295), (0.075, 1.426), (0.086, 1.504), (0.1, 1.591),
        (0.125, 1.717), (0.15, 1.831), (0.165, 1.962), (0.182, 2.113),
    ]

    /// Apple's utterance rate that plays at `multiplier` times the default speed, capped at the maximum rate.
    public static func appleRate(_ multiplier: Double, defaultRate: Float, maximumRate: Float) -> Float {
        let target = clamped(multiplier)
        let upper = measured.firstIndex { $0.speedup >= target } ?? measured.count - 1
        let lower = max(0, upper - 1)
        let (a, b) = (measured[lower], measured[upper])
        let fraction = b.speedup > a.speedup ? Float((target - a.speedup) / (b.speedup - a.speedup)) : 0
        return min(maximumRate, defaultRate + a.offset + fraction * (b.offset - a.offset))
    }

    public static func label(_ value: Double) -> String {
        let text = String(format: "%.2f", clamped(value))
            .replacingOccurrences(of: #"\.?0+$"#, with: "", options: .regularExpression)
        return text + "×"
    }
}

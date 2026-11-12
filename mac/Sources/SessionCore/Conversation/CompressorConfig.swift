// Ported from Hermes Agent (MIT) agent/context_compressor.py, agent/context_compressor_tail.py and agent/context_engine.py.
import Foundation

public enum TailMode: String, Sendable {
    /// Small clamped verbatim tail; the summary carries continuity (verbatim user messages, anchor index, recovery footer).
    case lean
    /// Tail of `threshold × summaryTargetRatio` tokens and no lean extras.
    case legacy
}

/// Compression settings and the trigger math derived from them. Defaults match Hermes's `compression:` config.
public struct CompressorConfig: Sendable {
    public var model: String
    public var provider: String
    public var contextLength: Int
    /// Output reservation the provider carves out of the window; nil means none.
    public var maxTokens: Int?
    public var thresholdPercent: Double
    /// Per-model trigger overrides: substring of the model name (optionally `provider:substring`) to fraction.
    public var modelThresholds: [String: Double]
    /// Absolute trigger cap; compaction fires at the lower of the ratio trigger and this count.
    public var thresholdTokensCap: Int?
    public var protectFirstN: Int
    public var protectLastN: Int
    public var summaryTargetRatio: Double
    public var tailMode: TailMode
    public var minTailUserMessages: Int
    /// True aborts on any summary failure instead of inserting the deterministic fallback summary.
    public var abortOnSummaryFailure: Bool
    /// Proactive prune trigger in tokens; 0 disables it.
    public var proactivePruneTokens: Int
    public var proactivePruneMinResultChars: Int
    public var proactivePruneMinReclaimTokens: Int
    /// Tool names whose results summarize as shell commands (`[terminal] ran ...`).
    public var terminalToolNames: Set<String>

    public init(model: String = "", provider: String = "", contextLength: Int, maxTokens: Int? = nil,
                thresholdPercent: Double = 0.50, modelThresholds: [String: Double] = [:],
                thresholdTokensCap: Int? = nil, protectFirstN: Int = 3, protectLastN: Int = 20,
                summaryTargetRatio: Double = 0.20, tailMode: TailMode = .lean, minTailUserMessages: Int = 1,
                abortOnSummaryFailure: Bool = false, proactivePruneTokens: Int = 0,
                proactivePruneMinResultChars: Int = 8_000, proactivePruneMinReclaimTokens: Int = 4_096,
                terminalToolNames: Set<String> = ["terminal", "shell", "bash"]) {
        self.model = model
        self.provider = provider
        self.contextLength = contextLength
        self.maxTokens = (maxTokens ?? 0) > 0 ? maxTokens : nil
        self.thresholdPercent = thresholdPercent
        self.modelThresholds = modelThresholds
        self.thresholdTokensCap = (thresholdTokensCap ?? 0) > 0 ? thresholdTokensCap : nil
        self.protectFirstN = protectFirstN
        self.protectLastN = protectLastN
        self.summaryTargetRatio = max(0.10, min(summaryTargetRatio, 0.80))
        self.tailMode = tailMode
        self.minTailUserMessages = minTailUserMessages
        self.abortOnSummaryFailure = abortOnSummaryFailure
        self.proactivePruneTokens = max(0, proactivePruneTokens)
        self.proactivePruneMinResultChars = max(ToolOutput.pruneMinChars, proactivePruneMinResultChars == 0 ? 8_000 : proactivePruneMinResultChars)
        self.proactivePruneMinReclaimTokens = max(0, proactivePruneMinReclaimTokens)
        self.terminalToolNames = terminalToolNames
    }

    static let minimumContextLength = 64_000
    static let minContextTriggerRatio = 0.85
    static let smallContextWindowLimit = 512_000
    static let smallContextThresholdPercent = 0.75
    static let leanTailFloorTokens = 10_000
    static let leanTailCapTokens = 25_000
    static let tailMaxContextFraction = 0.20
    static let tailMaxThresholdFraction = 0.50
    static let minSummaryTokens = 2_000
    static let summaryRatio = 0.20
    static let summaryTokensCeiling = 10_000

    /// The settings with out-of-range values clamped (negative counts, non-finite fractions, an empty window).
    public var sanitized: CompressorConfig {
        var copy = self
        copy.contextLength = max(1, contextLength)
        copy.thresholdPercent = Self.validPercent(thresholdPercent) ?? 0.50
        copy.modelThresholds = modelThresholds.compactMapValues(Self.validPercent)
        copy.protectFirstN = max(0, protectFirstN)
        copy.protectLastN = max(0, protectLastN)
        copy.minTailUserMessages = max(1, minTailUserMessages)
        copy.summaryTargetRatio = summaryTargetRatio.isFinite ? max(0.10, min(summaryTargetRatio, 0.80)) : 0.20
        copy.proactivePruneTokens = max(0, proactivePruneTokens)
        copy.proactivePruneMinResultChars = max(ToolOutput.pruneMinChars, proactivePruneMinResultChars)
        copy.proactivePruneMinReclaimTokens = max(0, proactivePruneMinReclaimTokens)
        if let maxTokens, maxTokens <= 0 { copy.maxTokens = nil }
        if let cap = thresholdTokensCap, cap <= 0 { copy.thresholdTokensCap = nil }
        return copy
    }

    private static func validPercent(_ value: Double) -> Double? {
        value.isFinite && value > 0 ? min(value, 1.0) : nil
    }

    /// The model-specific trigger fraction before the small-window floor.
    public var basePercent: Double {
        Self.resolveModelThreshold(model: model, thresholds: modelThresholds.compactMapValues(Self.validPercent),
                                   default: Self.validPercent(thresholdPercent) ?? 0.50, provider: provider)
    }

    /// Windows under 512K trigger at no less than 75%: at 50% the incompressible floor eats the reclaimed room.
    public var effectivePercent: Double {
        Self.effectiveThresholdPercent(contextLength: contextLength, percent: basePercent)
    }

    public var thresholdTokens: Int {
        var threshold = Self.computeThresholdTokens(contextLength: max(1, contextLength), percent: effectivePercent, maxTokens: maxTokens)
        if let cap = thresholdTokensCap {
            threshold = min(threshold, min(cap, contextLength))
        }
        return max(1, threshold)
    }

    public var tailTokenBudget: Int {
        var budget = tailMode == .lean
            ? Self.leanTailBudget(contextLength: contextLength, thresholdTokens: thresholdTokens)
            : Int(Double(thresholdTokens) * summaryTargetRatio)
        if contextLength > 0 {
            budget = min(budget, Int(Double(contextLength) * Self.tailMaxContextFraction))
        }
        return max(1, budget)
    }

    public var maxSummaryTokens: Int {
        min(Int(Double(contextLength) * 0.05), Self.summaryTokensCeiling)
    }

    var effectiveInputWindow: Int {
        Self.effectiveInputWindow(contextLength: contextLength, maxTokens: maxTokens)
    }

    /// Longest matching key wins; a `provider:` scoped key beats a bare key with the same substring.
    public static func resolveModelThreshold(model: String, thresholds: [String: Double], default fallback: Double,
                                             provider: String = "") -> Double {
        guard !thresholds.isEmpty, !model.isEmpty else { return fallback }
        let provider = provider.trimmed.lowercased()
        var best: (length: Int, scoped: Int, key: String)?
        for key in thresholds.keys {
            let rank: (Int, Int)?
            if let colon = key.firstIndex(of: ":") {
                let scope = String(key[..<colon]).trimmed.lowercased()
                let substring = String(key[key.index(after: colon)...])
                rank = scope == provider && model.contains(substring) ? (substring.count, 1) : nil
            } else {
                rank = model.contains(key) ? (key.count, 0) : nil
            }
            guard let rank else { continue }
            if let current = best, (current.length, current.scoped, current.key) >= (rank.0, rank.1, key) { continue }
            best = (rank.0, rank.1, key)
        }
        return best.map { thresholds[$0.key]! } ?? fallback
    }

    static func effectiveThresholdPercent(contextLength: Int, percent: Double) -> Double {
        if contextLength > 0 && contextLength < smallContextWindowLimit {
            return max(percent, smallContextThresholdPercent)
        }
        return percent
    }

    static func effectiveInputWindow(contextLength: Int, maxTokens: Int?) -> Int {
        let window = contextLength - (maxTokens ?? 0)
        return window > 0 ? window : contextLength
    }

    /// `(window - maxTokens) × percent`, floored at 64K; when the floor binds it is held to 85% of the input window.
    static func computeThresholdTokens(contextLength: Int, percent: Double, maxTokens: Int?) -> Int {
        let window = effectiveInputWindow(contextLength: contextLength, maxTokens: maxTokens)
        let percentValue = Int(Double(window) * percent)
        var floored = max(percentValue, minimumContextLength)
        let triggerCap = Int(Double(window) * minContextTriggerRatio)
        if window > 0 && floored > percentValue && floored > triggerCap {
            floored = max(percentValue, triggerCap)
        }
        if window > 0 && floored >= window {
            return max(1, min(triggerCap, window - 1))
        }
        return floored
    }

    /// 2.5% of the window clamped to 10K–25K, then held to half the trigger.
    static func leanTailBudget(contextLength: Int, thresholdTokens: Int) -> Int {
        var floor = leanTailFloorTokens
        var windowTerm = min(leanTailCapTokens, Int(Double(contextLength) * 0.025))
        if thresholdTokens > 0 {
            let share = Int(Double(thresholdTokens) * tailMaxThresholdFraction)
            floor = min(floor, share)
            if thresholdTokens > minSummaryTokens {
                windowTerm = min(windowTerm, share)
            }
        }
        return max(floor, windowTerm)
    }
}

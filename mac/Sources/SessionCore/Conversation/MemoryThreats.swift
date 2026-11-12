// Ported from Hermes Agent (MIT) tools/threat_patterns.py (strict scope, the scope memory writes and loads use).
import Foundation

/// Injection, exfiltration, persistence, and secret patterns that block a memory entry from entering the prompt.
public enum MemoryThreats {
    public static let maxScanChars = 65_536

    private static let filler = #"(?:\w+\s+){0,8}"#
    private static let secretVar = #"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b"#
    private static let modify = #"(update|modify|edit|write|change|append|add\s+to)\s+[^\n]{0,2048}"#

    /// `(regex, pattern id)` in Hermes's order; the first hit names the rejection.
    public static let patterns: [(String, String)] = [
        (#"ignore\s+"# + filler + #"(previous|all|above|prior)\s+"# + filler + "instructions", "prompt_injection"),
        (#"system\s+prompt\s+override"#, "sys_prompt_override"),
        (#"disregard\s+"# + filler + #"(your|all|any)\s+"# + filler + "(instructions|rules|guidelines)", "disregard_rules"),
        (#"act\s+as\s+(if|though)\s+"# + filler + #"you\s+"# + filler + #"(have\s+no|don't\s+have)\s+"# + filler
            + "(restrictions|limits|rules)", "bypass_restrictions"),
        (#"<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->"#, "html_comment_injection"),
        (#"<\s*div\s+style\s*=\s*["'][^>]{0,2048}display\s*:\s*none"#, "hidden_div"),
        (#"translate\s+[^\n]{0,512}\s+into\s+\w+(?:[\s-]+\w+){0,2}\s+and\s+(execute|run|eval)\b"#, "translate_execute"),
        (#"do\s+not\s+"# + filler + #"tell\s+"# + filler + #"the\s+user"#, "deception_hide"),

        (#"you\s+are\s+"# + filler + #"now\s+(?:a|an|the)\s+"#, "role_hijack"),
        (#"pretend\s+"# + filler + #"(you\s+are|to\s+be)\s+"#, "role_pretend"),
        (#"output\s+"# + filler + #"(system|initial)\s+prompt"#, "leak_system_prompt"),
        (#"(respond|answer|reply)\s+without\s+"# + filler + "(restrictions|limitations|filters|safety)", "remove_filters"),
        (#"you\s+have\s+been\s+"# + filler + #"(updated|upgraded|patched)\s+to"#, "fake_update"),
        (#"\bname\s+yourself\s+\w+"#, "identity_override"),

        (#"register\s+(as\s+)?a?\s*node"#, "c2_node_registration"),
        (#"(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+"#, "c2_heartbeat"),
        (#"pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b"#, "c2_task_pull"),
        (#"connect\s+to\s+the\s+network\b"#, "c2_network_connect"),
        (#"you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b"#, "forced_action"),
        (#"only\s+use\s+one[\s\-]?liners?\b"#, "anti_forensic_oneliner"),
        (#"never\s+"# + filler + #"(?:create|write)\s+"# + filler + #"(?:script|file)\s+"# + filler + "disk",
         "anti_forensic_disk"),
        (#"unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC)\w*"#, "env_var_unset_agent"),

        (#"\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b"#, "known_c2_framework"),
        (#"\bc2\s+(?:server|channel|infrastructure|beacon)\b"#, "c2_explicit"),
        (#"\bcommand\s+and\s+control\b"#, "c2_explicit_long"),

        (#"curl\s+[^\n]{0,2048}"# + secretVar, "exfil_curl"),
        (#"wget\s+[^\n]{0,2048}"# + secretVar, "exfil_wget"),
        (#"cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)"#, "read_secrets"),
        (#"(send|post|upload|transmit)\s+[^\n]{0,2048}\s+(to|at)\s+https?://"#, "send_to_url"),
        (#"(include|output|print|share)\s+"# + filler
            + #"(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)"#, "context_exfil"),

        (#"authorized_keys"#, "ssh_backdoor"),
        (#"(?:\b(?:echo|cat|cp|mv|dd|tee|install|printf|rsync|scp|ln|append|add|write"#
            + #"|sed|chmod|chown|truncate|rm|touch|curl|wget|git)\b|\bopen\s*\(|>>?)"#
            + #"[^\n]{0,512}(?:\$HOME/\.ssh|~/\.ssh)"#, "ssh_access"),
        (#"\$HOME/\.hermes/\.env|\~/\.hermes/\.env"#, "hermes_env"),
        (modify + #"(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)"#, "agent_config_mod"),
        (modify + #"\.hermes/(config\.yaml|SOUL\.md)"#, "hermes_config_mod"),

        // The lookahead lets a value that is itself an env-var NAME (SHOUTY_SNAKE, 2+ segments) through.
        (#"(?:api[_-]?key|token|secret|password)\s*[=:]\s*["']"#
            + #"(?!(?-i:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)["'])"#
            + #"[A-Za-z0-9+/=_-]{20,}"#, "hardcoded_secret"),
    ]

    /// Zero-width, word-joiner, invisible-operator, BOM, and bidirectional embedding/override/isolate characters.
    public static let invisibleCharacters: Set<Unicode.Scalar> = Set(
        "\u{200B}\u{200C}\u{200D}\u{2060}\u{2062}\u{2063}\u{2064}\u{FEFF}\u{202A}\u{202B}\u{202C}\u{202D}\u{202E}\u{2066}\u{2067}\u{2068}\u{2069}"
            .unicodeScalars)

    /// Finding id for content longer than `maxScanChars`, which the scan can't vouch for.
    public static let scanLimitFinding = "scan_limit_exceeded"

    /// Finding id for a variation selector that doesn't follow a character it can vary, or a second one on the same
    /// character. Selectors placed anywhere else carry data, not appearance.
    public static let variationSelectorFinding = "variation_selector_misuse"

    /// Finding id for more than three invisible characters in a row.
    public static let invisibleRunFinding = "invisible_run"

    /// Finding id for more stray invisible characters than the visible text allows (`invisibleBudget`).
    public static let invisibleBudgetFinding = "invisible_budget"

    /// Stray invisible characters an entry may carry: at least 4, or one per 16 visible characters.
    public static func invisibleBudget(visible: Int) -> Int { max(4, visible / 16) }

    private static let compiled: [(NSRegularExpression, String)] = patterns.map {
        (try! NSRegularExpression(pattern: $0.0, options: [.caseInsensitive]), $0.1)
    }

    /// Literals each pattern can't match without: every group needs one of its strings present. A pattern whose
    /// literals are missing is skipped, which keeps Hermes's 2,048-wide repeats cheap on text that can't match. The
    /// literals are searched with the same engine and options as the patterns (`literalRegexes`), so any text a
    /// pattern matches also contains its literals, whatever the case folding or grapheme clustering around them.
    private static let requiredLiterals: [String: [[String]]] = [
        "prompt_injection": [["ignore"], ["instructions"]],
        "sys_prompt_override": [["override"]],
        "disregard_rules": [["disregard"], ["instructions", "rules", "guidelines"]],
        "bypass_restrictions": [["act"], ["restrictions", "limits", "rules"]],
        "html_comment_injection": [["<!--"], ["-->"]],
        "hidden_div": [["div"], ["display"]],
        "translate_execute": [["translate"], ["execute", "run", "eval"]],
        "deception_hide": [["tell"], ["user"]],
        "role_hijack": [["now"]],
        "role_pretend": [["pretend"]],
        "leak_system_prompt": [["output"], ["prompt"]],
        "remove_filters": [["without"], ["restrictions", "limitations", "filters", "safety"]],
        "fake_update": [["been"], ["updated", "upgraded", "patched"]],
        "identity_override": [["yourself"]],
        "c2_node_registration": [["register"], ["node"]],
        "c2_heartbeat": [["heartbeat", "beacon", "check"]],
        "c2_task_pull": [["pull"], ["task"]],
        "c2_network_connect": [["network"]],
        "forced_action": [["must"], ["register", "connect", "report", "beacon"]],
        "anti_forensic_oneliner": [["only"], ["liner"]],
        "anti_forensic_disk": [["never"], ["disk"]],
        "env_var_unset_agent": [["unset"]],
        "known_c2_framework": [["cobalt", "sliver", "havoc", "mythic", "metasploit", "brainworm"]],
        "c2_explicit": [["c2"]],
        "c2_explicit_long": [["command"], ["control"]],
        "exfil_curl": [["curl"], ["$"]],
        "exfil_wget": [["wget"], ["$"]],
        "read_secrets": [["cat"], [".env", "credentials", ".netrc", ".pgpass", ".npmrc", ".pypirc"]],
        "send_to_url": [["send", "post", "upload", "transmit"], ["http"]],
        "context_exfil": [["include", "output", "print", "share"], ["conversation", "chat", "previous", "context"]],
        "ssh_backdoor": [["authorized_keys"]],
        "ssh_access": [[".ssh"]],
        "hermes_env": [[".hermes/.env"]],
        "agent_config_mod": [["agents.md", "claude.md", ".cursorrules", ".clinerules"]],
        "hermes_config_mod": [[".hermes/"]],
        "hardcoded_secret": [["key", "token", "secret", "password"]],
    ]

    private static let literalRegexes: [String: [[NSRegularExpression]]] = requiredLiterals.mapValues { groups in
        groups.map { group in
            group.map { try! NSRegularExpression(pattern: NSRegularExpression.escapedPattern(for: $0), options: [.caseInsensitive]) }
        }
    }

    static func isVariationSelector(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.value {
        case 0xFE00...0xFE0F, 0xE0100...0xE01EF, 0x180B...0x180D, 0x180F: return true
        default: return false
        }
    }

    /// Whether `selector` can vary `base`: emoji presentation selectors after emoji-capable characters, ideographic
    /// variation selectors after CJK ideographs, Mongolian free variation selectors after Mongolian letters, and the
    /// other standardized selectors after symbols, ideographs, and the scripts that define variants.
    static func canVary(_ base: Unicode.Scalar, with selector: Unicode.Scalar) -> Bool {
        let properties = base.properties
        switch selector.value {
        case 0xFE0E, 0xFE0F: return properties.isEmoji
        case 0xE0100...0xE01EF: return properties.isIdeographic
        case 0x180B...0x180D, 0x180F: return (0x1820...0x18AA).contains(base.value)
        default:
            switch base.value {
            case 0x1000...0x109F, 0xA840...0xA87F, 0x10AC0...0x10AFF, 0x13000...0x1342F: return true
            default: return properties.isIdeographic || properties.generalCategory == .mathSymbol
                || properties.generalCategory == .otherSymbol
            }
        }
    }

    /// One selector per base character; a second identical selector right after it ("❤️️") is tolerated.
    static func isAllowedSelector(_ scalars: [Unicode.Scalar], _ i: Int) -> Bool {
        let selector = scalars[i]
        guard i > 0 else { return false }
        if scalars[i - 1] == selector {
            return i > 1 && canVary(scalars[i - 2], with: selector)
        }
        return canVary(scalars[i - 1], with: selector)
    }

    /// A joiner may follow a visible non-ASCII character or a variation selector, as Persian, Indic scripts, and emoji
    /// sequences (👨‍👩‍👧) need, and a non-joiner may sit between two ASCII letters ("Auf‌lage"). Joiners never chain, and
    /// U+034F or any other invisible never counts as the character before one.
    static func isAllowedJoiner(_ scalars: [Unicode.Scalar], _ i: Int) -> Bool {
        let joiner = scalars[i].value
        guard joiner == 0x200C || joiner == 0x200D, i > 0 else { return false }
        let previous = scalars[i - 1]
        if previous.value <= 0x7F {
            guard joiner == 0x200C, i + 1 < scalars.count else { return false }
            return previous.properties.isAlphabetic && scalars[i + 1].value <= 0x7F && scalars[i + 1].properties.isAlphabetic
        }
        if isVariationSelector(previous) { return true }
        if previous.properties.isDefaultIgnorableCodePoint { return false }
        switch previous.properties.generalCategory {
        case .format, .control, .spaceSeparator, .lineSeparator, .paragraphSeparator, .privateUse, .unassigned: return false
        default: return true
        }
    }

    /// The tag sequences Unicode recommends: the England, Scotland, and Wales flags.
    private static let flagTagSequences: [[Unicode.Scalar]] = ["gbeng", "gbsct", "gbwls"].map { code in
        [Unicode.Scalar(0x1F3F4)!] + code.unicodeScalars.map { Unicode.Scalar(0xE0000 + $0.value)! } + [Unicode.Scalar(0xE007F)!]
    }

    /// Indices of tag characters that belong to one of the recommended flags.
    static func flagTagIndices(_ scalars: [Unicode.Scalar]) -> Set<Int> {
        var valid = Set<Int>()
        for i in scalars.indices where scalars[i].value == 0x1F3F4 {
            for flag in flagTagSequences where i + flag.count <= scalars.count
                && scalars[i..<(i + flag.count)].elementsEqual(flag) {
                valid.formUnion((i + 1)..<(i + flag.count))
            }
        }
        return valid
    }

    /// A joiner between two emoji (👨‍👩‍👧, 🏳️‍🌈) is part of the picture, not hidden text.
    static func isEmojiJoiner(_ scalars: [Unicode.Scalar], _ i: Int) -> Bool {
        func pictograph(_ scalar: Unicode.Scalar) -> Bool { scalar.value > 0x7F && scalar.properties.isEmoji }
        guard scalars[i].value == 0x200D, i > 0, i + 1 < scalars.count else { return false }
        return (pictograph(scalars[i - 1]) || scalars[i - 1].value == 0xFE0F) && pictograph(scalars[i + 1])
    }

    /// Scripts whose spelling uses ZWJ/ZWNJ: Arabic and Syriac, the Indic scripts through Sinhala, Tibetan, Myanmar,
    /// Khmer, and Mongolian.
    static func usesJoiners(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.value {
        case 0x0600...0x08FF, 0x0900...0x0DFF, 0x0F00...0x0FFF, 0x1000...0x109F, 0x1780...0x18AF, 0xA8E0...0xA8FF,
             0xFB50...0xFDFF, 0xFE70...0xFEFF: return !scalar.properties.isDefaultIgnorableCodePoint
        default: return false
        }
    }

    /// A joiner where spelling puts one: between emoji, a non-joiner between two ASCII letters, or after a letter of a
    /// joining script before another one, a space, punctuation, or the end (Persian ZWNJ, Indic conjuncts, chillus).
    static func isSpellingJoiner(_ scalars: [Unicode.Scalar], _ i: Int) -> Bool {
        let joiner = scalars[i].value
        guard joiner == 0x200C || joiner == 0x200D, i > 0 else { return false }
        if isEmojiJoiner(scalars, i) { return true }
        let previous = scalars[i - 1]
        let next = i + 1 < scalars.count ? scalars[i + 1] : nil
        if previous.value <= 0x7F {
            guard joiner == 0x200C, let next, next.value <= 0x7F else { return false }
            return previous.properties.isAlphabetic && next.properties.isAlphabetic
        }
        guard usesJoiners(previous) else { return false }
        guard let next else { return true }
        switch next.properties.generalCategory {
        case .spaceSeparator, .connectorPunctuation, .dashPunctuation, .openPunctuation, .closePunctuation,
             .initialPunctuation, .finalPunctuation, .otherPunctuation: return true
        default: return usesJoiners(next)
        }
    }

    /// Invisible characters in a legitimate place: characters inside the recommended flags, a single variation selector
    /// after a base it can vary, and joiners where spelling puts them. After a digit, #, or * the selector must be
    /// inside a keycap; of U+FE00–FE0D only VS1 (any base) and VS2/VS3 after an ideograph are placed, the selectors
    /// Unicode's standardized variants use. Direction marks depend on their neighbors (`DirectionBoundaries`).
    static func isPlacedInvisible(_ scalars: [Unicode.Scalar], _ i: Int, flagTags: Set<Int>) -> Bool {
        let scalar = scalars[i]
        if flagTags.contains(i) { return true }
        if scalar.value == 0x200C || scalar.value == 0x200D { return isSpellingJoiner(scalars, i) }
        // A selector its base can't take is refused outright, so only its position matters here.
        guard isVariationSelector(scalar), i > 0, !isVariationSelector(scalars[i - 1]) else { return false }
        let base = scalars[i - 1]
        if "0123456789#*".unicodeScalars.contains(base) {
            return scalar.value == 0xFE0F && i + 1 < scalars.count && scalars[i + 1].value == 0x20E3
        }
        switch scalar.value {
        case 0xFE01, 0xFE02: return base.properties.isIdeographic
        case 0xFE03...0xFE0D: return false
        default: return true
        }
    }

    static func isDirectionMark(_ scalar: Unicode.Scalar) -> Bool { [0x200E, 0x200F, 0x061C].contains(scalar.value) }

    private enum Direction { case rightToLeft, leftToRight }

    /// Letters of the right-to-left scripts (Hebrew, Arabic, Syriac, Thaana, N'Ko, and the rest of U+0590–08FF, their
    /// presentation forms, and the historic RTL planes) are strong right-to-left; other letters and every decimal
    /// digit stand on the left-to-right side. Everything else is neutral.
    private static func direction(_ scalar: Unicode.Scalar) -> Direction? {
        let properties = scalar.properties
        if properties.generalCategory == .decimalNumber { return .leftToRight }
        guard properties.isAlphabetic else { return nil }
        switch scalar.value {
        case 0x0590...0x08FF, 0xFB1D...0xFDFF, 0xFE70...0xFEFF, 0x10800...0x10FFF, 0x1E800...0x1EFFF: return .rightToLeft
        default: return .leftToRight
        }
    }

    /// Free direction marks: one per boundary between a right-to-left letter and a left-to-right letter or digit, with
    /// only neutral characters (spaces, punctuation, other invisibles) between the mark and each of them.
    struct DirectionBoundaries {
        private var previousStrong: [Int?] = []
        private var nextStrong: [Int?] = []
        private var directions: [Direction?] = []
        private var used = Set<Int>()

        init(_ scalars: [Unicode.Scalar]) {
            directions = scalars.map(MemoryThreats.direction)
            var last: Int?
            for i in scalars.indices {
                previousStrong.append(last)
                if directions[i] != nil { last = i }
            }
            var next: Int?
            nextStrong = Array(repeating: nil, count: scalars.count)
            for i in scalars.indices.reversed() {
                nextStrong[i] = next
                if directions[i] != nil { next = i }
            }
        }

        /// Whether the mark at `i` is the first one at a real direction boundary; marking it uses up that boundary.
        mutating func isFree(_ i: Int) -> Bool {
            guard let left = previousStrong[i], let right = nextStrong[i], let a = directions[left], let b = directions[right],
                  a != b else { return false }
            return used.insert(left).inserted
        }
    }

    /// `(stray, visible)` scalar counts for the budget. Stray: every default-ignorable, format, variation-selector,
    /// joiner, control (other than tab, LF, CR), private-use, and unassigned scalar that `isPlacedInvisible` or
    /// `DirectionBoundaries` doesn't excuse. Placed invisibles and whitespace count as neither.
    static func strayInvisibleCounts(_ scalars: [Unicode.Scalar], flagTags: Set<Int>) -> (stray: Int, visible: Int) {
        var stray = 0, visible = 0
        var boundaries = DirectionBoundaries(scalars)
        for (i, scalar) in scalars.enumerated() {
            let category = scalar.properties.generalCategory
            let hidden = isVariationSelector(scalar) || scalar.properties.isDefaultIgnorableCodePoint
                || [.format, .privateUse, .unassigned].contains(category)
                || (category == .control && ![0x09, 0x0A, 0x0D].contains(scalar.value))
            if hidden {
                let placed = isDirectionMark(scalar) ? boundaries.isFree(i) : isPlacedInvisible(scalars, i, flagTags: flagTags)
                if !placed { stray += 1 }
            } else if ![.spaceSeparator, .lineSeparator, .paragraphSeparator, .control].contains(category) {
                visible += 1
            }
        }
        return (stray, visible)
    }

    /// Refused hidden characters: Hermes's 17 invisibles (joiners excepted as above), tag characters outside the
    /// recommended flags, misplaced variation selectors, runs of more than three invisible characters, and more stray
    /// invisible characters than `invisibleBudget` allows.
    static func hiddenFindings(_ scalars: [Unicode.Scalar]) -> [String] {
        let flagTags = flagTagIndices(scalars)
        var seen = Set<String>()
        var findings: [String] = []
        var run = 0
        for (i, scalar) in scalars.enumerated() {
            let invisible = !flagTags.contains(i)
                && (scalar.properties.isDefaultIgnorableCodePoint || scalar.properties.generalCategory == .format)
            run = invisible ? run + 1 : 0
            var found: [String] = []
            if invisibleCharacters.contains(scalar) && !isAllowedJoiner(scalars, i) {
                found.append(String(format: "invisible_unicode_U+%04X", scalar.value))
            } else if (0xE0000...0xE007F).contains(scalar.value) && !flagTags.contains(i) {
                found.append(String(format: "invisible_unicode_U+%04X", scalar.value))
            } else if isVariationSelector(scalar) && !isAllowedSelector(scalars, i) {
                found.append(variationSelectorFinding)
            }
            if run > 3 { found.append(invisibleRunFinding) }
            for finding in found where seen.insert(finding).inserted { findings.append(finding) }
        }
        let counts = strayInvisibleCounts(scalars, flagTags: flagTags)
        if counts.stray > invisibleBudget(visible: counts.visible) { findings.append(invisibleBudgetFinding) }
        return findings
    }

    /// Characters that render blank but aren't spaces. They may stand between words or inside one, so the patterns run
    /// once with them as spaces and once with them removed.
    private static let blankLookalikes: Set<UInt32> = [0x2800, 0x1D159, 0x3164, 0x115F, 0x1160, 0xFFA0, 0x180E]

    private enum Cleaned { case keep(Unicode.Scalar), space, blank, drop }

    private static func clean(_ scalar: Unicode.Scalar) -> Cleaned {
        switch scalar.value {
        case 0x0130, 0x0131: return .keep("i")
        case 0x09, 0x0A, 0x0C, 0x0D: return .keep(scalar)
        case 0x0B, 0x1C...0x1F, 0x85: return .space
        case 0xFFFC: return .drop
        case let v where blankLookalikes.contains(v): return .blank
        default: break
        }
        switch scalar.properties.generalCategory {
        case .spaceSeparator, .lineSeparator, .paragraphSeparator: return .space
        case .control, .format, .privateUse, .unassigned, .surrogate, .nonspacingMark, .spacingMark, .enclosingMark: return .drop
        default: return .keep(scalar)
        }
    }

    /// The copies the patterns run on; the stored text is never changed. NFKC folds compatibility forms
    /// (ｃａｔ, ⓘ -> i), then format, private-use, unassigned, and mark characters are dropped, every kind of space
    /// (including Python-only VT, FS–US, NEL) becomes one space, and İ/ı fold to "i" as Python's case-insensitive
    /// matching does.
    static func matchTexts(_ text: String) -> [String] {
        var spaced = String.UnicodeScalarView(), joined = String.UnicodeScalarView()
        var sawBlank = false
        func appendSpace(_ view: inout String.UnicodeScalarView) {
            if view.last != " " { view.append(" ") }
        }
        for scalar in text.precomposedStringWithCompatibilityMapping.unicodeScalars {
            switch clean(scalar) {
            case let .keep(kept):
                spaced.append(kept)
                joined.append(kept)
            case .space:
                appendSpace(&spaced)
                appendSpace(&joined)
            case .blank:
                sawBlank = true
                appendSpace(&spaced)
            case .drop: continue
            }
        }
        return sawBlank ? [String(spaced), String(joined)] : [String(spaced)]
    }

    /// Finding ids in `content`: the scan-limit finding, then hidden-character findings, then pattern ids in
    /// Hermes's order.
    public static func scan(_ content: String) -> [String] {
        guard !content.isEmpty else { return [] }
        let scalars = Array(content.unicodeScalars)
        var findings: [String] = scalars.count > maxScanChars ? [scanLimitFinding] : []
        findings += hiddenFindings(scalars)
        let texts = matchTexts(MemoryText.string(scalars.prefix(maxScanChars)))
        findings += compiled.filter { regex, id in
            texts.contains { text in
                let range = NSRange(text.startIndex..., in: text)
                let groups = literalRegexes[id] ?? []
                guard groups.allSatisfy({ $0.contains { $0.firstMatch(in: text, range: range) != nil } }) else { return false }
                return regex.firstMatch(in: text, range: range) != nil
            }
        }.map(\.1)
        return findings
    }

    /// The rejection text for the first threat found, or nil when the content is clean.
    public static func firstThreatMessage(_ content: String) -> String? {
        guard let id = scan(content).first else { return nil }
        if id == scanLimitFinding {
            return "Blocked: content is longer than \(groupedNumber(maxScanChars)) characters, more than the threat scan "
                + "reads, so it can't be checked for injection."
        }
        if id == variationSelectorFinding {
            return "Blocked: content contains a variation selector that doesn't follow a character it can vary, which "
                + "can hide data (possible injection)."
        }
        if id == invisibleRunFinding {
            return "Blocked: content contains more than three invisible characters in a row, which can hide data "
                + "(possible injection)."
        }
        if id == invisibleBudgetFinding {
            return "Blocked: content contains more stray invisible characters than its visible text allows (at most 4, or one "
                + "per 16 visible characters), which can hide data (possible injection)."
        }
        if id.hasPrefix("invisible_unicode_") {
            let codepoint = id.replacingOccurrences(of: "invisible_unicode_", with: "")
            return "Blocked: content contains invisible unicode character \(codepoint) (possible injection)."
        }
        return "Blocked: content matches threat pattern '\(id)'. "
            + "Content is injected into the system prompt and must not contain "
            + "injection or exfiltration payloads."
    }
}

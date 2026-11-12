// Ported from Hermes Agent (MIT) agent/redact.py (the strict `force=True, redact_url_credentials=True` pass compaction uses).
import Foundation

/// Masks secrets in text that crosses a compaction boundary: summaries persist and re-enter every later prompt.
/// Output matches Hermes's `redact_sensitive_text` on its own shapes; a few shapes Hermes passes through (ASIA key ids,
/// Slack webhook paths, JSON Authorization headers, quoted values with spaces, PEM blocks cut before END) are masked too.
public enum Redaction {
    public static func redact(_ text: String) -> String {
        if text.isEmpty { return text }
        var text = Regex.replace(prefixPattern, in: text) { groups in mask(groups[1] ?? "") }
        if text.contains("=") {
            text = Regex.replace(envAssignPattern, in: text) { groups in
                assignment(groups, keyword: hasSecretKeyword(groups[1] ?? "", caseSensitive: true))
            }
            if !text.contains("://") {
                text = Regex.replace(envAssignLowerPattern, in: text) { groups in assignment(groups, keyword: true) }
            }
            text = Regex.replace(connectionStringKeyPattern, in: text) { groups in "\(groups[1] ?? "")\(mask(groups[2] ?? ""))" }
        }
        if text.contains(":") {
            text = Regex.replace(yamlAssignPattern, in: text) { groups in
                let key = groups[1] ?? ""
                let value = groups[3] ?? ""
                guard hasSecretKeyword(key, caseSensitive: false), !isPlaceholder(value), !value.allSatisfy(\.isNumber) else {
                    return groups[0] ?? ""
                }
                return key + (groups[2] ?? "") + maskQuoted(value)
            }
        }
        if text.contains(":") && text.contains("\"") {
            text = Regex.replace(jsonFieldPattern, in: text) { groups in
                let key = groups[2] ?? ""
                let value = groups[3] ?? ""
                if key.lowercased() == "authorization" || key.lowercased() == "proxy-authorization" {
                    return "\(groups[1] ?? ""): \"\(maskAuthorization(value))\""
                }
                guard jsonKeyHoldsSecret(key), !isPlaceholder(value), !jsonFlagValues.contains(value.lowercased()) else {
                    return groups[0] ?? ""
                }
                return "\(groups[1] ?? ""): \"\(mask(value))\""
            }
        }
        for pattern in [privateKeyPattern, pgpPrivateKeyPattern, ageSecretKeyPattern] {
            text = Regex.replace(pattern, in: text) { _ in "[REDACTED PRIVATE KEY]" }
        }
        if text.range(of: "login", options: .caseInsensitive) != nil {
            text = Regex.replace(containerLoginPattern, in: text) { groups in "\(groups[1] ?? "")\(maskQuoted(groups[2] ?? ""))" }
        }
        text = Regex.replace(cliSecretFlagPattern, in: text) { groups in
            let value = groups[3] ?? ""
            guard !isPlaceholder(value) else { return groups[0] ?? "" }
            return "\(groups[1] ?? "")\(groups[2] ?? "")\(maskQuoted(value))"
        }
        if text.range(of: "authorization", options: .caseInsensitive) != nil {
            text = Regex.replace(authHeaderPattern, in: text) { groups in
                "\(groups[1] ?? "")\(groups[2] ?? "")\(mask(groups[3] ?? ""))"
            }
        }
        text = Regex.replace(secretHeaderPattern, in: text) { groups in "\(groups[1] ?? "")***" }
        if text.range(of: "cookie:", options: .caseInsensitive) != nil {
            text = Regex.replace(cookieLinePattern, in: text) { groups in "\(groups[1] ?? "")***" }
            text = Regex.replace(cookieInlinePattern, in: text) { groups in "\(groups[1] ?? "")***" }
        }
        if text.contains(":") {
            text = Regex.replace(telegramPattern, in: text) { groups in "\(groups[1] ?? "")\(groups[2] ?? ""):***" }
        }
        if text.contains("://") {
            text = Regex.replace(slackWebhookPattern, in: text) { groups in "\(groups[1] ?? "")***" }
            text = Regex.replace(strictURLParamPattern, in: text) { groups in
                let name = (groups[2] ?? "").lowercased()
                return sensitiveQueryParams.contains(name) ? "\(groups[1] ?? "")\(groups[2] ?? "")=***" : groups[0] ?? ""
            }
            text = Regex.replace(strictURLUserinfoPattern, in: text) { _ in "//***:***@" }
        }
        if text.contains("eyJ") {
            text = Regex.replace(jwtPattern, in: text) { groups in mask(groups[0] ?? "") }
        }
        return text
    }

    /// 18-char floor; keeps a 6-char head and 4-char tail so the kind of credential stays visible.
    static func mask(_ token: String) -> String {
        if token.scalarCount < 18 { return "***" }
        return "\(token.scalarPrefix(6))...\(token.scalarSuffix(4))"
    }

    /// Mask the inside of a quoted value, keeping its quotes; unquoted values mask whole.
    private static func maskQuoted(_ value: String) -> String {
        for quote in ["\"", "'"] where value.hasPrefix(quote) {
            let closed = value.hasSuffix(quote) && value.scalarCount > 1
            let inner = closed ? String(value.dropFirst().dropLast()) : String(value.dropFirst())
            return quote + mask(inner) + (closed ? quote : "")
        }
        return mask(value)
    }

    private static func maskAuthorization(_ value: String) -> String {
        let parts = value.split(separator: " ", maxSplits: 1).map(String.init)
        return parts.count == 2 ? "\(parts[0]) \(mask(parts[1]))" : mask(value)
    }

    /// Env assignments mask the whole value (`***`), as Hermes renders them; digits-only values are settings, not secrets.
    private static func assignment(_ groups: [String?], keyword: Bool) -> String {
        let key = groups[1] ?? ""
        let value = groups[2] ?? ""
        let bare = value.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        guard keyword, !isPlaceholder(bare), !bare.isEmpty, !bare.allSatisfy(\.isNumber), !Regex.matches(pathOrVariablePattern, bare) else {
            return groups[0] ?? ""
        }
        let quote = value.hasPrefix("\"") ? "\"" : value.hasPrefix("'") ? "'" : ""
        return "\(key)=\(quote)***\(quote)"
    }

    private static func isPlaceholder(_ value: String) -> Bool {
        value.hasPrefix("$") || value.hasPrefix("***") || value.hasPrefix("[REDACTED") || value.hasPrefix("<")
            || value.hasPrefix("os.getenv") || value.hasPrefix("os.environ") || value.hasPrefix("process.env")
    }

    private static let secretWords = ["api_key", "token", "secret", "password", "passwd", "pass", "pw", "credential", "auth",
                                      "key", "apikey", "authtoken", "secretkey"]

    private static func keyParts(_ key: String) -> [String] {
        key.replacingOccurrences(of: "([a-z0-9])([A-Z])", with: "$1_$2", options: .regularExpression)
            .lowercased().split(whereSeparator: { "_-. ".contains($0) }).map(String.init)
    }

    /// A secret word counts only at a word edge (`_`, `-`, `.`, camelCase or the key's ends), so `KEYBOARD`, `Secretary`
    /// and `tokenizer` stay untouched. `caseSensitive` accepts all-caps keys only (env style).
    static func hasSecretKeyword(_ key: String, caseSensitive: Bool) -> Bool {
        if caseSensitive && key != key.uppercased() { return false }
        if Regex.matches(gluedCredentialEnvPattern, key) { return true }
        let parts = keyParts(key)
        let joined = parts.joined(separator: "_")
        for word in secretWords {
            if word.contains("_") {
                if joined.contains(word) { return true }
            } else if parts.contains(where: { $0 == word || $0 == word + "s" }) {
                return true
            }
        }
        return false
    }

    /// JSON keys hold a secret only when they END in the secret word (`SecretAccessKey`, `SessionToken`); `AccessKeyId`,
    /// `passwordChanged` and `tokenUrl` describe one.
    private static func jsonKeyHoldsSecret(_ key: String) -> Bool {
        guard let last = keyParts(key).last else { return false }
        return ["key", "apikey", "token", "secret", "password", "passwd", "pwd", "credential", "credentials", "bearer"].contains(last)
    }

    private static let jsonFlagValues: Set<String> = ["true", "false", "yes", "no", "null", "none"]

    private static let prefixes = [
        #"sk-[A-Za-z0-9_-](?:\.?[A-Za-z0-9_-]){9,}"#, #"ctx7sk-[A-Za-z0-9_-]{10,}"#,
        #"ghp_[A-Za-z0-9]{10,}"#, #"github_pat_[A-Za-z0-9_]{10,}"#, #"gho_[A-Za-z0-9]{10,}"#, #"ghu_[A-Za-z0-9]{10,}"#,
        #"ghs_[A-Za-z0-9]{10,}"#, #"ghr_[A-Za-z0-9]{10,}"#, #"xapp-\d+-[A-Za-z0-9-]{10,}"#, #"xox[baprs]-[A-Za-z0-9-]{10,}"#,
        #"AIza[A-Za-z0-9_-]{30,}"#, #"pplx-[A-Za-z0-9]{10,}"#, #"fal_[A-Za-z0-9_-]{10,}"#, #"fc-[A-Za-z0-9]{10,}"#,
        #"bb_live_[A-Za-z0-9_-]{10,}"#, #"gAAAA[A-Za-z0-9_=-]{20,}"#, #"AKIA[A-Z0-9]{16}"#, #"ASIA[A-Z0-9]{16}"#,
        #"sk_live_[A-Za-z0-9]{10,}"#, #"sk_test_[A-Za-z0-9]{10,}"#, #"rk_live_[A-Za-z0-9]{10,}"#, #"SG\.[A-Za-z0-9_-]{10,}"#,
        #"hf_[A-Za-z0-9]{10,}"#, #"r8_[A-Za-z0-9]{10,}"#, #"npm_[A-Za-z0-9]{10,}"#, #"pypi-[A-Za-z0-9_-]{10,}"#,
        #"dop_v1_[A-Za-z0-9]{10,}"#, #"doo_v1_[A-Za-z0-9]{10,}"#, #"am_(?:org_)?[A-Za-z0-9]{20,}"#, #"sk_[A-Za-z0-9_]{10,}"#,
        #"tvly-[A-Za-z0-9]{10,}"#, #"exa_[A-Za-z0-9]{10,}"#, #"gsk_[A-Za-z0-9]{10,}"#, #"syt_[A-Za-z0-9]{10,}"#,
        #"xai-[A-Za-z0-9]{30,}"#, #"ntn_[A-Za-z0-9]{10,}"#, #"fw-[A-Za-z0-9]{30,}"#, #"fw_[A-Za-z0-9]{30,}"#,
        #"fpk_[A-Za-z0-9]{30,}"#, #"glpat-[A-Za-z0-9_\-]{10,}"#, #"gloas-[A-Za-z0-9_\-]{10,}"#, #"gldt-[A-Za-z0-9_\-]{10,}"#,
        #"glrt-[A-Za-z0-9_.\-]{10,}"#, #"glcbt-[A-Za-z0-9_\-]{10,}"#, #"glptt-[A-Za-z0-9_\-]{10,}"#,
        #"glft-[A-Za-z0-9_\-]{10,}"#, #"pk-lf-[A-Za-z0-9\-]{8,}"#,
    ]
    private static let prefixPattern = Regex.compile("(?<![A-Za-z0-9])(" + prefixes.joined(separator: "|") + ")(?![A-Za-z0-9_-])")

    private static let assignedValue = #"("(?:\\.|[^"\\\n]){0,512}"|'[^'\n]{0,512}'|[^\s'"&]+)"#
    private static let envAssignPattern = Regex.compile(
        #"(?<![A-Za-z0-9_\-])([A-Z0-9_]{0,50}(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PW|CREDENTIAL|AUTH)S?[A-Z0-9_]{0,50})\s*=\s*"#
            + assignedValue)
    private static let envAssignLowerPattern = Regex.compile(
        #"(?<![A-Za-z0-9_\-])([A-Za-z0-9_]+_(?:key|pass|pw|token|secret|password|passwd|credential|auth)(?=[^A-Za-z0-9_]|$))\s*=\s*"#
            + assignedValue, options: [.caseInsensitive])
    private static let yamlAssignPattern = Regex.compile(
        #"(^[ \t]*(?:[0-9]+(?:[|:\-]|\t)[ \t]*)?[A-Za-z0-9_.\-]*(?:api[ _.\-]?key|token|secret|passwd|password|credential|(?:private|signing|encryption|master|hmac)[ _.\-]?key)[A-Za-z0-9_.\-]*)(:[ \t]*)("(?:\\[^\r\n]|[^"\\\r\n])*"|'(?:''|[^'\r\n])*'|['"][^\r\n]*|[^\s&]+)"#,
        options: [.caseInsensitive, .anchorsMatchLines])
    private static let jsonFieldPattern = Regex.compile(#"("([A-Za-z0-9_.\-]{1,128})")\s*:\s*"((?:[^"\\\n]|\\.)+)""#)
    private static let connectionStringKeyPattern = Regex.compile(
        #"((?<![^;,?&'"\s])(?:AccountKey|SharedAccessKey|SharedAccessSignature)=)([^;\s'"&]+)"#, options: [.caseInsensitive])
    private static let privateKeyPattern = Regex.compile(
        #"-----BEGIN[A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----|(?:[ \t]*\r?\n[A-Za-z0-9+/=:,. \-]*)*)"#)
    /// Values that name a file or variable (`/home/me/sa.json`, `$HOME/x`, `~/.creds`) are references, not secrets.
    private static let pathOrVariablePattern = Regex.compile(#"^(?:\$\{?\w+\}?|\$\(|~|/)(?:[\w./:-]|\$\{?\w+\}?)*$"#)
    /// Credential env names that glue the secret word to a prefix, which the word-edge rule would miss.
    private static let gluedCredentialEnvPattern = Regex.compile("PGPASSWORD|MYSQL_PWD|SSHPASS", options: [.caseInsensitive])
    private static let pgpPrivateKeyPattern = Regex.compile(
        #"-----BEGIN PGP PRIVATE KEY BLOCK-----[\s\S]*?-----END PGP PRIVATE KEY BLOCK-----"#)
    private static let ageSecretKeyPattern = Regex.compile(#"AGE-SECRET-KEY-1[0-9A-Z]{20,}"#)
    private static let containerLoginPattern = Regex.compile(
        #"(\b(?:docker|podman|nerdctl)\s+login\b[^\n;|&]*?\s-p\s*)("[^"\n]{1,512}"|'[^'\n]{1,512}'|\S+)"#)
    private static let cookieLinePattern = Regex.compile(
        #"(^[ \t]*(?:[<>][ \t]+)?(?:Set-)?Cookie:[ \t]*)([!#$%&'*+\-.^_`|~0-9A-Za-z]+=[^\r\n]*)"#,
        options: [.caseInsensitive, .anchorsMatchLines])
    private static let cookieInlinePattern = Regex.compile(
        #"(\b(?:Set-)?Cookie:[ \t]*)([!#$%&'*+\-.^_`|~0-9A-Za-z]+=[^\s;"'`]*(?:;[ \t]*[!#$%&'*+\-.^_`|~0-9A-Za-z]+=[^\s;"'`]*)*)"#,
        options: [.caseInsensitive])
    private static let telegramPattern = Regex.compile(#"(?<![A-Za-z_\d])(bot)?(\d{8,}):[-A-Za-z0-9_]{30,}"#)
    private static let cliSecretFlagPattern = Regex.compile(
        #"((?<![\w-])--?(?:password|passwd|passphrase|pass|token|api[-_]?key|apikey|client[-_]?secret|access[-_]?key|account[-_]?key|auth[-_]?token|session[-_]?token|secret|bearer|credential)s?)(\s*=|\s+)("[^"\n]{1,512}"|'[^'\n]{1,512}'|\S+)"#,
        options: [.caseInsensitive])
    private static let authHeaderPattern = Regex.compile(#"((?:Proxy-)?Authorization:\s*)([A-Za-z][\w.+-]*\s+)?([^\s"']+)"#,
                                                         options: [.caseInsensitive])
    private static let secretHeaderPattern = Regex.compile(
        #"((?<![A-Za-z0-9-])(?:X-)?(?:Api-?Key|Auth-?Token|Access-?Token|Secret-?Key)\s*:\s*)([^\s"']+)"#, options: [.caseInsensitive])
    private static let slackWebhookPattern = Regex.compile(#"(https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/)[A-Za-z0-9]+"#)
    private static let strictURLParamPattern = Regex.compile(#"([?#&;])([A-Za-z0-9_.~+%\-]+)=([^#&;\s"'<>]*)"#)
    private static let strictURLUserinfoPattern = Regex.compile(#"//[^/\s?#@"]+@"#)
    private static let jwtPattern = Regex.compile(#"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}"#)
    private static let sensitiveQueryParams: Set<String> = [
        "access_token", "refresh_token", "id_token", "token", "api_key", "apikey", "client_secret", "password", "auth",
        "jwt", "session", "secret", "key", "code", "signature", "x-amz-signature", "x-goog-signature", "sig",
    ]
}

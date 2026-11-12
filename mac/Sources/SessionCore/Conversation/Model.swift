import Foundation

/// Conversation management for the Assistant chat, ported from Hermes Agent (MIT, Nous Research):
/// https://github.com/NousResearch/hermes-agent

public enum MessageRole: String, Codable, Sendable {
    case system, user, assistant, tool
}

/// One message in an Assistant chat. Tool calls and their results are separate messages, linked by `toolCallID`.
public struct ConversationMessage: Codable, Equatable, Sendable, Identifiable {
    public var id: Int64?
    public var sessionID: String
    public var seq: Int
    public var role: MessageRole
    public var content: String
    public var toolName: String?
    public var toolCallID: String?
    public var timestamp: Date
    /// False once the message is folded into a compaction summary. It stays stored and searchable.
    public var active: Bool
    /// True for a summary message that replaces compacted turns.
    public var isSummary: Bool

    public init(id: Int64? = nil, sessionID: String, seq: Int, role: MessageRole, content: String,
                toolName: String? = nil, toolCallID: String? = nil, timestamp: Date = Date(), active: Bool = true,
                isSummary: Bool = false) {
        self.id = id
        self.sessionID = sessionID
        self.seq = seq
        self.role = role
        self.content = content
        self.toolName = toolName
        self.toolCallID = toolCallID
        self.timestamp = timestamp
        self.active = active
        self.isSummary = isSummary
    }
}

public struct ConversationSession: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var title: String
    /// The session this one continues after a compaction or a new chat.
    public var parentID: String?
    public var createdAt: Date
    public var archived: Bool
    /// Prompt tokens the model last reported for this session, and the message count at that point.
    public var lastPromptTokens: Int?
    public var lastPromptTokensAtSeq: Int?

    public init(id: String = UUID().uuidString, title: String, parentID: String? = nil, createdAt: Date = Date(),
                archived: Bool = false, lastPromptTokens: Int? = nil, lastPromptTokensAtSeq: Int? = nil) {
        self.id = id
        self.title = title
        self.parentID = parentID
        self.createdAt = createdAt
        self.archived = archived
        self.lastPromptTokens = lastPromptTokens
        self.lastPromptTokensAtSeq = lastPromptTokensAtSeq
    }
}

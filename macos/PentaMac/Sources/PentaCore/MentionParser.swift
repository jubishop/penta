import Foundation

private func replacingMatches(in text: String, pattern: String, with replacement: String = "") -> String {
    guard let regex = try? NSRegularExpression(
        pattern: pattern,
        options: [.dotMatchesLineSeparators, .caseInsensitive]
    ) else {
        return text
    }
    let range = NSRange(text.startIndex..<text.endIndex, in: text)
    return regex.stringByReplacingMatches(in: text, options: [], range: range, withTemplate: replacement)
}

public func stripCodeBlocks(from text: String) -> String {
    let withoutFenced = replacingMatches(in: text, pattern: "```[\\s\\S]*?```")
    return replacingMatches(in: withoutFenced, pattern: "`[^`]+`")
}

private func containsRegex(_ pattern: String, in text: String) -> Bool {
    guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else {
        return false
    }
    let range = NSRange(text.startIndex..<text.endIndex, in: text)
    return regex.firstMatch(in: text, options: [], range: range) != nil
}

public func hasBroadcastMention(_ text: String) -> Bool {
    containsRegex("(?<!\\w)@(?:all|everyone)\\b", in: stripCodeBlocks(from: text.lowercased()))
}

public func hasAgentMention(_ text: String, agentName: String) -> Bool {
    let escaped = NSRegularExpression.escapedPattern(for: agentName.lowercased())
    return containsRegex("(?<!\\w)@\(escaped)\\b", in: stripCodeBlocks(from: text.lowercased()))
}

public func extractMentions(from text: String, agents: [AgentConfig]) -> Set<UUID> {
    let stripped = stripCodeBlocks(from: text.lowercased())
    if containsRegex("(?<!\\w)@(?:all|everyone)\\b", in: stripped) {
        return Set(agents.map(\.id))
    }

    var mentioned = Set<UUID>()
    for agent in agents {
        let escaped = NSRegularExpression.escapedPattern(for: agent.name.lowercased())
        if containsRegex("(?<!\\w)@\(escaped)\\b", in: stripped) {
            mentioned.insert(agent.id)
        }
    }
    return mentioned
}

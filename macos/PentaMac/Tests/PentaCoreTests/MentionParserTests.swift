import PentaCore
import Testing

@Test func mentionsIgnoreCodeBlocks() {
    let claude = AgentConfig(name: "claude", kind: .claude)
    let codex = AgentConfig(name: "codex", kind: .codex)

    let mentioned = extractMentions(
        from: "Please ask @codex, not `@claude`.",
        agents: [claude, codex]
    )

    #expect(mentioned == [codex.id])
}

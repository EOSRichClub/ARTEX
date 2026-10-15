import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Request one tool call from the outer ARTEX runtime. Use only tool names and JSON arguments supplied in the current prompt.",
  args: {
    name: tool.schema.string().describe("Exact ARTEX tool name"),
    arguments: tool.schema.string().describe("JSON object encoded as a string"),
  },
  async execute(args) {
    return `ARTEX_TOOL_REQUEST_RECORDED:${args.name}:${args.arguments}`
  },
})

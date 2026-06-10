import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from ollama import AsyncClient

SYSTEM_PROMPT = """You are an autonomous web browser agent. When given a task:
- Analyze the goal and break it into sequential steps
- Use the available browser tools to execute each step one at a time
- After each action, assess the current state and decide the next step
- Continue calling tools until every part of the task is fully complete
- Only produce a plain-text final response when the task is done — summarize what was accomplished

Be methodical and persistent. Never stop mid-task. If something fails, try an alternative approach."""


def mcp_tool_to_ollama(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


async def agent_loop(
    original_task: str,
    messages: list[dict],
    session: ClientSession,
    max_iterations: int = 50,
):
    model = "gemma4:e2b"

    tools_result = await session.list_tools()
    ollama_tools = [mcp_tool_to_ollama(t) for t in tools_result.tools]

    for i in range(max_iterations):
        response = await AsyncClient().chat(
            model=model,
            messages=messages,
            tools=ollama_tools,
            think=True,
        )
        message = response.message

        print(f"\n--- iteration {i + 1} ---")
        if message.thinking:
            print(f"thinking: {message.thinking[:400]}...")
        # print(f"content: {message.content}")

        if not message.tool_calls:
            return message.content

        messages.append(
            {
                "role": message.role,
                "content": message.content or "",
                "tool_calls": message.tool_calls,
            }
        )

        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = tool_call.function.arguments or {}
            print(f"calling tool: {name}({args})")
            result = await session.call_tool(name, args)
            text = "\n".join(
                c.text if hasattr(c, "text") else str(c) for c in result.content
            )

            if ".playwright-mcp/" in text:
                snap = await session.call_tool("browser_snapshot", {})
                snap_text = "\n".join(
                    c.text if hasattr(c, "text") else str(c) for c in snap.content
                )
                text = text + "\n\n### Current page accessibility tree:\n" + snap_text

            # print(f"tool result [{name}]: {text[:300]}")  # truncate for PRINT only
            messages.append({"role": "tool", "content": text})  # full text to model ✅

        print(messages)


async def main():
    original_task = "go to wikipedia, task: search for 'shah rukh khan' and take the screenshot of the home page"
    async with streamable_http_client("http://localhost:8931/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": original_task,
                },
            ]
            result = await agent_loop(original_task, messages, session)
            print(f"\nfinal: {result}")


if __name__ == "__main__":
    asyncio.run(main())

import asyncio

from ollama import AsyncClient, Message


def greeting():
    """Greets the user"""
    return "Hello world"


def goodbye():
    """Exit message for the user"""
    return "Goodbye world"


TOOLS = {"greeting": greeting, "goodbye": goodbye}


async def chat(model: str, messages: list) -> Message:
    response = await AsyncClient().chat(
        model=model, messages=messages, tools=[greeting, goodbye]
    )
    return response.message


async def agent_loop(messages: list[dict], max_iterations: int = 10):
    model = "gemma4:e2b"
    for _ in range(max_iterations):
        response = await chat(model=model, messages=messages)

        if not response.tool_calls:
            return response.content

        print(f"thinking: {response.thinking}")
        print(f"content: {response.content}")
        print(f"tool_calls: {response.tool_calls}")

        messages.append(
            {
                "role": response.role,
                "content": response.content or "",
                "tool_calls": response.tool_calls,
            }
        )

        for tool_call in response.tool_calls:
            name = tool_call.function.name
            args = tool_call.function.arguments or {}
            result = TOOLS[name](**args) if name in TOOLS else f"unknown tool: {name}"
            # print(f"tool result [{name}]: {result}")
            messages.append({"role": "tool", "content": str(result)})


if __name__ == "__main__":
    messages = [{"role": "user", "content": "exit"}]
    result = asyncio.run(agent_loop(messages))
    print(f"final: {result}")

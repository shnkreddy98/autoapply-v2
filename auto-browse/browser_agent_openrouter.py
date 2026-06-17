import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from openai import AsyncOpenAI

load_dotenv()

SYSTEM_PROMPT = """You are an autonomous web browser agent. When given a task:

- Analyze the goal and break it into sequential steps
- Use the available browser tools to execute each step one at a time
- After each action, assess the current state and decide the next step
- Continue calling tools until every part of the task is fully complete
- Only produce a plain-text final response when the task is done — summarize what was accomplished

Be methodical and persistent. Never stop mid-task. If something fails, try an alternative approach."""


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "~openai/gpt-mini-latest"

SNAPSHOT_PATTERN = re.compile(r"\[Snapshot\]\(([^)]+\.ya?ml)\)")


def debug(label: str, value: Any = None) -> None:
    """Print debug output with DEBUG at the start of every message."""

    if value is None:
        print(f"DEBUG {label}")
        return

    if isinstance(value, (dict, list)):
        formatted_value = json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    else:
        formatted_value = str(value)

    print(f"DEBUG {label}: {formatted_value}")


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema
            or {
                "type": "object",
                "properties": {},
            },
        },
    }


def parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if arguments is None or arguments == "":
        return {}

    if isinstance(arguments, dict):
        return arguments

    if isinstance(arguments, str):
        parsed = json.loads(arguments)

        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must decode to a JSON object.")

        return parsed

    raise TypeError(f"Unsupported tool argument type: {type(arguments).__name__}")


def result_text(result: Any) -> str:
    text_parts = [
        content.text for content in result.content if isinstance(content, TextContent)
    ]

    if text_parts:
        return "\n".join(text_parts)

    return str(result.content)


def snapshot_or_tool_result(text: str) -> str:
    match = SNAPSHOT_PATTERN.search(text)

    if not match:
        return text

    snapshot_path = Path(match.group(1))

    if snapshot_path.is_absolute():
        candidates = [snapshot_path]
    else:
        candidates = [
            snapshot_path,
            Path("data") / snapshot_path,
        ]

    for candidate in candidates:
        if candidate.is_file():
            return read_text_file(candidate)

    return (
        f"Snapshot was referenced but could not be read: "
        f"{snapshot_path}\n\n"
        f"Raw tool result:\n{text}"
    )


def create_openrouter_client() -> AsyncOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")

    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Export your OpenRouter API key first."
        )

    default_headers: dict[str, str] = {}

    http_referer = os.getenv("OPENROUTER_HTTP_REFERER")
    if http_referer:
        default_headers["HTTP-Referer"] = http_referer

    app_name = os.getenv("OPENROUTER_APP_NAME")
    if app_name:
        default_headers["X-OpenRouter-Title"] = app_name

    client_options: dict[str, Any] = {
        "api_key": api_key,
        "base_url": OPENROUTER_BASE_URL,
    }

    if default_headers:
        client_options["default_headers"] = default_headers

    return AsyncOpenAI(**client_options)


async def agent_loop(
    model: str,
    original_task: str,
    messages: list[dict[str, Any]],
    session: ClientSession,
    client: AsyncOpenAI,
    max_iterations: int = 50,
) -> str:

    tools_result = await session.list_tools()

    openrouter_tools = [mcp_tool_to_openai(tool) for tool in tools_result.tools]

    for iteration in range(1, max_iterations + 1):
        request_options: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": openrouter_tools,
            "tool_choice": "auto",
        }

        reasoning_effort = os.getenv("OPENROUTER_REASONING_EFFORT")

        if reasoning_effort:
            request_options["extra_body"] = {
                "reasoning": {
                    "effort": reasoning_effort,
                }
            }

        response = await client.chat.completions.create(**request_options)

        message = response.choices[0].message

        debug("iteration", iteration)

        reasoning = getattr(message, "reasoning", None)
        reasoning_details = getattr(
            message,
            "reasoning_details",
            None,
        )

        if reasoning:
            debug("reasoning", reasoning)

        if reasoning_details:
            debug("reasoning_details", reasoning_details)

        if not reasoning and not reasoning_details:
            debug("reasoning", "No reasoning returned by model")

        if not message.tool_calls:
            return message.content or ""

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                tool_call.model_dump(exclude_none=True)
                for tool_call in message.tool_calls
            ],
        }

        if reasoning_details:
            assistant_message["reasoning_details"] = reasoning_details

        if reasoning:
            assistant_message["reasoning"] = reasoning

        messages.append(assistant_message)

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name

            try:
                arguments = parse_tool_arguments(tool_call.function.arguments)

                debug(
                    "tool_call",
                    {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                )

                result = await session.call_tool(
                    tool_name,
                    arguments,
                )

                raw_tool_result = result_text(result)

                debug(
                    "tool_call_result",
                    {
                        "name": tool_name,
                        "result": raw_tool_result,
                    },
                )

                tool_content = snapshot_or_tool_result(raw_tool_result)

                if tool_content != raw_tool_result:
                    debug(
                        "tool_snapshot_content",
                        {
                            "name": tool_name,
                            "content": tool_content,
                        },
                    )

            except Exception as exc:
                tool_content = (
                    f"Tool call {tool_name!r} failed: {type(exc).__name__}: {exc}"
                )

                debug(
                    "tool_call_error",
                    {
                        "name": tool_name,
                        "error": tool_content,
                    },
                )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_content,
                }
            )

        messages.append(
            {
                "role": "user",
                "content": (f"REMINDER — ORIGINAL TASK: {original_task}"),
            }
        )

    raise RuntimeError(f"Agent did not finish after {max_iterations} iterations.")


async def main(message: str, model: str) -> None:

    client = create_openrouter_client()

    try:
        async with streamable_http_client("http://localhost:8931/mcp") as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(
                read_stream,
                write_stream,
            ) as session:
                await session.initialize()

                messages: list[dict[str, Any]] = [
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": message,
                    },
                ]

                result = await agent_loop(
                    model=model,
                    original_task=message,
                    messages=messages,
                    session=session,
                    client=client,
                )

                print(f"final: {result}")

    finally:
        await client.close()


if __name__ == "__main__":
    user_prompt = "go to wikipedia, navigate to shah rukh khan, take a screenshot"
    model = "google/gemini-3-flash-preview"
    asyncio.run(main(message=user_prompt, model=model))
